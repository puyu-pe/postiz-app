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
import signal
import socket
import sys
import time

from config import (ROOT, STATE, SERVICES, SCOPES, COMMANDS, light_values,
                    app_environment, app_port, app_compiler, command, compiler)
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


def fingerprint(env, selected=None):
    keys = ('DATABASE_URL', 'REDIS_URL', 'TEMPORAL_ADDRESS', 'TEMPORAL_NAMESPACE',
            'PORT', 'ORCHESTRATOR_PORT')
    values = [env[key] for key in keys]
    # None retains the pre-selector format solely for conservative legacy reuse.
    if selected is not None:
        values.append(selected)
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def check_running(name, entry, env):
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


def up(scope):
    values = light_values()
    env = app_environment(values)  # Must precede *any* engine command or launch.
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
    if os.environ.get('CONTAINER_ENGINE', 'docker') not in ('docker', 'podman'):
        parser.error('CONTAINER_ENGINE must be docker or podman')
    compiler()  # Validate selector for every entry point, before any engine call.
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.chmod(0o700)
    with (STATE / 'controller.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another controller transition is in progress') from None
        if args.action == 'up':
            up(scope)
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
