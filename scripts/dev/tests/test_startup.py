"""Startup prerequisite and public URL reuse contracts; fake tools only."""
import os
import sys
from unittest.mock import patch

from support import ControllerCase, config, controller, processes


class StartupTests(ControllerCase):
    def fake_tool(self, name, version):
        path = self.dir / name
        path.write_text(f'#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ["--version"]\nprint({version!r})\n')
        path.chmod(0o700)
        return path

    def test_path_tool_versions_against_package_declarations(self):
        self.toolchain_patch.stop()
        node = self.fake_tool('node', 'v22.12.0')
        pnpm = self.fake_tool('pnpm', '10.6.1')
        env = {**os.environ, 'PATH': str(self.dir), 'DATABASE_URL': 'do-not-print'}
        controller.validate_toolchain(env)
        for key, files, message in (
            ('node', [pnpm], 'Node.js is missing'),
            ('pnpm', [node], 'pnpm is missing'),
        ):
            with self.subTest(missing=key), patch.dict(os.environ, clear=True):
                directory = self.dir / ('missing-' + key)
                directory.mkdir()
                for executable in files:
                    (directory / executable.name).symlink_to(executable)
                with self.assertRaisesRegex(RuntimeError, message):
                    controller.validate_toolchain({'PATH': str(directory)})
        for node_version, pnpm_version in (('v21.9.0', '10.6.1'), ('v23.0.0', '10.6.1'),
                                           ('v22.12.0', '10.6.0')):
            with self.subTest(node=node_version, pnpm=pnpm_version):
                self.fake_tool('node', node_version)
                self.fake_tool('pnpm', pnpm_version)
                with self.assertRaisesRegex(RuntimeError, '(outside the supported range|not the required version); package.json requires'):
                    controller.validate_toolchain(env)

    def test_corepack_probe_forces_offline_no_prompt_no_pin_and_fails_before_engine(self):
        self.toolchain_patch.stop()
        node = self.fake_tool('node', 'v22.12.0')
        pnpm = self.dir / 'pnpm'
        report = self.dir / 'corepack-env.json'
        pnpm.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ["--version"]
values = {{key: os.environ.get(key) for key in (
    "COREPACK_ENABLE_NETWORK", "COREPACK_ENABLE_DOWNLOAD_PROMPT", "COREPACK_ENABLE_AUTO_PIN")}}
Path({str(report)!r}).write_text(json.dumps(values))
if values != {{"COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "COREPACK_ENABLE_AUTO_PIN": "0"}}:
    raise SystemExit("unsafe Corepack probe environment")
# Simulate an uncached pinned manager. Never access network or install anything.
raise SystemExit(1)
''')
        pnpm.chmod(0o700)
        env = {**os.environ, 'PATH': str(self.dir),
               'COREPACK_ENABLE_NETWORK': '1', 'COREPACK_ENABLE_DOWNLOAD_PROMPT': '1',
               'COREPACK_ENABLE_AUTO_PIN': '1'}
        package_before = (config.ROOT / 'package.json').read_bytes()
        with (patch.object(controller, 'Infrastructure') as engine,
              patch.object(processes, 'launch') as launch,
              self.assertRaisesRegex(RuntimeError, 'local cache first.*will not download or bootstrap')):
            controller.up('backend', env)
        self.assertEqual(__import__('json').loads(report.read_text()), {
            'COREPACK_ENABLE_NETWORK': '0', 'COREPACK_ENABLE_DOWNLOAD_PROMPT': '0',
            'COREPACK_ENABLE_AUTO_PIN': '0'})
        engine.assert_not_called()
        launch.assert_not_called()
        self.assertEqual((config.ROOT / 'package.json').read_bytes(), package_before)

    def test_validation_is_first_and_non_start_actions_skip_toolchain(self):
        calls = []
        with (patch.object(controller, 'validate_toolchain', side_effect=lambda _env: calls.append('tools')),
              patch.object(controller, 'Infrastructure', side_effect=lambda *_: calls.append('engine')),
              patch.object(controller.processes, 'launch', side_effect=lambda *_: calls.append('launch'))):
            with self.assertRaises(RuntimeError):
                controller.up('backend', {})
        self.assertEqual(calls, ['tools'])
        with patch.object(controller, 'validate_toolchain', side_effect=AssertionError('unexpected tool check')):
            for action in ('status', 'logs', 'down'):
                with self.subTest(action=action), patch.object(sys, 'argv', ['controller.py', action]):
                    controller.main()
            with self.assertRaises(SystemExit), patch.object(sys, 'argv', ['controller.py', 'help']):
                controller.main()

    def test_public_url_fingerprint_distinguishes_missing_empty_and_values(self):
        env = config.app_environment(config.light_values())
        baseline = controller.fingerprint(env, 'tsc')
        for key in controller.URL_IDENTITY_KEYS:
            with self.subTest(key=key):
                self.assertNotEqual(baseline, controller.fingerprint({**env, key: ''}, 'tsc'))
                self.assertNotEqual(baseline, controller.fingerprint({**env, key: 'https://example.invalid'}, 'tsc'))
                self.assertNotEqual(controller.fingerprint({**env, key: ''}, 'tsc'),
                                    controller.fingerprint({**env, key: 'https://example.invalid'}, 'tsc'))
        self.assertNotEqual(controller.fingerprint(env, 'tsc'),
                            controller.fingerprint({**env, 'MAIN_URL': ''}, 'tsc'))

    def test_live_public_url_change_fails_before_engine_and_state_rewrite(self):
        entry, child = self.dummy()
        env = config.app_environment(config.light_values())
        entry.update(command=config.command('backend', env), compiler='tsc',
                     destination=controller.fingerprint(env, 'tsc'),
                     url_identity=controller.URL_IDENTITY_VERSION)
        controller.save({'backend': entry})
        state = (self.dir / 'processes.json').read_bytes()
        changed = {**env, 'FRONTEND_URL': ''}
        with (patch.object(controller, 'Infrastructure') as engine,
              patch.object(processes, 'terminate') as terminate,
              self.assertRaisesRegex(RuntimeError, 'configuration differs.*make down')):
            controller.up('backend', changed)
        engine.assert_not_called()
        terminate.assert_not_called()
        self.assertEqual((self.dir / 'processes.json').read_bytes(), state)
        self.assertIsNone(child.poll())
