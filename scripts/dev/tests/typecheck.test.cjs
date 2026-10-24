// Command contracts only; real small semantic probes live in swc.cjs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const ts = require('typescript');

const root = path.resolve(__dirname, '../../..');
const scripts = JSON.parse(fs.readFileSync(path.join(root, 'package.json'))).scripts;
const apps = ['backend', 'orchestrator'];
const args = (app) => ['--noEmit', '-p', `apps/${app}/tsconfig.build.json`, '--incremental', 'false'];
const base = path.join(root, '.dev-state/typecheck-tests');
fs.mkdirSync(base, { recursive: true });
const run = fs.mkdtempSync(path.join(base, 'run-'));
console.log(`Artifacts: ${path.relative(root, run)}; root TypeScript ${ts.version}`);

// Use the exact checkout scripts with a harmless fake compiler in a private
// package. No real app/config/source/env is modified; no engine can be invoked.
fs.writeFileSync(path.join(run, 'package.json'), JSON.stringify({
  private: true,
  scripts: Object.fromEntries(['typecheck', ...apps.map((app) => `typecheck:${app}`)]
    .map((name) => [name, scripts[name]])),
}));
fs.writeFileSync(path.join(run, 'pnpm-workspace.yaml'), 'packages: []\n');
const bin = path.join(run, 'node_modules/.bin');
fs.mkdirSync(bin, { recursive: true });
fs.writeFileSync(path.join(bin, 'tsc'), `#!/usr/bin/env node
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.TYPECHECK_TEST_LOG, JSON.stringify({ args, cwd: process.cwd() }) + '\\n');
const app = args.includes('apps/backend/tsconfig.build.json') ? 'BACKEND' : 'ORCHESTRATOR';
process.exit(Number(process.env['TYPECHECK_TEST_' + app] || 0));
`, { mode: 0o755 });

function command(label, name, backend = 0, orchestrator = 0) {
  const log = path.join(run, `${label}.jsonl`);
  const result = spawnSync('pnpm', ['run', name], {
    cwd: run, encoding: 'utf8', timeout: 20000,
    env: { PATH: process.env.PATH, HOME: process.env.HOME, CI: 'true',
      TYPECHECK_TEST_LOG: log, TYPECHECK_TEST_BACKEND: String(backend),
      TYPECHECK_TEST_ORCHESTRATOR: String(orchestrator) },
  });
  fs.writeFileSync(path.join(run, `${label}.log`), result.stdout + result.stderr);
  assert.ifError(result.error);
  assert.equal(result.signal, null);
  const records = fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').map(JSON.parse) : [];
  for (const record of records) assert.equal(record.cwd, run);
  return { status: result.status, calls: records.map((record) => record.args) };
}

test('explicit root scripts use tsc, actual build configs, no emit/cache/watch', () => {
  assert.equal(ts.version, '5.5.4');
  for (const app of apps) assert.equal(scripts[`typecheck:${app}`], `tsc ${args(app).join(' ')}`);
  assert.equal(scripts.typecheck, 'pnpm run typecheck:backend && pnpm run typecheck:orchestrator');
});

for (const app of apps) {
  test(`${app}: unchanged build config preserves source aliases/shared resolution`, () => {
    const configPath = path.join(root, `apps/${app}/tsconfig.build.json`);
    const parsed = ts.getParsedCommandLineOfConfigFile(configPath, { noEmit: true, incremental: false }, {
      ...ts.sys, onUnRecoverableConfigFileDiagnostic: (error) => assert.fail(error.messageText),
    });
    assert.deepEqual(parsed.errors, []);
    assert.equal(parsed.options.noEmit, true);
    assert.equal(parsed.options.incremental, false);
    assert(!parsed.options.composite, 'disabling incremental must remain legal');
    assert(!parsed.options.noResolve, 'imports must remain checked');
    assert(parsed.fileNames.includes(path.join(root, `apps/${app}/src/main.ts`)));
    const inheritedPaths = JSON.parse(fs.readFileSync(path.join(root, 'tsconfig.base.json'))).compilerOptions.paths;
    assert.deepEqual(parsed.options.paths, inheritedPaths);
    for (const [alias, target] of [
      ['@gitroom/helpers/auth/auth.service', 'libraries/helpers/src/auth/auth.service.ts'],
      ['@gitroom/nestjs-libraries/database/prisma/prisma.service', 'libraries/nestjs-libraries/src/database/prisma/prisma.service.ts'],
    ]) {
      const resolved = ts.resolveModuleName(alias, path.join(root, `apps/${app}/src/main.ts`), parsed.options, ts.sys);
      assert.equal(resolved.resolvedModule?.resolvedFileName, path.join(root, target));
      assert.equal(resolved.resolvedModule.isExternalLibraryImport, false);
    }
  });
  for (const status of [0, 17]) {
    test(`${app}: pnpm routes exact arguments and propagates status ${status}`, () => {
      const result = command(`${app}-${status}`, `typecheck:${app}`, status, status);
      assert.equal(result.status, status);
      assert.deepEqual(result.calls, [args(app)]);
    });
  }
}

for (const [backend, orchestrator, expected] of [[0, 0, 0], [17, 0, 17], [0, 23, 23]]) {
  test(`combined: sequential, backend=${backend}, orchestrator=${orchestrator}, exit=${expected}`, () => {
    const result = command(`combined-${backend}-${orchestrator}`, 'typecheck', backend, orchestrator);
    assert.equal(result.status, expected);
    assert.deepEqual(result.calls, backend ? [args('backend')] : apps.map(args));
  });
}
