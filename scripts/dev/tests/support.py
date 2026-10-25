"""Test-only process/Make fixtures; never load checkout dotenv or controller state."""
import ctypes
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import controller
import infra
import processes

RUN = config.STATE / 'tests' / uuid.uuid4().hex
RUN.mkdir(mode=0o700, parents=True)
BASE_ENV = {
    'PATH': os.defpath, 'HOME': str(RUN), 'PYTHONDONTWRITEBYTECODE': '1',
    'DATABASE_URL': 'postgresql://postiz_light:postiz-light-local-only@127.0.0.1:15433/postiz_light',
    'REDIS_URL': 'redis://127.0.0.1:6380/0', 'TEMPORAL_ADDRESS': '127.0.0.1:7234',
    'TEMPORAL_NAMESPACE': 'default',
}
MAKE_INPUTS = (
    'CONTAINER_ENGINE', 'SCOPE', 'DEV_COMPILER', 'LIGHT_POSTGRES_USER', 'LIGHT_POSTGRES_PASSWORD',
    'LIGHT_POSTGRES_DB', 'LIGHT_POSTGRES_PORT', 'LIGHT_REDIS_PORT',
    'LIGHT_TEMPORAL_PORT', 'LIGHT_TEMPORAL_NAMESPACE', 'DATABASE_URL', 'REDIS_URL',
    'TEMPORAL_ADDRESS', 'TEMPORAL_NAMESPACE', 'TEMPORAL_TLS', 'TEMPORAL_API_KEY',
    'PORT', 'ORCHESTRATOR_PORT',
)
APP_MAKE_INPUTS = (
    'FRONTEND_URL', 'NEXT_PUBLIC_BACKEND_URL', 'BACKEND_INTERNAL_URL', 'MAIN_URL',
    'NEXT_PUBLIC_OVERRIDE_BACKEND_URL', 'STORAGE_PROVIDER', 'UPLOAD_DIRECTORY',
    'NEXT_PUBLIC_UPLOAD_STATIC_DIRECTORY', 'JWT_SECRET', 'NOT_SECURED',
)


def eventually(predicate, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Dummy fixture did not reach expected state within deadline')


BASE_SWAP_KIB = None


def resources():
    global BASE_SWAP_KIB
    memory = {line.split(':')[0]: int(line.split()[1])
              for line in Path('/proc/meminfo').read_text().splitlines()}
    used_swap = memory['SwapTotal'] - memory['SwapFree']
    if BASE_SWAP_KIB is None:
        BASE_SWAP_KIB = used_swap
    snapshot = {'availableKiB': memory['MemAvailable'], 'swapGrowthKiB': used_swap - BASE_SWAP_KIB}
    reason = ('MemAvailable below 3 GiB' if snapshot['availableKiB'] < 3 * 1024 * 1024 else
              'swap growth exceeds 256 MiB' if snapshot['swapGrowthKiB'] > 256 * 1024 else '')
    return snapshot, reason


class ControllerCase(unittest.TestCase):
    def setUp(self):
        _, blocker = resources()
        self.assertFalse(blocker, 'Resource safety stop: ' + blocker)
        self.dir = RUN / self._testMethodName
        self.dir.mkdir(mode=0o700)
        self.children = []
        self.env = patch.dict(os.environ, BASE_ENV, clear=True)
        self.env.start()
        # No binding/probing of the real application's fixed ports.
        with ExitStack() as stack:
            sockets = [stack.enter_context(socket.socket()) for _ in range(2)]
            for sock in sockets:
                sock.bind(('127.0.0.1', 0))
            ports = [str(sock.getsockname()[1]) for sock in sockets]
        os.environ.update(PORT=ports[0], ORCHESTRATOR_PORT=ports[1])
        self.addCleanup(self.env.stop)
        for module in (controller, infra, processes):
            patcher = patch.object(module, 'STATE', self.dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.toolchain_patch = patch.object(controller, 'validate_toolchain', return_value=None)
        self.toolchain_patch.start()
        self.addCleanup(self.toolchain_patch.stop)
        self.real_launch = processes.launch
        # Unexpected launch/engine access must fail rather than touch real apps.
        for target in ('processes.launch', 'infra.shutil.which'):
            guard = patch(target, side_effect=AssertionError('Unconfigured real app/engine access'))
            guard.start()
            self.addCleanup(guard.stop)
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.previous_subreaper = ctypes.c_int()
        if self.libc.prctl(37, ctypes.byref(self.previous_subreaper), 0, 0, 0) != 0:
            self.fail('Linux PR_GET_CHILD_SUBREAPER unavailable')
        if self.libc.prctl(36, 1, 0, 0, 0) != 0:
            self.fail('Cannot enable test-only child subreaper')
        self.addCleanup(lambda: self.libc.prctl(36, self.previous_subreaper.value, 0, 0, 0))
        self.addCleanup(self.cleanup_children)

    def require_pidfd(self):
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            self.skipTest('Linux Python pidfd support unavailable; ownership signaling not exercised')
        try:
            fd = os.pidfd_open(os.getpid())
        except OSError as exc:
            self.skipTest(f'Kernel pidfd unavailable: {exc}')
        else:
            os.close(fd)

    def cleanup_children(self):
        errors = []
        evidence = []
        for entry, child in reversed(self.children):
            try:
                processes.terminate(entry)
                child.wait(timeout=4)
                remaining = processes.members(entry)
                evidence.append({'pid': child.pid, 'start': entry['start'],
                                 'reaped': child.returncode is not None, 'remainingMembers': len(remaining)})
                self.assertFalse(remaining, 'Owned test session leaked')
            except BaseException as exc:
                errors.append(str(exc))
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break
        (self.dir / 'process-cleanup.json').write_text(json.dumps({'sessions': evidence, 'errors': errors}, indent=2) + '\n')
        self.assertEqual(errors, [], 'Test process cleanup failed')

    def dummy(self, code='import time; time.sleep(60)'):
        self.require_pidfd()
        token = 'postiz-light-test-' + uuid.uuid4().hex
        with (self.dir / (token + '.log')).open('ab') as log:
            child = subprocess.Popen([sys.executable, '-c', code], cwd=config.ROOT,
                                     env={**os.environ, 'DEV_CONTROLLER_TOKEN': token},
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True,
                                     preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, []))
        info = processes.proc(child.pid)
        entry = {'pid': child.pid, 'pgid': child.pid, 'sid': child.pid, 'start': info['start'],
                 'root': str(config.ROOT), 'token': token}
        self.children.append((entry, child))
        return entry, child

    def make_fixture(self):
        # Exercise a private .make.local, never replace the user's ignored file.
        makefile = self.dir / 'Makefile'
        makefile.write_text((config.ROOT / 'Makefile').read_text().replace(
            'python3 scripts/dev/controller.py', 'python3 scripts/dev/controller.py') + '''
.PHONY: probe-config
probe-config:
\t@python3 -c 'import json,os,sys; print(json.dumps({k: os.environ.get(k) for k in sys.argv[1:]}))' ''' + ' '.join(MAKE_INPUTS + APP_MAKE_INPUTS) + '''
.PHONY: probe-private
probe-private:
\t@python3 scripts/dev/controller.py --probe-dev-env
''')
        fake_bin = self.dir / 'fake-bin'
        fake_bin.mkdir()
        for name, version in (('node', 'v22.12.0'), ('pnpm', '10.6.1')):
            executable = fake_bin / name
            executable.write_text(f'#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ["--version"]\nprint({version!r})\n')
            executable.chmod(0o700)
        (self.dir / 'package.json').write_text((config.ROOT / 'package.json').read_text())
        (self.dir / 'node_modules').mkdir()
        script = self.dir / 'scripts/dev/controller.py'
        script.parent.mkdir(parents=True)
        # Real argument/config validation, with a hard guard against any engine
        # or app invocation even if a regression accepts an invalid input.
        script.write_text(f'''import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(config.ROOT / 'scripts/dev')!r})
import config, controller
config.ROOT = Path({str(self.dir)!r})
config.DEV_FILE = Path({str(self.dir / '.dev.env.local')!r})
config.dev_environment.__defaults__ = (None, config.DEV_FILE)
controller.STATE = Path({str(self.dir / 'state')!r})
def forbidden(*args, **kwargs):
    raise AssertionError('TEST: engine or app launch reached')
controller.Infrastructure = forbidden
controller.processes.launch = forbidden
if os.environ.get('POSTIZ_TEST_MOCK_LISTENERS') == '1':
    controller.processes.listeners = lambda port: []
if sys.argv[1:] == ['--probe-dev-env']:
    print(json.dumps({{key: controller.dev_environment().get(key) for key in {(APP_MAKE_INPUTS + ('PORT',))!r}}}))
    raise SystemExit(0)
try:
    controller.main()
except Exception as exc:
    print('dev controller:', exc, file=sys.stderr)
    sys.exit(1)
''')
    def fixture_make(self, target, assignments=(), env=None, mock_listeners=False):
        child_env = dict(os.environ) if env is None else dict(env)
        child_env['PATH'] = str(self.dir / 'fake-bin') + os.pathsep + child_env.get('PATH', '')
        if mock_listeners:
            child_env['POSTIZ_TEST_MOCK_LISTENERS'] = '1'
        else:
            child_env.pop('POSTIZ_TEST_MOCK_LISTENERS', None)
        return subprocess.run(['make', '--no-print-directory', target, *assignments],
                              cwd=self.dir, env=child_env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)


class EngineFixture:
    """Allowlisted executables only; Infrastructure still builds/runs its real argv."""
    def __init__(self, case, engine='docker', provider=False):
        self.case = case
        self.engine = engine
        self.directory = case.dir / ('engine-' + engine)
        self.directory.mkdir()
        self.inventory_path = self.directory / 'inventory.json'
        self.inventory_path.write_text('{}')
        (self.directory / 'pollers.json').write_text('{"pollers": []}')
        self.templates = {}
        values = config.light_values()
        for index, (name, container, host) in enumerate((
            ('postgres', '5432/tcp', 'LIGHT_POSTGRES_PORT'),
            ('redis', '6379/tcp', 'LIGHT_REDIS_PORT'),
            ('temporal', '7233/tcp', 'LIGHT_TEMPORAL_PORT'),
        )):
            labels = {'com.docker.compose.project': config.PROJECT,
                      'com.docker.compose.service': name,
                      'com.docker.compose.project.working_dir': str(config.ROOT),
                      'com.docker.compose.project.config_files': str(config.ROOT / 'docker-compose.light.yaml')}
            self.templates[name] = {
                'Id': str(index + 1) * 64,
                'Config': {'Labels': labels, 'Env': [f'POSTGRES_{key}={values["LIGHT_POSTGRES_" + key]}'
                                                   for key in ('USER', 'PASSWORD', 'DB')],
                           'Cmd': ['server', 'start-dev', '--namespace', values['LIGHT_TEMPORAL_NAMESPACE']]},
                'State': {'Status': 'running', 'Running': True, 'Health': {'Status': 'healthy'}},
                'HostConfig': {'PortBindings': {container: [{'HostIp': '127.0.0.1', 'HostPort': values[host]}]}},
            }
        (self.directory / 'templates.json').write_text(json.dumps(self.templates))
        self.executables = {}
        names = [engine] + (['podman-compose'] if provider else [])
        for name in names:
            path = self.directory / name
            path.write_text(f'#!{sys.executable}\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, {str(Path(__file__).parent)!r})\nfrom engine_fixture import main\nmain(Path(__file__).parent, {name!r})\n')
            path.chmod(0o700)
            self.executables[name] = str(path)
        which = patch('infra.shutil.which', side_effect=self.executables.get)
        which.start()
        case.addCleanup(which.stop)
        selection = patch.dict(os.environ, {'CONTAINER_ENGINE': engine})
        selection.start()
        case.addCleanup(selection.stop)

    def seed(self, items):
        self.inventory_path.write_text(json.dumps(items))

    def commands(self):
        path = self.directory / 'commands.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_preserved_data(self):
        for command in self.commands():
            self.case.assertFalse(set(command['argv']) & {'rm', '--rm', 'down', '-v', '--volumes', 'prune', 'volume'})
