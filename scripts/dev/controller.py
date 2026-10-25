#!/usr/bin/env python3
"""Conservative, additive lifecycle controller for isolated light development."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from shutil import which as find_executable
import signal
import socket
import subprocess
import sys
import time

from config import (ROOT, STATE, SERVICES, SCOPES, COMMANDS, light_values,
                    app_environment, app_port, app_compiler, command, compiler, dev_environment, parse_dev_file)
from infra import Infrastructure
import processes


def load():
    path = STATE / 'processes.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(name not in ('backend', 'workers', 'frontend') for name in data):
        raise RuntimeError('Corrupt process state; inspect manually')
    return data


def save(data, filename='processes.json'):
    tmp = STATE / f'{filename}.{os.getpid()}.tmp'
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(STATE / filename)


URL_IDENTITY_VERSION = 1
URL_IDENTITY_KEYS = ('MAIN_URL', 'FRONTEND_URL', 'NEXT_PUBLIC_BACKEND_URL',
                     'BACKEND_INTERNAL_URL', 'NEXT_PUBLIC_OVERRIDE_BACKEND_URL')


def _version_tuple(value, label):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value.strip())
    if not match:
        raise RuntimeError(f'Could not determine {label} version; install the required tool and retry')
    return tuple(map(int, match.groups()))


def validate_toolchain(env):
    """Check declared Node/pnpm requirements from PATH without bootstrap or network."""
    try:
        package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
        node_requirement = package['engines']['node']
        package_manager = package['packageManager']
        node_bin = find_executable('node', path=env.get('PATH'))
        pnpm_bin = find_executable('pnpm', path=env.get('PATH'))
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        raise RuntimeError('Cannot read the declared Node.js/pnpm requirements from package.json') from None
    if not node_bin:
        raise RuntimeError(f'Node.js is missing from PATH; package.json requires {node_requirement}; install/select it locally')
    if not pnpm_bin:
        raise RuntimeError(f'pnpm is missing from PATH; package.json requires {package_manager}; install/select it locally')
    try:
        node_result = subprocess.run([node_bin, '--version'], cwd=ROOT, env=env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
        node_version = _version_tuple(node_result.stdout, 'Node.js')
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        detail = 'version format' if isinstance(exc, RuntimeError) else 'PATH tool execution'
        raise RuntimeError(f'Could not verify Node.js version from PATH ({detail}); check the required tool and retry') from None
    # A PATH pnpm may be a Corepack shim. Force Corepack's documented controls
    # for this probe only so ambient opt-ins cannot trigger a download or pin edit.
    pnpm_env = {**env, 'COREPACK_ENABLE_NETWORK': '0',
                'COREPACK_ENABLE_DOWNLOAD_PROMPT': '0', 'COREPACK_ENABLE_AUTO_PIN': '0'}
    try:
        pnpm_result = subprocess.run([pnpm_bin, '--version'], cwd=ROOT, env=pnpm_env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(
            f'Could not verify pnpm from PATH; package.json requires {package_manager}. '
            'For Corepack, make this version available in its local cache first, or manually install/select '
            'the required pnpm, then retry; startup will not download or bootstrap it.'
        ) from None
    try:
        pnpm_version = _version_tuple(pnpm_result.stdout, 'pnpm')
    except RuntimeError:
        raise RuntimeError('Could not determine pnpm version from PATH; check the required tool and retry') from None
    node_range = re.fullmatch(r'>=(\d+)\.(\d+)\.(\d+)\s+<(\d+)\.0\.0', node_requirement)
    manager = re.fullmatch(r'pnpm@(\d+)\.(\d+)\.(\d+)', package_manager)
    if not node_range or not manager:
        raise RuntimeError('Unsupported Node.js/pnpm requirement format in package.json; update the startup check')
    lower = tuple(map(int, node_range.groups()[:3]))
    upper_major = int(node_range.group(4))
    expected_pnpm = tuple(map(int, manager.groups()))
    if not lower <= node_version < (upper_major, 0, 0):
        raise RuntimeError(f'Node.js from PATH is outside the supported range; package.json requires {node_requirement}')
    if pnpm_version != expected_pnpm:
        raise RuntimeError(f'pnpm from PATH is not the required version; package.json requires {package_manager}')
    if not (ROOT / 'node_modules').is_dir():
        raise RuntimeError('Workspace dependencies are missing; follow docs/development.md setup instructions first')


def fingerprint(env, selected=None):
    keys = ('DATABASE_URL', 'REDIS_URL', 'TEMPORAL_ADDRESS', 'TEMPORAL_NAMESPACE',
            'PORT', 'ORCHESTRATOR_PORT')
    values = [env.get(key) for key in keys]
    values.append([[key, env.get(key)] for key in URL_IDENTITY_KEYS])
    if selected is not None:
        values.append(selected)
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def check_running(name, entry, env):
    if entry.get('url_identity') != URL_IDENTITY_VERSION:
        raise RuntimeError(f'{name} live URL identity is unknown; use make down, then make up to restart safely')
    selected = app_compiler(name, env)
    if 'compiler' not in entry:
        # Old controller could only launch these exact original commands. Do not
        # infer tsc from missing/unknown argv or rewrite a live legacy record.
        if entry.get('command') != COMMANDS[name]:
            raise RuntimeError(f'{name} live compiler is unknown; use make down before restarting')
        actual = 'next' if name == 'frontend' else 'tsc'
        expected = fingerprint(env)
    else:
        actual = entry['compiler']
        expected = fingerprint(env, selected)
        if actual == selected and entry.get('command') != command(name, env):
            raise RuntimeError(f'{name} live command is ambiguous; use make down before restarting')
    if actual != selected:
        raise RuntimeError(f'{name} running compiler differs from DEV_COMPILER; use make down before changing compiler')
    if entry.get('destination') != expected:
        raise RuntimeError('Running app configuration differs; use make down before changing destinations/ports')


@contextmanager
def atomic_launch():
    # Defer external cancellation until the new PID has durable rollback state.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def own_poller(entry, pollers, now=None):
    live = processes.members(entry)
    identities = {f"{info['pid']}@{socket.gethostname()}": info for info in live}
    boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
    ticks = os.sysconf('SC_CLK_TCK')
    now = time.time() if now is None else now
    for poller in pollers:
        if not isinstance(poller, dict) or poller.get('taskQueueType') != 'workflow':
            continue
        info = identities.get(poller.get('identity'))
        if not info:
            continue
        try:
            accessed = datetime.fromisoformat(poller['lastAccessTime'].replace('Z', '+00:00')).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if boot + info['start'] / ticks - 1 <= accessed <= now + 5 and now - accessed <= 60:
            # A second scan avoids accepting an exited/reused worker PID.
            return any(all(current[key] == info[key] for key in ('pid', 'start', 'pgid', 'sid'))
                       for current in processes.members(entry))
    return False


def ready(name, entry, env, infra):
    path = {'backend': '/auth/can-register', 'workers': '/health/status', 'frontend': '/auth'}[name]
    if not processes.http_ready(entry, app_port(name, env), path):
        return False
    return name != 'workers' or own_poller(entry, infra.pollers())


def wait(predicate, label, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.5)
    raise RuntimeError(f'Timed out waiting for {label}; inspect make logs')


def rollback(infra, before, attempted, data, started):
    errors = []
    # Further terminal signals must not interrupt cleanup. Commands and signal
    # loops still have their own bounded deadlines.
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        for name in reversed(started):
            try:
                processes.terminate(data[name])
                data.pop(name)
                save(data)
            except BaseException as exc:
                errors.append(f'{name}: {exc}')
        try:
            current = infra.inventory()
            newly_running = {name: item for name, item in current.items() if name in attempted
                             and not (name in before and before[name]['running'])}
            infra.stop(newly_running)
        except BaseException as exc:
            errors.append(f'infrastructure: {exc}')
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    for error in errors:
        print(f'Rollback needs manual inspection: {error}', file=sys.stderr)


def up(scope, effective=None):
    effective = dev_environment() if effective is None else effective
    validate_toolchain(effective)
    values = light_values(effective)
    env = app_environment(values, effective)  # Must precede *any* engine command or launch.
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('Linux Python with pidfd signal support is required')
    data = load()
    for name, entry in list(data.items()):
        if processes.members(entry):
            check_running(name, entry, env)
        else:
            data.pop(name)
    # Preflight every requested app before touching infrastructure. Any existing
    # listener must be owned, even for an idempotent call.
    for name in SCOPES[scope]:
        port = app_port(name, env)
        if processes.listeners(port) and (name not in data or not processes.own_listener(data[name], port)):
            raise RuntimeError(f'{name} port {port} has a foreign listener; nothing was started')
    infra = Infrastructure(values)
    before = infra.inventory()  # Errors are never mistaken for an empty set.
    for name, item in before.items():
        if item['running'] and not item['healthy']:
            raise RuntimeError(f'Preexisting {name} is not healthy; leaving it unchanged')
    attempted = [name for name in SERVICES if name not in before or not before[name]['running']]
    started = []
    children = []
    try:
        # Save the selected engine/configuration for down, which must work even
        # without the explicit app environment used at startup.
        save({'engine': infra.engine, 'values': values}, 'infrastructure.json')
        infra.start(attempted)
        def infra_ready():
            current = infra.inventory()
            return all(current.get(name, {}).get('healthy') for name in SERVICES)

        wait(infra_ready, 'light service health', 180)
        for name in SCOPES[scope]:
            if name not in data:
                # Register before readiness: failure during compilation must roll
                # back this process too, not only apps that already became ready.
                with atomic_launch():
                    entry, child = processes.launch(name, env)
                    entry['compiler'] = app_compiler(name, env)
                    entry['destination'] = fingerprint(env, entry['compiler'])
                    entry['url_identity'] = URL_IDENTITY_VERSION
                    data[name] = entry
                    started.append(name)
                    children.append(child)
                    save(data)
            entry = data[name]

            def app_ready():
                if not processes.members(entry):
                    raise RuntimeError(f'{name} exited; inspect make logs')
                return ready(name, entry, env, infra)

            wait(app_ready, f'{name} readiness', 240)
        save(data)
        print(f'{scope} ready ({infra.engine}); apps run in background, logs: make logs')
    except BaseException:
        rollback(infra, before, attempted, data, started)
        raise
    finally:
        # Reap any exited immediate supervisors; live ones remain background.
        for child in children:
            child.poll()


def down():
    data = load()
    errors = []
    for name in ('frontend', 'workers', 'backend'):
        if name not in data:
            continue
        try:
            processes.terminate(data[name])
            data.pop(name)
            save(data)
        except BaseException as exc:
            errors.append(f'{name}: {exc}')
    path = STATE / 'infrastructure.json'
    if path.exists():
        recorded = json.loads(path.read_text())
        selected = os.environ.get('CONTAINER_ENGINE', 'docker')
        if recorded['engine'] != selected:
            errors.append('Engine differs from recorded startup; select the same CONTAINER_ENGINE for down')
        else:
            try:
                infra = Infrastructure(recorded['values'])
                infra.stop(infra.inventory())
            except BaseException as exc:
                errors.append(f'infrastructure: {exc}')
    if errors:
        raise RuntimeError('; '.join(errors))
    print('Stopped only verified owned apps/light containers; data and logs preserved')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('up', 'down', 'logs', 'status'))
    parser.add_argument('--scope', choices=SCOPES)
    parser.add_argument('--scope-env', action='store_true')
    args = parser.parse_args()
    scope = os.environ.get('SCOPE', 'full') if args.scope_env else args.scope or 'full'
    if scope not in SCOPES:
        parser.error('SCOPE must be full, backend or workers')
    env = dev_environment() if args.action == 'up' else os.environ
    if env.get('CONTAINER_ENGINE', 'docker') not in ('docker', 'podman'):
        parser.error('CONTAINER_ENGINE must be docker or podman')
    compiler(env)  # Validate selector for every entry point, before any engine call.
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / 'controller.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another controller transition is in progress') from None
        if args.action == 'up':
            up(scope, env)
        elif args.action == 'down':
            down()
        elif args.action == 'status':
            data = load()
            print(json.dumps({name: {'pid': entry.get('pid'), 'liveMembers': len(processes.members(entry))}
                              for name, entry in data.items()}, indent=2))
        else:
            for name in ('backend', 'workers', 'frontend'):
                path = STATE / f'{name}.log'
                print(f'===== {name} =====')
                if path.exists():
                    # Bounded tail without an external process or whole-log read.
                    with path.open('rb') as log:
                        log.seek(max(0, path.stat().st_size - 32768))
                        print(b'\n'.join(log.read().splitlines()[-80:]).decode(errors='replace'))
                else:
                    print('no log')


def interrupted(signum, _frame):
    raise InterruptedError(f'Interrupted by signal {signum}')


if __name__ == '__main__':
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'dev controller: {exc}', file=sys.stderr)
        sys.exit(1)
