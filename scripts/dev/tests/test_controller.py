"""Controller contracts using isolated disk fixtures and harmless Python sessions."""
from datetime import datetime, timezone
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import time
from unittest.mock import patch

from support import ControllerCase, EngineFixture, MAKE_INPUTS, APP_MAKE_INPUTS, BASE_ENV, eventually, config, controller, infra, processes


class ControllerTests(ControllerCase):
    def test_docker_jsonl_podman_array_health(self):
        docker = []
        podman = []
        for index, service in enumerate(config.SERVICES):
            labels = {'com.docker.compose.project': config.PROJECT, 'com.docker.compose.service': service}
            docker.append(json.dumps({'ID': str(index + 1) * 12, 'Service': service,
                                      'Labels': ','.join(f'{k}={v}' for k, v in labels.items()),
                                      'State': 'running', 'Health': 'healthy'}))
            podman.append({'Id': str(index + 1) * 64, 'Labels': {
                               'io.podman.compose.project': config.PROJECT,
                               'io.podman.compose.service': service},
                           'State': 'running', 'Status': 'Up 1 minute (healthy)'})
        self.assertTrue(all(x['healthy'] for x in infra.normalize('\n'.join(docker)).values()))
        self.assertTrue(all(x['healthy'] for x in infra.normalize(json.dumps(podman, indent=2)).values()))
        podman[0]['State'] = {'Status': 'exited', 'Running': False, 'Health': {'Status': 'healthy'}}
        self.assertFalse(infra.normalize(json.dumps(podman))['postgres']['healthy'])
        podman[0]['State'] = {'Status': 'running', 'Running': True, 'Healthcheck': {'Status': 'healthy'}}
        self.assertTrue(infra.normalize(json.dumps(podman))['postgres']['healthy'])
        self.assertEqual(infra.normalize('[]'), {})
        for bad in ('', 'not json', '[null]', '{}', json.dumps(podman + [podman[0]])):
            with self.subTest(bad=bad[:20]), self.assertRaises(RuntimeError):
                infra.normalize(bad)

    def test_effective_destinations_fail_before_engine(self):
        values = config.light_values()
        env = config.app_environment(values)
        self.assertEqual(env['TEMPORAL_API_KEY'], '')
        self.assertEqual(env['TEMPORAL_TLS'], 'false')
        self.assertEqual(env['PORT'], os.environ['PORT'])
        invalid = {
            'DATABASE_URL': ['', BASE_ENV['DATABASE_URL'].replace('postiz_light:', 'wrong:'),
                             BASE_ENV['DATABASE_URL'].replace('/postiz_light', '/real_db'),
                             BASE_ENV['DATABASE_URL'] + '?host=real.example',
                             BASE_ENV['DATABASE_URL'].replace('15433', '5432'),
                             BASE_ENV['DATABASE_URL'].replace('postiz-light-local-only', 'wrong-password'),
                             BASE_ENV['DATABASE_URL'] + '?host=/var/run/postgresql',
                             BASE_ENV['DATABASE_URL'] + '?schema=public&host=foreign',
                             BASE_ENV['DATABASE_URL'] + '#override'],
            'REDIS_URL': ['', 'redis://127.0.0.1:6379/0', 'redis://127.0.0.1:6380/2',
                             'redis://127.0.0.1:6380/0?host=foreign',
                             'redis://user:password@127.0.0.1:6380/0'],
            'TEMPORAL_ADDRESS': ['', 'real.example:7233'],
            'TEMPORAL_NAMESPACE': ['', 'production'],
            'TEMPORAL_TLS': ['true'], 'TEMPORAL_API_KEY': ['remote-key'],
            'LIGHT_POSTGRES_PORT': ['0', '65536', 'invalid'],
            'LIGHT_REDIS_PORT': ['15433'], 'PORT': ['4200'], 'ORCHESTRATOR_PORT': ['7234'],
        }
        for key, candidates in invalid.items():
            for candidate in candidates:
                with self.subTest(key=key, candidate=candidate):
                    with patch.dict(os.environ, {key: candidate}), patch.object(controller, 'Infrastructure') as engine:
                        with self.assertRaises(RuntimeError):
                            controller.up('backend')
                        engine.assert_not_called()

    def test_all_make_inputs_literal_environment_cli_and_local(self):
        self.make_fixture()
        local = self.dir / '.make.local'
        for source in ('environment', 'cli', 'local'):
            for key in MAKE_INPUTS:
                with self.subTest(source=source, key=key):
                    marker = self.dir / f'EXECUTED-{source}-{key}'
                    attack = f'$(shell printf executed > {marker})invalid'
                    env = {**os.environ, 'SCOPE': 'backend'}
                    args = []
                    local.write_text('')
                    if source == 'environment':
                        env[key] = attack
                    elif source == 'cli':
                        args = [key + '=' + attack]
                    else:
                        local.write_text(key + '=' + attack + '\n')
                    captured = self.fixture_make('probe-config', args, env)
                    self.assertEqual(captured.returncode, 0, captured.stderr)
                    self.assertEqual(json.loads(captured.stdout)[key], attack)
                    rejected = self.fixture_make('up', args, env)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertNotIn('TEST: engine or app launch reached', rejected.stderr)
                    expected = ('CONTAINER_ENGINE must be' if key == 'CONTAINER_ENGINE' else
                                'SCOPE must be' if key == 'SCOPE' else
                                'Light Temporal requires' if key in ('TEMPORAL_TLS', 'TEMPORAL_API_KEY') else key)
                    self.assertIn(expected, rejected.stderr)
                    self.assertFalse(marker.exists())

    def test_make_defaults_and_configuration_precedence(self):
        self.make_fixture()
        env = {key: value for key, value in os.environ.items() if key not in MAKE_INPUTS}
        defaults = self.fixture_make('probe-config', env=env)
        self.assertEqual(defaults.returncode, 0, defaults.stderr)
        values = json.loads(defaults.stdout)
        self.assertEqual(values.pop('CONTAINER_ENGINE'), 'docker')
        self.assertEqual(values.pop('SCOPE'), 'full')
        self.assertEqual(values.pop('DEV_COMPILER'), 'tsc')
        self.assertTrue(all(value is None for value in values.values()))
        env.update(CONTAINER_ENGINE='docker', SCOPE='backend', PORT='3010')
        captured = self.fixture_make('probe-config', env=env)
        self.assertEqual(captured.returncode, 0, captured.stderr)
        self.assertEqual(json.loads(captured.stdout)['PORT'], '3010')
        (self.dir / '.make.local').write_text('CONTAINER_ENGINE=podman\nSCOPE=workers\nPORT=3011\n')
        for args, expected in (([], ('podman', 'workers', '3011')),
                               (['CONTAINER_ENGINE=docker', 'SCOPE=full', 'PORT=3012'],
                                ('docker', 'full', '3012'))):
            captured = self.fixture_make('probe-config', args, env)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            values = json.loads(captured.stdout)
            self.assertEqual(tuple(values[key] for key in ('CONTAINER_ENGINE', 'SCOPE', 'PORT')), expected)

    def test_private_file_values_route_through_make_without_exporting_unset_inputs(self):
        self.make_fixture()
        local_file = self.dir / '.dev.env.local'
        make_local = self.dir / '.make.local'
        make_env = {key: value for key, value in os.environ.items() if key not in MAKE_INPUTS + APP_MAKE_INPUTS}
        marker = self.dir / 'payload-executed'
        local_file.write_text(f'PORT=3111\nJWT_SECRET=$(touch {marker})\nMAIN_URL=file-main\n')
        captured = self.fixture_make('probe-private', env=make_env)
        self.assertEqual(captured.returncode, 0, captured.stderr)
        values = json.loads(captured.stdout)
        self.assertEqual(values['PORT'], '3111')
        self.assertEqual(values['JWT_SECRET'], f'$(touch {marker})')
        self.assertEqual(values['MAIN_URL'], 'file-main')
        self.assertFalse(marker.exists())

        make_local.write_text('PORT=3222\n')
        values = json.loads(self.fixture_make('probe-private', env=make_env).stdout)
        self.assertEqual(values['PORT'], '3222')  # inherited Make variable beats file
        values = json.loads(self.fixture_make('probe-private', ['PORT='], make_env).stdout)
        self.assertEqual(values['PORT'], '')  # explicit empty Make CLI value beats both
        make_local.write_text('PORT=\n')
        values = json.loads(self.fixture_make('probe-private', env=make_env).stdout)
        self.assertEqual(values['PORT'], '')  # explicit empty .make.local value beats file
        make_local.write_text('')
        make_env['PORT'] = '3333'
        values = json.loads(self.fixture_make('probe-private', env=make_env).stdout)
        self.assertEqual(values['PORT'], '3333')  # inherited environment beats file
        make_env['PORT'] = ''
        values = json.loads(self.fixture_make('probe-private', env=make_env).stdout)
        self.assertEqual(values['PORT'], '')  # inherited empty remains set

    def test_app_make_inputs_precedence_and_empty_export(self):
        self.make_fixture()
        for key in APP_MAKE_INPUTS:
            with self.subTest(key=key):
                env = {k: v for k, v in os.environ.items() if k not in MAKE_INPUTS + APP_MAKE_INPUTS}
                env[key] = 'from-environment'
                local = self.dir / '.make.local'
                local.write_text(key + '=from-local\n')
                captured = self.fixture_make('probe-config', env=env)
                self.assertEqual(json.loads(captured.stdout)[key], 'from-local')
                captured = self.fixture_make('probe-config', [key + '='], env=env)
                self.assertEqual(json.loads(captured.stdout)[key], '')

    def test_precedence_for_all_supported_make_inputs(self):
        self.make_fixture()
        local = self.dir / '.make.local'
        for key in MAKE_INPUTS:
            with self.subTest(key=key):
                env = {**os.environ, key: 'from-environment'}
                local.write_text(key + '=from-local\n')
                captured = self.fixture_make('probe-config', env=env)
                self.assertEqual(captured.returncode, 0, captured.stderr)
                self.assertEqual(json.loads(captured.stdout)[key], 'from-local')
                captured = self.fixture_make('probe-config', [key + '=from-cli'], env)
                self.assertEqual(captured.returncode, 0, captured.stderr)
                self.assertEqual(json.loads(captured.stdout)[key], 'from-cli')

    def test_foreign_port_rejected_before_infrastructure(self):
        self.require_pidfd()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            sock.listen()
            with patch.dict(os.environ, {'PORT': str(sock.getsockname()[1])}), patch.object(controller, 'Infrastructure') as engine:
                with self.assertRaisesRegex(RuntimeError, 'foreign listener'):
                    controller.up('backend')
                engine.assert_not_called()

    def test_listener_poller_and_pid_identity(self):
        port_file = self.dir / 'port'
        code = (
            'import socket,time; from pathlib import Path; '
            's=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(); '
            f'Path({str(port_file)!r}).write_text(str(s.getsockname()[1])); time.sleep(60)'
        )
        entry, child = self.dummy(code)
        controller.wait(port_file.exists, 'dummy listener', 3)
        port = int(port_file.read_text())
        self.assertTrue(processes.own_listener(entry, port))
        poller = {'identity': f'{child.pid}@{socket.gethostname()}', 'taskQueueType': 'workflow',
                  'lastAccessTime': datetime.now(timezone.utc).isoformat()}
        self.assertTrue(controller.own_poller(entry, [poller]))
        from pathlib import Path
        boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
        before_start = boot + entry['start'] / os.sysconf('SC_CLK_TCK') - 10
        cases = (
            {'identity': '1@foreign'}, {'taskQueueType': 'activity'},
            {'lastAccessTime': '2000-01-01T00:00:00Z'}, {'lastAccessTime': 'invalid'},
            {'lastAccessTime': datetime.fromtimestamp(before_start, timezone.utc).isoformat()},
            {'lastAccessTime': datetime.fromtimestamp(time.time() + 30, timezone.utc).isoformat()},
        )
        for wrong in cases:
            with self.subTest(poller=wrong):
                self.assertFalse(controller.own_poller(entry, [{**poller, **wrong}]))
        with self.assertRaises(RuntimeError):
            processes.terminate({**entry, 'start': entry['start'] - 1})
        self.assertIsNone(child.poll())
        with self.assertRaises(RuntimeError):
            processes.terminate({**entry, 'token': 'postiz-light-wrong'})
        self.assertIsNone(child.poll())
        processes.terminate(entry)
        child.wait(timeout=3)
        self.assertFalse(processes.members(entry))
        self.assertFalse(processes.listeners(port))
        self.assertFalse(controller.own_poller(entry, [poller]))

    def test_combined_http_worker_readiness(self):
        port_file = self.dir / 'http-port'
        code = f'''from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{{"status":"ok"}}')
server=HTTPServer(('127.0.0.1',0),Handler)
Path({str(port_file)!r}).write_text(str(server.server_port))
server.serve_forever()
'''
        entry, child = self.dummy(code)
        controller.wait(port_file.exists, 'dummy HTTP', 3)
        port = int(port_file.read_text())
        env = {'ORCHESTRATOR_PORT': str(port), 'PORT': '3000'}
        poller = {'identity': f'{child.pid}@{socket.gethostname()}', 'taskQueueType': 'workflow',
                  'lastAccessTime': datetime.now(timezone.utc).isoformat()}
        class FakeInfra:
            def pollers(self):
                return [poller]
        self.assertTrue(controller.ready('workers', entry, env, FakeInfra()))
        for wrong in ([], [{**poller, 'identity': '1@foreign'}],
                      [{**poller, 'taskQueueType': 'activity'}],
                      [{**poller, 'lastAccessTime': '2000-01-01T00:00:00Z'}]):
            with self.subTest(pollers=wrong), patch.object(FakeInfra, 'pollers', return_value=wrong):
                self.assertFalse(controller.ready('workers', entry, env, FakeInfra()))
        # A live owned worker/poller cannot borrow a foreign HTTP listener.
        foreign, other = self.dummy()
        with patch.object(FakeInfra, 'pollers', return_value=[poller]) as get_pollers:
            self.assertFalse(controller.ready('workers', foreign, env, FakeInfra()))
            get_pollers.assert_not_called()
        self.assertIsNone(other.poll())
        # An actual owned TCP server that speaks no HTTP is insufficient too.
        tcp_file = self.dir / 'tcp-port'
        tcp_code = f'''import socket
from pathlib import Path
s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen()
Path({str(tcp_file)!r}).write_text(str(s.getsockname()[1]))
while True:
    connection,_=s.accept()
    connection.recv(1024)
    connection.sendall(b'not-http\\r\\n')
    connection.close()
'''
        tcp_entry, _ = self.dummy(tcp_code)
        eventually(tcp_file.exists)
        tcp_env = {**env, 'ORCHESTRATOR_PORT': tcp_file.read_text()}
        self.assertTrue(processes.own_listener(tcp_entry, int(tcp_file.read_text())))
        with patch.object(FakeInfra, 'pollers', return_value=[poller]) as get_pollers:
            self.assertFalse(controller.ready('workers', tcp_entry, tcp_env, FakeInfra()))
            get_pollers.assert_not_called()

    def test_idempotence_expansion_narrowing_and_down(self):
        self.require_pidfd()
        fake = EngineFixture(self)
        launched = []
        def launch(name, env):
            launched.append(name)
            entry, child = self.dummy()
            entry['command'] = config.command(name, env)
            return entry, child
        with patch.object(processes, 'launch', side_effect=launch), patch.object(controller, 'ready', return_value=True):
            controller.up('backend')
            first = controller.load()['backend']['pid']
            controller.up('backend')
            controller.up('workers')
            controller.up('backend')
            self.assertEqual(launched, ['backend', 'workers'])
            self.assertEqual(controller.load()['backend']['pid'], first)
            self.assertEqual(set(controller.load()), {'backend', 'workers'})
            controller.down()
            self.assertEqual(controller.load(), {})
            controller.down()
        starts = [c for c in fake.commands() if 'up' in c['argv']]
        stops = [c for c in fake.commands() if c['argv'][0] == 'stop']
        self.assertEqual(len(starts), 1, 'Repeated up must not recreate existing infrastructure')
        self.assertEqual(len(stops), 1, 'Repeated down must not signal stopped containers')
        self.assertEqual(set(stops[0]['argv'][3:]), {item['Id'] for item in fake.templates.values()})
        fake.assert_preserved_data()
        for entry, child in self.children:
            child.wait(timeout=3)
            self.assertFalse(processes.members(entry))

    def test_empty_ps_and_engine_environment(self):
        with patch.dict(os.environ, {'CONTAINER_ENGINE': 'docker', 'DOCKER_HOST': 'tcp://foreign:2375',
                                     'CONTAINER_HOST': 'ssh://foreign', 'COMPOSE_FILE': 'foreign.yaml'}), patch('infra.shutil.which', return_value='/fake/docker'):
            stack = infra.Infrastructure(config.light_values())
        for key in ('DOCKER_HOST', 'CONTAINER_HOST', 'COMPOSE_FILE'):
            self.assertNotIn(key, stack.env)
        with patch.object(stack, 'command', return_value=''):
            self.assertEqual(stack.inventory(), {})
        with patch.object(stack, 'command', return_value='broken-json'):
            with self.assertRaises(RuntimeError):
                stack.inventory()

    def test_leader_exit_orphan_cleanup(self):
        foreign, other = self.dummy()
        pid_file = self.dir / 'orphan'
        code = f'''import os,time
from pathlib import Path
pid=os.fork()
if pid:
    Path({str(pid_file)!r}).write_text(str(pid))
    os._exit(0)
time.sleep(60)
'''
        entry, child = self.dummy(code)
        controller.wait(pid_file.exists, 'orphan creation', 3)
        orphan = int(pid_file.read_text())
        child.wait(timeout=3)  # leader disappears; orphan still in the recorded session
        self.assertIn(orphan, [x['pid'] for x in processes.members(entry)])
        processes.terminate(entry)
        os.waitpid(orphan, 0)
        self.assertFalse(processes.members(entry))
        self.assertIsNone(other.poll(), 'Unrelated session must survive orphan cleanup')
        self.assertTrue(processes.members(foreign))

    def test_members_ambiguous_diagnostic_is_safe_and_refuses_ownership(self):
        info = {'pid': 12345, 'pgid': 12345, 'sid': 12345, 'start': 100, 'state': 'S'}
        token_value = 'postiz-light-secret-marker'
        entry = {**info, 'root': str(config.ROOT), 'token': token_value}
        path = processes.Path('/proc/12345')
        with patch.object(processes, 'proc', side_effect=[info, info, info]), \
             patch.object(processes.Path, 'iterdir', return_value=iter([path])), \
             patch.object(processes.Path, 'read_bytes', return_value=b''), \
             patch.object(processes.os, 'readlink', return_value='/tmp'), \
             patch.object(processes, 'signal_member') as send_signal:
            with self.assertRaisesRegex(RuntimeError, 'Ambiguous session member') as raised:
                processes.members(entry)
        diagnostic = str(raised.exception)
        self.assertIn('pid=12345', diagnostic)
        self.assertIn('tagged=False', diagnostic)
        self.assertIn('cwd_within_root=False', diagnostic)
        self.assertIn('start_valid=True', diagnostic)
        self.assertIn('identity_stable=True', diagnostic)
        self.assertNotIn(token_value, diagnostic)
        send_signal.assert_not_called()

    def test_members_procfs_exit_races_and_permission_errors(self):
        # A task can exit between stat and reading its token/cwd. Only ENOENT
        # and ESRCH mean disappearance; access/IO errors must still fail closed.
        info = {'pid': 12345, 'pgid': 12345, 'sid': 12345, 'start': 100, 'state': 'S'}
        entry = {**info, 'root': str(config.ROOT), 'token': 'postiz-light-race-test'}
        token = ('DEV_CONTROLLER_TOKEN=' + entry['token']).encode()
        path = processes.Path('/proc/12345')
        for stage in ('environ', 'cwd'):
            for code in (errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EPERM, errno.EIO):
                with self.subTest(stage=stage, errno=code), \
                     patch.object(processes, 'proc', return_value=info), \
                     patch.object(processes.Path, 'iterdir', return_value=iter([path])), \
                     patch.object(processes.Path, 'read_bytes',
                                  side_effect=OSError(code, 'injected procfs race') if stage == 'environ' else None,
                                  return_value=token), \
                     patch.object(processes.os, 'readlink',
                                  side_effect=OSError(code, 'injected procfs race') if stage == 'cwd' else None,
                                  return_value=str(config.ROOT)), \
                     patch.object(processes, 'signal_member') as send_signal:
                    if code in (errno.ENOENT, errno.ESRCH):
                        self.assertEqual(processes.members(entry), [])
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'Cannot verify session member ownership') as raised:
                            processes.members(entry)
                        self.assertEqual(raised.exception.__cause__.errno, code)
                    send_signal.assert_not_called()

    def test_actual_supervisor_argv_and_shutdown(self):
        self.require_pidfd()
        # Fake pnpm exercises the real supervisor seam without invoking apps.
        fake = self.dir / 'pnpm'
        argv_file = self.dir / 'argv'
        fake.write_text(f'#!{sys.executable}\nimport sys,time\nfrom pathlib import Path\nPath({str(argv_file)!r}).write_text(repr(sys.argv[1:]))\ntime.sleep(60)\n')
        fake.chmod(0o700)
        env = {**os.environ, 'PATH': str(self.dir) + ':' + os.environ['PATH']}
        with patch.object(processes, 'launch', wraps=self.real_launch), controller.atomic_launch():
            entry, child = processes.launch('backend', env)
            self.children.append((entry, child))
        controller.wait(argv_file.exists, 'fake pnpm', 3)
        self.assertEqual(argv_file.read_text(), "['run', 'dev:backend']")
        self.assertGreaterEqual(len(processes.members(entry)), 2)
        processes.terminate(entry)
        child.wait(timeout=3)
        self.assertFalse(processes.members(entry))

    def test_external_timeout_cleans_helpers(self):
        self.require_pidfd()
        pid_file = self.dir / 'command-child'
        entry_file = self.dir / 'command-entry.json'
        code = f'''import json,os,time,sys
from pathlib import Path
sys.path.insert(0, {str(config.ROOT / 'scripts/dev')!r})
import processes
info=processes.proc(os.getpid())
entry={{'pid':os.getpid(), 'pgid':os.getpid(), 'sid':os.getpid(), 'start':info['start'],
       'root':{str(config.ROOT)!r}, 'token':os.environ['DEV_CONTROLLER_TOKEN']}}
Path({str(entry_file)!r}).write_text(json.dumps(entry))
pid=os.fork()
if pid:
    Path({str(pid_file)!r}).write_text(str(pid))
time.sleep(60)
'''
        def cleanup():
            if entry_file.exists():
                entry = json.loads(entry_file.read_text())
                processes.terminate(entry)
                self.assertFalse(processes.members(entry))
        self.addCleanup(cleanup)
        began = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            infra.execute([sys.executable, '-c', code], timeout=.4)
        self.assertLess(time.monotonic() - began, 4)
        pid = int(pid_file.read_text())
        info = processes.proc(pid)
        self.assertTrue(info is None or info['state'] == 'Z')
        os.waitpid(pid, 0)

    def test_inventory_failure_never_starts(self):
        self.require_pidfd()
        with patch.object(controller, 'Infrastructure') as engine:
            engine.return_value.inventory.side_effect = RuntimeError('injected inventory failure')
            with self.assertRaisesRegex(RuntimeError, 'inventory failure'):
                controller.up('backend')
            engine.return_value.start.assert_not_called()

    def test_simulated_docker_podman_commands_and_main_queue(self):
        for engine, provider in (('docker', False), ('podman', False), ('podman', True)):
            with self.subTest(engine=engine, provider=provider):
                # Each provider gets its own executable directory and inventory.
                if provider:
                    self.dir = self.dir / 'provider'
                    self.dir.mkdir()
                fake = EngineFixture(self, engine, provider)
                stack = infra.Infrastructure(config.light_values())
                self.assertEqual(stack.inventory(), {})
                stack.start(list(config.SERVICES))
                self.assertEqual(set(stack.inventory()), set(config.SERVICES))
                self.assertEqual(stack.pollers(), [])
                command = next(c['argv'] for c in fake.commands() if c['argv'][0] == 'exec')
                for flag, expected in (('--namespace', 'default'), ('--task-queue', 'main'),
                                       ('--task-queue-type', 'workflow')):
                    self.assertEqual(command[command.index(flag) + 1], expected)
                self.assertIn('--select-unversioned', command)
                stack.stop(stack.inventory())
                self.assertTrue(all(not item['running'] for item in stack.inventory().values()))
                fake.assert_preserved_data()

    def rollback_scenario(self, failure):
        fake = EngineFixture(self)
        # A healthy container must survive; a stopped container newly started
        # by this invocation must be rolled back, not treated as preexisting up.
        stopped = json.loads(json.dumps(fake.templates['redis']))
        stopped['State'].update(Status='exited', Running=False)
        fake.seed({'postgres': fake.templates['postgres'], 'redis': stopped})
        existing, old_child = self.dummy()
        existing['destination'] = controller.fingerprint(config.app_environment(config.light_values()))
        existing['command'] = config.command('backend', os.environ)
        existing['url_identity'] = controller.URL_IDENTITY_VERSION
        if config.compiler() == 'swc':
            existing['compiler'] = 'swc'
            existing['destination'] = controller.fingerprint(config.app_environment(config.light_values()), 'swc')
        controller.save({'backend': existing})
        launched = []
        def launch(name, env):
            launched.append(name)
            entry, child = self.dummy()
            entry['command'] = config.command(name, env)
            return entry, child
        def readiness(name, *args):
            if name == 'workers':
                if failure == 'interrupt':
                    raise KeyboardInterrupt('injected after durable worker registration')
                return False
            return True
        real_wait = controller.wait
        def bounded_wait(predicate, label, seconds):
            return real_wait(predicate, label, .15)
        real_start = infra.Infrastructure.start
        def start(stack, names):
            if failure == 'partial-compose':
                real_start(stack, ['redis'])
                raise RuntimeError('injected partial Compose startup failure')
            real_start(stack, names)
        began = time.monotonic()
        error = KeyboardInterrupt if failure == 'interrupt' else RuntimeError
        with patch.object(processes, 'launch', side_effect=launch), \
             patch.object(controller, 'ready', side_effect=readiness), \
             patch.object(controller, 'wait', side_effect=bounded_wait), \
             patch.object(infra.Infrastructure, 'start', new=start):
            with self.assertRaises(error):
                controller.up('workers')
        self.assertLess(time.monotonic() - began, 5, 'Rollback must remain bounded')
        self.assertEqual(controller.load(), {'backend': existing})
        self.assertIsNone(old_child.poll(), 'Preexisting healthy app must survive rollback')
        items = infra.Infrastructure(config.light_values()).inventory()
        self.assertTrue(items['postgres']['running'])
        self.assertFalse(items['redis']['running'])
        if failure != 'partial-compose':
            self.assertFalse(items['temporal']['running'])
            self.assertEqual(launched, ['workers'])
        else:
            self.assertNotIn('temporal', items)
            self.assertEqual(launched, [])
        for entry, child in self.children[1:]:
            child.wait(timeout=3)
            self.assertFalse(processes.members(entry))
        fake.assert_preserved_data()

    def test_swc_additive_scopes_preserve_active_components(self):
        with patch.dict(os.environ, {'DEV_COMPILER': 'swc'}):
            self.test_idempotence_expansion_narrowing_and_down()

    def test_swc_timeout_preserves_previous_compiler_fingerprint(self):
        with patch.dict(os.environ, {'DEV_COMPILER': 'swc'}):
            self.rollback_scenario('timeout')

    def test_timeout_rolls_back_only_new_components(self):
        self.rollback_scenario('timeout')

    def test_keyboard_interrupt_rolls_back_only_new_components(self):
        self.rollback_scenario('interrupt')

    def test_partial_compose_failure_rolls_back_only_new_components(self):
        self.rollback_scenario('partial-compose')

    def test_stale_reused_pid_and_pidfd_identity_recheck(self):
        entry, child = self.dummy()
        info = processes.proc(child.pid)
        # Real pidfd_open on a harmless live process; a stale start time must
        # never send to that fd. PID reuse itself cannot be forced safely.
        with patch.object(signal, 'pidfd_send_signal', wraps=signal.pidfd_send_signal) as send:
            processes.signal_member({**info, 'start': info['start'] - 1}, signal.SIGTERM)
            send.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'changed/reused'):
            processes.terminate({**entry, 'start': entry['start'] - 1})
        with self.assertRaises(RuntimeError):
            processes.terminate({**entry, 'token': 'postiz-light-foreign'})
        self.assertIsNone(child.poll(), 'PID identity mismatch must preserve foreign live task')
        processes.terminate(entry)
        child.wait(timeout=3)
        self.assertEqual(processes.members(entry), [])
        # Persisted stale PID state with no remaining members is harmless and
        # down is repeatable, without touching any other session.
        foreign, other = self.dummy()
        controller.save({'backend': entry})
        controller.down()
        controller.down()
        self.assertIsNone(other.poll())
        self.assertTrue(processes.members(foreign))

    def test_concurrent_up_lock_rejects_second_transition(self):
        marker = self.dir / 'entered'
        script = f'''import sys,time
from pathlib import Path
sys.path.insert(0, {str(config.ROOT / 'scripts/dev')!r})
import controller
controller.STATE=Path({str(self.dir)!r})
def held_up(scope, effective=None):
    Path({str(marker)!r}).write_text(scope)
    time.sleep(60)
controller.up=held_up
sys.argv=['controller.py','up','--scope','backend']
controller.main()
'''
        entry, first = self.dummy(script)
        eventually(marker.exists)
        began = time.monotonic()
        second = subprocess.run([sys.executable, '-c', script], cwd=config.ROOT,
                                env=dict(os.environ), text=True, capture_output=True, timeout=3)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('Another controller transition is in progress', second.stderr)
        self.assertLess(time.monotonic() - began, 2)
        self.assertIsNone(first.poll())
        self.assertTrue(processes.members(entry))

    def test_inventory_labels_config_health_fail_closed(self):
        self.require_pidfd()
        fake = EngineFixture(self)
        stack = infra.Infrastructure(config.light_values())
        valid = fake.templates['postgres']
        def mutate(path, value):
            item = json.loads(json.dumps(valid))
            cursor = item
            for key in path[:-1]:
                cursor = cursor[key]
            cursor[path[-1]] = value
            return item
        variants = [
            mutate(('Config', 'Labels', 'com.docker.compose.project'), 'foreign'),
            mutate(('Config', 'Labels', 'com.docker.compose.project.working_dir'), '/foreign'),
            mutate(('Config', 'Labels', 'com.docker.compose.project.config_files'), 'other.yaml'),
            mutate(('HostConfig', 'PortBindings', '5432/tcp'), [{'HostIp': '0.0.0.0', 'HostPort': '15433'}]),
            mutate(('Config', 'Env'), ['POSTGRES_USER=wrong']),
            mutate(('State', 'Status'), 'unknown'),
        ]
        for index, item in enumerate(variants):
            with self.subTest(variant=index):
                fake.seed({'postgres': item})
                with self.assertRaises((RuntimeError, ValueError)):
                    controller.up('backend')
        unhealthy = mutate(('State', 'Health', 'Status'), 'unhealthy')
        fake.seed({'postgres': unhealthy})
        with self.assertRaisesRegex(RuntimeError, 'Preexisting postgres is not healthy'):
            controller.up('backend')
        self.assertFalse(any('up' in c['argv'] or 'stop' in c['argv'] for c in fake.commands()))
        fake.assert_preserved_data()

    def test_stop_identity_change_does_not_signal_replacement(self):
        fake = EngineFixture(self)
        fake.seed(fake.templates)
        stack = infra.Infrastructure(config.light_values())
        before = stack.inventory()
        changed = json.loads(json.dumps(fake.templates))
        changed['postgres']['Id'] = 'f' * 64
        fake.seed(changed)
        stack.stop({'postgres': before['postgres']})
        self.assertFalse(any(c['argv'][0] == 'stop' for c in fake.commands()))
        self.assertTrue(stack.inventory()['postgres']['running'])
        fake.assert_preserved_data()
