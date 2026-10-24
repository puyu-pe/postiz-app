"""Linux process ownership and readiness primitives (no application changes).

Signals use pidfds, never a bare/reusable PID or killpg. Every live session
member must carry the invocation token and a valid start time. This also works
when the original pnpm/session leader has died and left watcher children.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from config import ROOT, STATE, COMMANDS, command, app_compiler


def proc(pid):
    try:
        raw = Path(f'/proc/{pid}/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        return {'pid': int(pid), 'state': fields[0], 'pgid': int(fields[2]),
                'sid': int(fields[3]), 'start': int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def members(entry):
    """Return verified live descendants, or refuse ambiguous session ownership."""
    if not isinstance(entry, dict) or not all(k in entry for k in ('pid', 'pgid', 'start', 'token', 'sid', 'root')):
        raise RuntimeError('Legacy/invalid process state; inspect manually (no signals sent)')
    if (entry['sid'] != entry['pid'] or entry['pgid'] != entry['pid'] or entry['root'] != str(ROOT)
            or not isinstance(entry['token'], str) or not entry['token'].startswith('postiz-light-')):
        raise RuntimeError('Invalid process identity; inspect manually')
    leader = proc(entry['pid'])
    if leader and (leader['start'] != entry['start'] or leader['sid'] != entry['sid'] or leader['pgid'] != entry['pgid']):
        raise RuntimeError('Recorded session leader identity changed/reused; refusing signals')
    found = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        info = proc(int(path.name))
        if not info or info['sid'] != entry['sid'] or info['state'] == 'Z':
            continue
        try:
            tagged = ('DEV_CONTROLLER_TOKEN=' + entry['token']).encode() in (path / 'environ').read_bytes().split(b'\0')
            cwd = Path(os.readlink(path / 'cwd'))
            valid = tagged and cwd.is_relative_to(ROOT) and info['start'] >= entry['start']
            # Re-read after environ/cwd: no accepting an identity across PID reuse.
            current = proc(info['pid'])
            if not current or current['state'] == 'Z':
                continue
            valid = valid and all(current[key] == info[key] for key in ('start', 'pgid', 'sid'))
        except (FileNotFoundError, ProcessLookupError):
            # procfs may report ENOENT or ESRCH when a task exits between stat
            # and environ/cwd reads. All other ownership errors fail closed.
            continue
        except OSError as exc:
            raise RuntimeError('Cannot verify session member ownership') from exc
        if not valid:
            raise RuntimeError('Ambiguous session member; refusing signals')
        found.append(info)
    return found


def signal_member(info, sig):
    """Pin the task before final identity check; a reused PID is never signaled."""
    try:
        fd = os.pidfd_open(info['pid'])
    except ProcessLookupError:
        return
    try:
        current = proc(info['pid'])
        if current and all(current[k] == info[k] for k in ('pid', 'start', 'pgid', 'sid')):
            signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def terminate(entry):
    # Re-scan for children forked during shutdown; session remains pinned by
    # extant members even if the leader exits. Never signal an unverified group.
    for sig, seconds in ((signal.SIGTERM, 10), (signal.SIGKILL, 4)):
        deadline = time.monotonic() + seconds
        while True:
            live = members(entry)
            if not live:
                return
            for info in live:
                signal_member(info, sig)
            if time.monotonic() >= deadline:
                break
            time.sleep(.1)
    if members(entry):
        raise RuntimeError('Owned processes did not stop within the deadline')


def launch(name, env):
    argv = command(name, env)  # Validate before creating even the supervisor.
    selected = app_compiler(name, env)
    token = 'postiz-light-' + uuid.uuid4().hex
    with (STATE / f'{name}.log').open('ab', buffering=0) as log:
        # A small persistent session leader prevents pnpm exit from orphaning a
        # running watcher unnoticed. No token is added to the application argv.
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--supervise', token, name],
            cwd=ROOT, env={**env, 'DEV_CONTROLLER_TOKEN': token}, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            # The controller defers interrupts while recording this launch, but
            # the exec'd supervisor must not inherit that blocked signal mask.
            preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, []),
        )
    info = proc(child.pid)
    if not info or info['sid'] != child.pid or info['pgid'] != child.pid:
        raise RuntimeError(f'Could not establish {name} session identity')
    return {'pid': child.pid, 'pgid': child.pid, 'sid': child.pid, 'start': info['start'],
            'root': str(ROOT), 'token': token, 'command': argv, 'compiler': selected}, child


def listeners(port):
    inodes = set()
    for table in ('tcp', 'tcp6'):
        for line in Path('/proc/net', table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].split(':')[1], 16) == port:
                inodes.add(fields[9])
    return inodes


def own_listener(entry, port):
    expected = listeners(port)
    if not expected:
        return False
    actual = set()
    for info in members(entry):
        sockets = set()
        try:
            for fd in Path(f"/proc/{info['pid']}/fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if target.startswith('socket:['):
                    sockets.add(target[8:-1])
        except FileNotFoundError:
            continue
        current = proc(info['pid'])
        if current and all(current[key] == info[key] for key in ('start', 'sid', 'pgid')):
            actual.update(sockets)
    return expected <= actual


def http_ready(entry, port, path):
    if not own_listener(entry, port):
        return False
    # Direct HTTP avoids proxy env, redirects and URL fallthrough. Next's /auth
    # may legitimately redirect; backend/health endpoints must return 2xx.
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2) as sock:
            sock.sendall(f'GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n'.encode())
            status = int(sock.recv(256).split(b' ', 2)[1])
        return 200 <= status < (400 if path == '/auth' else 300) and own_listener(entry, port)
    except (OSError, ValueError, IndexError):
        return False


def supervise(name):
    """Stay alive across watch-child exits; bound and verify cleanup on signals."""
    info = proc(os.getpid())
    entry = {'pid': os.getpid(), 'pgid': os.getpid(), 'sid': os.getpid(), 'start': info['start'],
             'root': str(ROOT), 'token': os.environ['DEV_CONTROLLER_TOKEN']}
    stopping = False

    def stop(_sig, _frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    child = subprocess.Popen(command(name, os.environ), cwd=ROOT)
    try:
        while not stopping:
            # Do not reap the session leader (this process) until its children
            # have stopped. Watchers can survive a pnpm parent exit.
            if child.poll() is not None:
                break
            time.sleep(.2)
    finally:
        for sig, seconds in ((signal.SIGTERM, 8), (signal.SIGKILL, 3)):
            end = time.monotonic() + seconds
            while True:
                live = [item for item in members(entry) if item['pid'] != os.getpid()]
                if not live:
                    return
                for item in live:
                    signal_member(item, sig)
                if time.monotonic() >= end:
                    break
                time.sleep(.1)
        child.poll()


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--supervise' and sys.argv[3] in COMMANDS:
        supervise(sys.argv[3])
    else:
        raise SystemExit('Internal supervisor entry point only')
