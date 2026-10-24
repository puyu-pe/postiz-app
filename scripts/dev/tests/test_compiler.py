"""L2 selector contracts: no Nest, pnpm app, dotenv or real service launches."""
import json
import os
import sys
from unittest.mock import patch

from support import ControllerCase, EngineFixture, config, controller, processes


class CompilerTests(ControllerCase):
    def test_selector_validation_before_engine_or_launch(self):
        self.assertEqual(config.compiler({}), 'tsc')
        for value in ('', 'SWC', 'typescript', 'swc ', 'swc\ntsc', '$(shell touch nope)', ';swc'):
            with self.subTest(value=value), patch.dict(os.environ, {'DEV_COMPILER': value}), \
                 patch.object(controller, 'Infrastructure') as engine, patch.object(processes, 'launch') as launch:
                with self.assertRaisesRegex(RuntimeError, 'DEV_COMPILER must be tsc or swc'):
                    controller.up('backend')
                engine.assert_not_called()
                launch.assert_not_called()
        for action in ('up', 'down', 'status', 'logs'):
            with self.subTest(action=action), patch.dict(os.environ, {'DEV_COMPILER': 'invalid'}), \
                 patch.object(sys, 'argv', ['controller.py', action]), patch.object(controller, 'Infrastructure') as engine:
                with self.assertRaisesRegex(RuntimeError, 'DEV_COMPILER'):
                    controller.main()
                engine.assert_not_called()

    def test_valid_selectors_through_make_sources(self):
        self.make_fixture()
        local = self.dir / '.make.local'
        for selected in ('tsc', 'swc'):
            for source in ('environment', 'cli', 'local'):
                with self.subTest(compiler=selected, source=source):
                    local.write_text('')
                    env = dict(os.environ)
                    args = []
                    if source == 'environment':
                        env['DEV_COMPILER'] = selected
                    elif source == 'cli':
                        args = ['DEV_COMPILER=' + selected]
                    else:
                        local.write_text('DEV_COMPILER=' + selected + '\n')
                    captured = self.fixture_make('probe-config', args, env)
                    self.assertEqual(captured.returncode, 0, captured.stderr)
                    self.assertEqual(json.loads(captured.stdout)['DEV_COMPILER'], selected)
                    guarded = self.fixture_make('up', args, env)
                    self.assertNotEqual(guarded.returncode, 0)
                    self.assertIn('TEST: engine or app launch reached', guarded.stderr)

    def test_stopped_record_allows_explicit_new_compiler(self):
        entry, child = self.dummy()
        env = config.app_environment(config.light_values())
        entry.update(compiler='tsc', command=config.COMMANDS['backend'],
                     destination=controller.fingerprint(env, 'tsc'))
        processes.terminate(entry)
        child.wait(timeout=3)
        controller.save({'backend': entry})
        EngineFixture(self)

        def launch(name, env):
            current, new_child = self.dummy()
            current['command'] = config.command(name, env)
            return current, new_child

        with patch.dict(os.environ, {'DEV_COMPILER': 'swc'}), \
             patch.object(processes, 'launch', side_effect=launch), patch.object(controller, 'ready', return_value=True):
            controller.up('backend')
            current = controller.load()['backend']
            self.assertNotEqual(current['pid'], entry['pid'])
            self.assertEqual(current['compiler'], 'swc')
            self.assertEqual(current['destination'], controller.fingerprint(env, 'swc'))
            controller.down()

    def test_commands_all_scopes_and_persisted_fingerprints(self):
        self.require_pidfd()
        EngineFixture(self)
        launched = []

        def launch(name, env):
            entry, child = self.dummy()
            entry['command'] = config.command(name, env)
            launched.append((name, entry['command']))
            return entry, child

        for selected in ('tsc', 'swc'):
            for scope, names in config.SCOPES.items():
                with self.subTest(compiler=selected, scope=scope), patch.dict(os.environ, {'DEV_COMPILER': selected}), \
                     patch.object(processes, 'launch', side_effect=launch), patch.object(controller, 'ready', return_value=True):
                    launched.clear()
                    controller.up(scope)
                    before = (self.dir / 'processes.json').read_bytes()
                    controller.up(scope)
                    self.assertEqual([name for name, _ in launched], list(names))
                    self.assertEqual((self.dir / 'processes.json').read_bytes(), before)
                    env = config.app_environment(config.light_values())
                    for name, entry in controller.load().items():
                        script = {'backend': 'dev:backend', 'workers': 'dev:orchestrator', 'frontend': 'dev:frontend'}[name]
                        if selected == 'swc' and name != 'frontend':
                            script += ':swc'
                        self.assertEqual(entry['command'], ['pnpm', 'run', script])
                        self.assertEqual(entry['compiler'], 'next' if name == 'frontend' else selected)
                        self.assertEqual(entry['destination'], controller.fingerprint(env, entry['compiler']))
                    controller.down()
        env = config.app_environment(config.light_values())
        self.assertNotEqual(controller.fingerprint(env, 'tsc'), controller.fingerprint(env, 'swc'))
        self.assertEqual(config.command('frontend', {'DEV_COMPILER': 'swc'}), config.COMMANDS['frontend'])

    def test_live_compiler_change_refused_without_state_or_process_changes(self):
        for name in ('backend', 'workers'):
            for original, selected in (('tsc', 'swc'), ('swc', 'tsc')):
                with self.subTest(app=name, original=original, selected=selected):
                    entry, child = self.dummy()
                    env = config.app_environment(config.light_values())
                    entry.update(compiler=original, command=config.command(name, {'DEV_COMPILER': original}),
                                 destination=controller.fingerprint(env, original))
                    controller.save({name: entry})
                    before = (self.dir / 'processes.json').read_bytes()
                    with patch.dict(os.environ, {'DEV_COMPILER': selected}), \
                         patch.object(controller, 'Infrastructure') as engine, patch.object(processes, 'terminate') as terminate:
                        with self.assertRaisesRegex(RuntimeError, 'compiler.*make down'):
                            controller.up('backend')  # Includes live workers outside requested scope.
                        engine.assert_not_called()
                        terminate.assert_not_called()
                    self.assertEqual((self.dir / 'processes.json').read_bytes(), before)
                    self.assertIsNone(child.poll())
                    processes.terminate(entry)
                    child.wait(timeout=3)

    def test_legacy_and_ambiguous_metadata_fail_closed(self):
        entry, child = self.dummy()
        env = config.app_environment(config.light_values())
        entry['destination'] = controller.fingerprint(env)
        entry['command'] = config.COMMANDS['backend']
        controller.save({'backend': entry})
        original = (self.dir / 'processes.json').read_bytes()
        controller.check_running('backend', entry, env)  # Exact old command proves tsc.
        self.assertEqual((self.dir / 'processes.json').read_bytes(), original)
        cases = [
            (entry, {**env, 'DEV_COMPILER': 'swc'}),
            ({k: v for k, v in entry.items() if k != 'command'}, env),
            ({**entry, 'command': ['pnpm', 'run', 'unknown']}, env),
            ({**entry, 'compiler': 'tsc', 'destination': controller.fingerprint(env, 'tsc'),
              'command': config.command('backend', {'DEV_COMPILER': 'swc'})}, env),
        ]
        for candidate, requested in cases:
            with self.subTest(candidate=candidate.get('command'), requested=requested['DEV_COMPILER']), \
                 patch.object(controller, 'Infrastructure') as engine, patch.object(processes, 'terminate') as terminate:
                controller.save({'backend': candidate})
                before = (self.dir / 'processes.json').read_bytes()
                with patch.dict(os.environ, requested), self.assertRaisesRegex(RuntimeError, 'make down'):
                    controller.up('backend')
                engine.assert_not_called()
                terminate.assert_not_called()
                self.assertEqual((self.dir / 'processes.json').read_bytes(), before)
                self.assertIsNone(child.poll())
        frontend = {**entry, 'command': config.COMMANDS['frontend']}
        controller.check_running('frontend', frontend, {**env, 'DEV_COMPILER': 'swc'})
        frontend.update(compiler='next', destination=controller.fingerprint(env, 'next'))
        controller.check_running('frontend', frontend, {**env, 'DEV_COMPILER': 'swc'})

    def test_real_supervisor_selected_command_and_environment(self):
        self.require_pidfd()
        fake = self.dir / 'pnpm'
        argv_file = self.dir / 'argv.json'
        fake.write_text(f'#!{sys.executable}\nimport json,os,sys,time\nfrom pathlib import Path\nPath({str(argv_file)!r}).write_text(json.dumps([sys.argv[1:],os.environ["DEV_COMPILER"],os.environ["DEV_CONTROLLER_TOKEN"]]))\ntime.sleep(60)\n')
        fake.chmod(0o700)
        for selected in ('tsc', 'swc'):
            for name in config.COMMANDS:
                with self.subTest(compiler=selected, app=name):
                    argv_file.write_text('')
                    env = {**os.environ, 'DEV_COMPILER': selected, 'PATH': str(self.dir) + ':' + os.environ['PATH']}
                    with patch.object(processes, 'launch', wraps=self.real_launch), controller.atomic_launch():
                        entry, child = processes.launch(name, env)
                        self.children.append((entry, child))
                    controller.wait(lambda: bool(argv_file.read_text()), 'fake pnpm argv', 3)
                    argv, actual, token = json.loads(argv_file.read_text())
                    self.assertEqual(argv, config.command(name, env)[1:])
                    self.assertEqual(entry['command'], config.command(name, env))
                    self.assertEqual(entry['compiler'], config.app_compiler(name, env))
                    self.assertEqual(actual, selected)
                    self.assertEqual(token, entry['token'])
                    self.assertGreaterEqual(len(processes.members(entry)), 2)
                    processes.terminate(entry)
                    child.wait(timeout=3)
                    self.assertFalse(processes.members(entry))
