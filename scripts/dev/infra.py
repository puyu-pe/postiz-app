"""Bounded engine/Compose operations and fail-closed inventory normalization."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from config import ROOT, STATE, PROJECT, SERVICES
import processes


def execute(argv, env=None, timeout=15):
    """A command gets its own session; timeouts/interruption kill its helpers too."""
    token = 'postiz-light-command-' + uuid.uuid4().hex
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    child = entry = None
    try:
        child = subprocess.Popen(argv, cwd=ROOT,
                                 env={**(os.environ if env is None else env), 'DEV_CONTROLLER_TOKEN': token},
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True,
                                 preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, []))
        info = processes.proc(child.pid)
        entry = {'pid': child.pid, 'pgid': child.pid, 'sid': child.pid, 'start': info['start'],
                 'token': token, 'root': str(ROOT)}
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        stdout, stderr = child.communicate(timeout=timeout)
    except BaseException:
        # The same PID-safe session proof handles Compose helpers even after
        # the command leader exits. No unverified/reused process group signal.
        if entry:
            try:
                processes.terminate(entry)
            except BaseException as exc:
                print(f'Command cleanup needs inspection: {exc}', file=sys.stderr)
        if child:
            try:
                child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    if child.returncode:
        # Avoid emitting engine env/config or credentials in error messages.
        raise RuntimeError(f'External command {Path(argv[0]).name} failed (exit {child.returncode})')
    return stdout


def records(text):
    if not text.strip():
        raise RuntimeError('Empty inventory output (expected JSON, possibly [])')
    try:
        obj = json.loads(text)
        result = obj if isinstance(obj, list) else [obj]
    except ValueError:
        try:
            result = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            raise RuntimeError('Invalid inventory JSON') from None
    if not all(isinstance(item, dict) for item in result):
        raise RuntimeError('Invalid inventory records')
    return result


def labels_of(item):
    labels = item.get('Labels')
    if labels is None:
        labels = item.get('Config', {}).get('Labels')
    if isinstance(labels, str):
        pairs = labels.split(',')
        if any('=' not in part for part in pairs):
            raise RuntimeError('Malformed inventory labels')
        labels = dict(part.split('=', 1) for part in pairs)
    if not isinstance(labels, dict):
        raise RuntimeError('Missing inventory labels')
    return labels


def normalize(text):
    """Docker JSONL and Podman pretty arrays, including inspect health shapes."""
    result = {}
    for item in records(text):
        labels = labels_of(item)
        project = labels.get('com.docker.compose.project') or labels.get('io.podman.compose.project')
        service = item.get('Service') or labels.get('com.docker.compose.service') or labels.get('io.podman.compose.service')
        cid = item.get('ID') or item.get('Id')
        if (project != PROJECT or service not in SERVICES or not isinstance(cid, str)
                or not re.fullmatch(r'[a-fA-F0-9]{12,64}', cid) or service in result):
            raise RuntimeError('Ambiguous or foreign light-stack inventory')
        raw_state = item.get('State')
        if isinstance(raw_state, dict):
            status = str(raw_state.get('Status', '')).lower()
            running = raw_state.get('Running') is True and status == 'running'
            health = raw_state.get('Health') or raw_state.get('Healthcheck') or {}
            health = health.get('Status', '') if isinstance(health, dict) else health
        elif isinstance(raw_state, str):
            status = raw_state.lower()
            running = status == 'running'
            health = item.get('Health', '')
            if not health:
                match = re.search(r'\((healthy|unhealthy|health: starting)\)', str(item.get('Status', '')))
                health = match.group(1) if match else ''
        else:
            raise RuntimeError('Missing container runtime state')
        if status not in ('running', 'exited', 'created', 'dead', 'paused', 'restarting', 'stopped', 'configured'):
            raise RuntimeError('Unknown container runtime state')
        result[service] = {'id': cid, 'running': running,
                           'healthy': running and str(health).lower() == 'healthy',
                           'labels': labels, 'raw': item}
    return result


class Infrastructure:
    def __init__(self, values):
        self.values = values
        self.engine = os.environ.get('CONTAINER_ENGINE', 'docker')
        if self.engine not in ('docker', 'podman'):
            raise RuntimeError('CONTAINER_ENGINE must be docker or podman')
        self.executable = shutil.which(self.engine)
        if not self.executable:
            raise RuntimeError(f'{self.engine} executable unavailable')
        if self.engine == 'docker':
            self.compose = [self.executable, 'compose']
        else:
            provider = shutil.which('podman-compose')
            self.compose = [provider] if provider else [self.executable, 'compose']
        # Do not leak unvalidated COMPOSE_*, DOCKER_HOST, CONTAINER_HOST, etc.
        # Only a local engine socket and the explicitly selected executable.
        self.env = {key: os.environ[key] for key in (
            'PATH', 'HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if key in os.environ}
        self.env.update(values)
        self.env['COMPOSE_DISABLE_ENV_FILE'] = 'true'
        self.env['DOCKER_CONTEXT'] = 'default'
        # Credentials/interpolation are disk-backed and private, never .env.
        path = STATE / 'compose.env'
        path.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        path.chmod(0o600)
        self.prefix = self.compose + ['--env-file', str(path), '-p', PROJECT,
                                      '-f', str(ROOT / 'docker-compose.light.yaml')]

    def command(self, *args, timeout=15):
        return execute([self.executable, *args], self.env, timeout)

    def inventory(self):
        output = self.command('ps', '-a', '--filter', f'label=com.docker.compose.project={PROJECT}', '--format', 'json')
        # Docker emits zero JSONL records (empty stdout) when no container
        # matches. This is valid only after a successful ps command, never as
        # recovery from a command/JSON error. Podman normally emits [].
        listed = normalize(output if output.strip() else '[]')
        if not listed:
            return {}
        inspected = normalize(self.command('inspect', *(item['id'] for item in listed.values())))
        if set(inspected) != set(listed):
            raise RuntimeError('Inventory changed during inspection')
        for name, item in inspected.items():
            if not item['id'].startswith(listed[name]['id']):
                raise RuntimeError('Container identity changed during inspection')
            self.validate(name, item)
        return inspected

    def validate(self, name, item):
        labels, raw = item['labels'], item['raw']
        workdir = labels.get('com.docker.compose.project.working_dir')
        files = labels.get('com.docker.compose.project.config_files', '').split(',')
        if workdir != str(ROOT) or len(files) != 1 or (Path(workdir) / files[0]).resolve() != ROOT / 'docker-compose.light.yaml':
            raise RuntimeError('Light project label belongs to another checkout/configuration')
        container_port, host_key = {
            'postgres': ('5432/tcp', 'LIGHT_POSTGRES_PORT'),
            'redis': ('6379/tcp', 'LIGHT_REDIS_PORT'),
            'temporal': ('7233/tcp', 'LIGHT_TEMPORAL_PORT'),
        }[name]
        # HostConfig remains available for stopped Docker containers too.
        bindings = raw.get('HostConfig', {}).get('PortBindings', {}).get(container_port)
        if not bindings:
            bindings = raw.get('NetworkSettings', {}).get('Ports', {}).get(container_port)
        if bindings != [{'HostIp': '127.0.0.1', 'HostPort': self.values[host_key]}]:
            raise RuntimeError(f'{name} published port does not match light configuration')
        config = raw.get('Config', {})
        if name == 'postgres':
            env = dict(value.split('=', 1) for value in config.get('Env', []) if '=' in value)
            for key in ('USER', 'PASSWORD', 'DB'):
                if env.get('POSTGRES_' + key) != self.values['LIGHT_POSTGRES_' + key]:
                    raise RuntimeError('Existing light PostgreSQL configuration differs; inspect manually')
        if name == 'temporal':
            cmd = config.get('Cmd') or []
            try:
                namespace = cmd[cmd.index('--namespace') + 1]
            except (ValueError, IndexError):
                raise RuntimeError('Cannot verify Temporal namespace') from None
            if namespace != self.values['LIGHT_TEMPORAL_NAMESPACE']:
                raise RuntimeError('Existing light Temporal namespace differs')

    def start(self, names):
        if names:
            execute(self.prefix + ['up', '-d', '--no-recreate', *names], self.env, 180)

    def stop(self, items):
        # IDs, not service names: cannot stop a replacement or foreign project.
        current = self.inventory()
        ids = [item['id'] for name, item in items.items()
               if name in current and current[name]['id'] == item['id'] and current[name]['running']]
        if ids:
            self.command('stop', '--time', '10', *ids, timeout=35)

    def pollers(self):
        current = self.inventory()
        temporal = current.get('temporal')
        if not temporal or not temporal['healthy']:
            return []
        text = self.command('exec', temporal['id'], 'temporal', 'task-queue', 'describe',
                            '--address', '127.0.0.1:7233', '--namespace', self.values['LIGHT_TEMPORAL_NAMESPACE'],
                            '--task-queue', 'main', '--task-queue-type', 'workflow',
                            '--select-unversioned', '--disable-stats', '--output', 'json', timeout=12)
        obj = json.loads(text)
        # CLI 1.9 emits describe-task-queue response with lowerCamelCase keys.
        pollers = obj.get('pollers') if isinstance(obj, dict) else None
        if isinstance(obj, dict) and 'pollers' in obj and pollers is None:
            return []  # CLI 1.9 encodes a nil (empty) poller slice as null.
        if not isinstance(pollers, list):
            raise RuntimeError('Temporal CLI returned no recognizable poller inventory')
        return pollers
