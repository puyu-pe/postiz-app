"""Validated light-stack configuration; never read the repository dotenv file."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / '.dev-state'
PROJECT = 'postiz-light'
SERVICES = ('postgres', 'redis', 'temporal')
SCOPES = {
    'backend': ('backend',),
    'workers': ('backend', 'workers'),
    'full': ('backend', 'workers', 'frontend'),
}
COMMANDS = {name: ['pnpm', 'run', script] for name, script in (
    ('backend', 'dev:backend'), ('workers', 'dev:orchestrator'),
    ('frontend', 'dev:frontend'),
)}


def compiler(env=None):
    selected = (os.environ if env is None else env).get('DEV_COMPILER', 'tsc')
    if selected not in ('tsc', 'swc'):
        raise RuntimeError('DEV_COMPILER must be tsc or swc')
    return selected


def app_compiler(name, env):
    selected = compiler(env)
    return 'next' if name == 'frontend' else selected


def command(name, env):
    selected = compiler(env)
    argv = COMMANDS[name]
    return list(argv) if name == 'frontend' or selected == 'tsc' else [*argv[:-1], argv[-1] + ':swc']


def port(value, label):
    if not re.fullmatch(r'[0-9]+', value) or not 1 <= int(value) <= 65535:
        raise RuntimeError(f'{label} must be a TCP port from 1 to 65535')
    return int(value)


def light_values():
    defaults = {
        'LIGHT_POSTGRES_USER': 'postiz_light',
        'LIGHT_POSTGRES_PASSWORD': 'postiz-light-local-only',
        'LIGHT_POSTGRES_DB': 'postiz_light',
        'LIGHT_POSTGRES_PORT': '15433', 'LIGHT_REDIS_PORT': '6380',
        'LIGHT_TEMPORAL_PORT': '7234', 'LIGHT_TEMPORAL_NAMESPACE': 'default',
    }
    values = {key: os.environ.get(key, value) for key, value in defaults.items()}
    values['LIGHT_COMPOSE_PROJECT'] = PROJECT
    # These values enter Compose's env-file. No interpolation or line injection.
    for key, value in values.items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', value):
            raise RuntimeError(f'{key} must contain only letters, digits, _, . or -')
    ports = [port(values[key], key) for key in (
        'LIGHT_POSTGRES_PORT', 'LIGHT_REDIS_PORT', 'LIGHT_TEMPORAL_PORT')]
    if len(set(ports)) != len(ports):
        raise RuntimeError('Light service ports must be distinct')
    return values


def app_environment(values):
    """Export every datastore selector, overriding dotenv's non-override loading.

    Reject query-based PostgreSQL host/socket overrides, Redis sentinels and
    dotenv-expand metacharacters. Error messages intentionally omit values.
    """
    env = dict(os.environ)
    env['DEV_COMPILER'] = compiler(env)
    for key in ('DATABASE_URL', 'REDIS_URL', 'TEMPORAL_ADDRESS', 'TEMPORAL_NAMESPACE'):
        if not env.get(key) or any(c in env[key] for c in '$\\\r\n'):
            raise RuntimeError(f'{key} must be explicitly exported without expansion characters')
    try:
        db = urlsplit(env['DATABASE_URL'])
        valid_db = (
            db.scheme in ('postgres', 'postgresql') and db.hostname == '127.0.0.1'
            and db.port == int(values['LIGHT_POSTGRES_PORT'])
            and unquote(db.username or '') == values['LIGHT_POSTGRES_USER']
            and unquote(db.password or '') == values['LIGHT_POSTGRES_PASSWORD']
            and unquote(db.path) == '/' + values['LIGHT_POSTGRES_DB']
            and db.query in ('', 'schema=public') and not db.fragment
        )
        redis = urlsplit(env['REDIS_URL'])
        valid_redis = (
            redis.scheme == 'redis' and redis.hostname == '127.0.0.1'
            and redis.port == int(values['LIGHT_REDIS_PORT'])
            and redis.username is None and redis.password is None
            and redis.path in ('', '/', '/0') and not redis.query and not redis.fragment
        )
    except ValueError:
        raise RuntimeError('Malformed database or Redis URL') from None
    if not valid_db:
        raise RuntimeError('DATABASE_URL must match the light PostgreSQL host, port, user, password and database (no destination overrides)')
    if not valid_redis:
        raise RuntimeError('REDIS_URL must match unauthenticated light Redis, database 0 (no overrides)')
    if env['TEMPORAL_ADDRESS'] != '127.0.0.1:' + values['LIGHT_TEMPORAL_PORT']:
        raise RuntimeError('TEMPORAL_ADDRESS must match light Temporal')
    if env['TEMPORAL_NAMESPACE'] != values['LIGHT_TEMPORAL_NAMESPACE']:
        raise RuntimeError('TEMPORAL_NAMESPACE must match LIGHT_TEMPORAL_NAMESPACE')
    if env.get('TEMPORAL_TLS', 'false') != 'false' or env.get('TEMPORAL_API_KEY', ''):
        raise RuntimeError('Light Temporal requires TEMPORAL_TLS=false and no API key')
    # An explicitly empty value also prevents dotenv fallback to remote credentials.
    env.update(TEMPORAL_TLS='false', TEMPORAL_API_KEY='')
    env['PORT'] = str(port(env.get('PORT', '3000'), 'PORT'))
    env['ORCHESTRATOR_PORT'] = str(port(env.get('ORCHESTRATOR_PORT', '3002'), 'ORCHESTRATOR_PORT'))
    # The existing frontend script explicitly fixes 4200; do not pretend it is configurable.
    app_ports = [int(env['PORT']), int(env['ORCHESTRATOR_PORT']), 4200]
    infra_ports = [int(values[key]) for key in ('LIGHT_POSTGRES_PORT', 'LIGHT_REDIS_PORT', 'LIGHT_TEMPORAL_PORT')]
    if len(set(app_ports + infra_ports)) != 6:
        raise RuntimeError('Application and light infrastructure ports must be distinct')
    return env


def app_port(name, env):
    return {'backend': int(env['PORT']), 'workers': int(env['ORCHESTRATOR_PORT']), 'frontend': 4200}[name]
