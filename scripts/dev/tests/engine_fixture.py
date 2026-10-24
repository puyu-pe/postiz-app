"""Strict, disk-backed Docker/Podman CLI simulator. No sockets or real engines."""
import json
from pathlib import Path
import sys


def main(directory, engine):
    directory = Path(directory)
    args = sys.argv[1:]
    log = directory / 'commands.jsonl'
    with log.open('a') as stream:
        stream.write(json.dumps({'engine': engine, 'argv': args}) + '\n')
    # Fail loudly on any destructive command, including an unexpected future one.
    if any(word in args for word in ('rm', '--rm', 'down', '-v', '--volumes', 'prune', 'volume')):
        raise SystemExit('Destructive fixture command forbidden')
    path = directory / 'inventory.json'
    items = json.loads(path.read_text())
    operation = args[0] if args else ''
    if operation == 'compose':
        args = args[1:]
        operation = 'compose'
    if engine == 'podman-compose' or operation == 'compose':
        if args[:1] != ['--env-file'] or 'up' not in args:
            raise SystemExit('Unexpected Compose invocation')
        index = args.index('up')
        if args[index:index + 3] != ['up', '-d', '--no-recreate']:
            raise SystemExit('Unsafe Compose startup')
        if not Path(args[1]).is_file():
            raise SystemExit('Missing private env file')
        template = json.loads((directory / 'templates.json').read_text())
        for name in args[index + 3:]:
            if name not in template:
                raise SystemExit('Unknown service')
            item = items.setdefault(name, template[name])
            item['State'] = {'Status': 'running', 'Running': True, 'Health': {'Status': 'healthy'}}
        path.write_text(json.dumps(items))
        print('Started fixture containers only')
    elif operation == 'ps':
        if args[1:] != ['-a', '--filter', 'label=com.docker.compose.project=postiz-light', '--format', 'json']:
            raise SystemExit('Unexpected inventory command')
        listed = []
        for name, item in items.items():
            labels = item['Config']['Labels']
            listed.append({'ID': item['Id'][:12], 'Service': name, 'Labels': labels,
                           'State': item['State']['Status'], 'Health': item['State']['Health']['Status']})
        if engine == 'docker':
            for item in listed:
                item['Labels'] = ','.join(f'{k}={v}' for k, v in item['Labels'].items())
                print(json.dumps(item))
        else:
            print(json.dumps(listed, indent=2))
    elif operation == 'inspect':
        selected = [item for item in items.values() if any(item['Id'].startswith(cid) for cid in args[1:])]
        if len(selected) != len(args[1:]):
            raise SystemExit('Unknown container identity')
        print(json.dumps(selected, indent=2))
    elif operation == 'stop':
        if args[1:3] != ['--time', '10']:
            raise SystemExit('Unbounded stop')
        for cid in args[3:]:
            matching = [item for item in items.values() if item['Id'] == cid]
            if len(matching) != 1:
                raise SystemExit('Stop requires exact fixture ID')
            matching[0]['State'] = {'Status': 'exited', 'Running': False, 'Health': {'Status': 'healthy'}}
        path.write_text(json.dumps(items))
    elif operation == 'exec':
        if args[2:5] != ['temporal', 'task-queue', 'describe']:
            raise SystemExit('Unexpected exec')
        print((directory / 'pollers.json').read_text())
    else:
        raise SystemExit('Unexpected fixture command')
