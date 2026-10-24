// Installed-Nest/SWC and small explicit tsc fixtures: no real app, dotenv or service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const { setTimeout: sleep } = require('node:timers/promises');
const { swcDefaultsFactory } = require('@nestjs/cli/lib/compiler/defaults/swc-defaults');
const { TsConfigProvider } = require('@nestjs/cli/lib/compiler/helpers/tsconfig-provider');
const { TypeScriptBinaryLoader } = require('@nestjs/cli/lib/compiler/typescript-loader');

const root = path.resolve(__dirname, '../../..');
const json = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const write = (file, content) => {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
};
const records = (file) => fs.existsSync(file)
  ? fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
async function until(check, label) {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    if (check()) return;
    await sleep(50);
  }
  throw new Error(`Timeout: ${label}`);
}

async function watchChild() {
  // Run the actual installed CLI parser/build/restart path, without dotenv or apps.
  // Explicit fixture termination runs Nest's exit hook, which stops its Node child.
  process.on('SIGTERM', () => process.exit(0));
  process.argv = [process.execPath, require.resolve('@nestjs/cli/bin/nest.js'),
    'start', '--config', process.argv[3], '--watch'];
  require('@nestjs/cli/bin/nest.js');
}

async function fixture(app, runDir) {
  const appConfig = json(path.join(root, `apps/${app}/nest-cli.swc.json`));
  const provider = new TsConfigProvider(new TypeScriptBinaryLoader());
  const { options } = provider.getByConfigFilename(appConfig.compilerOptions.tsConfigPath);
  const defaults = swcDefaultsFactory(options, appConfig);
  assert.equal(appConfig.monorepo, false);
  assert.equal(appConfig.compilerOptions.webpack, false);
  assert.equal(appConfig.compilerOptions.typeCheck, false);
  assert.equal(appConfig.compilerOptions.deleteOutDir, false);
  assert.deepEqual(appConfig.compilerOptions.plugins, []);
  assert.equal(defaults.cliOptions.stripLeadingPaths, false);
  assert.equal(defaults.cliOptions.sync, true); // No CPU-count-sized SWC worker pool.
  assert.equal(defaults.cliOptions.copyFiles, false);
  assert.equal(defaults.cliOptions.deleteDirOnStart, false);
  assert.deepEqual(defaults.cliOptions.extensions, ['.ts', '.js']);
  assert.equal(options.baseUrl, root);
  assert.equal(options.outDir, path.join(root, `.dev-state/swc/${app}`));
  assert.equal(defaults.cliOptions.outDir, `.dev-state/swc/${app}`);
  const rootPaths = json(path.join(root, 'tsconfig.base.json')).compilerOptions.paths;
  assert.deepEqual(Object.keys(options.paths).sort(), [
    '@gitroom/backend/*', '@gitroom/helpers/*', '@gitroom/nestjs-libraries/*',
    '@gitroom/orchestrator/*',
  ].sort());
  for (const [alias, targets] of Object.entries(options.paths)) {
    assert.deepEqual(targets, rootPaths[alias]);
    assert(!targets.some((target) => target.endsWith('.d.ts')));
  }
  assert.deepEqual(defaults.cliOptions.filenames, [
    `apps/${app}/src`, 'libraries/helpers/src', 'libraries/nestjs-libraries/src',
    ...(app === 'orchestrator' ? ['apps/backend/src'] : []),
  ]);
  assert.equal(json(path.join(root, 'package.json')).scripts[`dev:${app}:swc`],
    `pnpm --filter ./apps/${app} run dev:swc`);
  const script = json(path.join(root, `apps/${app}/package.json`)).scripts['dev:swc'];
  assert.equal(script,
    `cd ../.. && dotenv -e .env -- nest start --config apps/${app}/nest-cli.swc.json --watch`);
  assert.equal(spawnSync('bash', ['-n', '-c', script]).status, 0);

  const cwd = path.join(runDir, app);
  const outDir = path.join(cwd, 'output');
  const log = path.join(cwd, 'runtime.jsonl');
  const at = (file) => path.join(cwd, file);
  const helper = 'libraries/helpers/src/probe.ts';
  const shared = 'libraries/nestjs-libraries/src/probe.ts';
  const typeProbe = 'libraries/helpers/src/typecheck-probe.ts';
  write(at(typeProbe), "export const typedValue: string = 'safe';\n");
  const originalProbe = fs.readFileSync(at(typeProbe));
  // The checker includes only an app entry, following its relative import into
  // a private shared source. All compiler settings/aliases come from the actual
  // app build config, not the SWC config or Nest's internal TypeScript loader.
  write(at(`apps/${app}/src/typecheck-probe.ts`),
    "export { typedValue } from '../../../libraries/helpers/src/typecheck-probe';\n");
  write(at('tsconfig.typecheck.json'), JSON.stringify({
    extends: path.join(root, `apps/${app}/tsconfig.build.json`),
    compilerOptions: { types: [] },
    include: [`apps/${app}/src/typecheck-probe.ts`],
  }));
  const tsc = require.resolve('typescript/bin/tsc');
  assert.equal(require('typescript').version, '5.5.4');
  function checkTypes(label) {
    const result = spawnSync(process.execPath,
      [tsc, '--noEmit', '-p', 'tsconfig.typecheck.json', '--incremental', 'false', '--pretty', 'false'],
      { cwd, encoding: 'utf8', timeout: 20000 });
    write(at(`typecheck-${label}.log`), result.stdout + result.stderr);
    assert.ifError(result.error);
    assert.equal(result.signal, null);
    return result;
  }
  const safe = checkTypes('safe');
  assert.equal(safe.status, 0, safe.stdout + safe.stderr);
  write(at(typeProbe), 'export const typedValue: string = 123;\n');
  try {
    const fault = checkTypes('fault');
    assert.equal(fault.status, 2, fault.stdout + fault.stderr);
    assert.match(fault.stdout, /libraries\/helpers\/src\/typecheck-probe\.ts\(1,14\): error TS2322/);
    assert.match(fault.stdout, /Type 'number' is not assignable to type 'string'/);
    assert(!fs.readdirSync(cwd, { recursive: true }).some((file) => /\.(js|tsbuildinfo)$/.test(file)),
      'explicit checker must not emit JS or a build-info cache');
  } finally {
    fs.writeFileSync(at(typeProbe), originalProbe);
    assert.deepEqual(fs.readFileSync(at(typeProbe)), originalProbe);
  }
  write(at(helper), 'export class Helper {}\nexport const value = 1;\n');
  write(at('apps/backend/src/probe.ts'), 'export const backend = 7;\n');
  write(at(shared), `import { backend } from '@gitroom/backend/probe';
export const shared = backend;
`);
  // Real P5 exposed a TDZ through export getters in mutually injected services.
  // Keep the forwardRef source pattern intact; only DEV class lowering fixes it.
  write(at('libraries/nestjs-libraries/src/cycle-a.ts'), `
import { Injectable, Inject, forwardRef } from '@nestjs/common';
import { CycleB } from './cycle-b';
@Injectable()
export class CycleA { constructor(@Inject(forwardRef(() => CycleB)) public b: CycleB) {} }
`);
  write(at('libraries/nestjs-libraries/src/cycle-b.ts'), `
import { Injectable, Inject, forwardRef } from '@nestjs/common';
import { CycleA } from './cycle-a';
@Injectable()
export class CycleB { constructor(@Inject(forwardRef(() => CycleA)) public a: CycleA) {} }
`);
  // Prove the explicit dev SWC config prevents accidental nested .swcrc loading.
  write(at(`apps/${app}/.swcrc`), 'intentionally invalid inherited fixture config');
  write(at(`apps/${app}/src/workflows/index.ts`), `export const workflow = 'fixture';\n`);
  write(at(`apps/${app}/src/main.ts`), `
import 'reflect-metadata';
import { Injectable } from '@nestjs/common';
import { IsString, validateSync } from 'class-validator';
import { Exclude, instanceToPlain } from 'class-transformer';
import { Helper, value } from '@gitroom/helpers/probe';
import { shared } from '@gitroom/nestjs-libraries/probe';
import { CycleA } from '@gitroom/nestjs-libraries/cycle-a';
import { CycleB } from '@gitroom/nestjs-libraries/cycle-b';
import { SELF_DECLARED_DEPS_METADATA } from '@nestjs/common/constants';
import { strict as assert } from 'node:assert';
import { appendFileSync } from 'node:fs';
@Injectable()
class Consumer { constructor(public helper: Helper) {} }
class LoweringProbe {
  static value: number;
  static { this.value = 9; }
  #value: number;
  constructor(value = 4) { this.#value = value; }
  read() { return this.#value; }
}
assert.equal(LoweringProbe.value, 9);
assert.equal(new LoweringProbe().read(), 4);
class Dto {
  @IsString() title: string;
  @Exclude() secret: string;
}
assert.equal(Reflect.getMetadata(SELF_DECLARED_DEPS_METADATA, CycleA)[0].param.forwardRef(), CycleB);
assert.equal(Reflect.getMetadata(SELF_DECLARED_DEPS_METADATA, CycleB)[0].param.forwardRef(), CycleA);
assert.deepEqual(Reflect.getMetadata('design:paramtypes', Consumer), [Helper]);
assert.equal(Consumer.name, 'Consumer');
assert.equal(Reflect.getMetadata('design:type', Dto.prototype, 'title'), String);
const dto = new Dto();
dto.title = 1 as any;
dto.secret = 'fixture-only';
assert.equal(validateSync(dto).length, 1);
assert.equal('secret' in instanceToPlain(dto), false);
assert(!require.resolve('@langchain/langgraph/prebuilt').endsWith('.d.ts'));
const workflow = require.resolve('./workflows');
assert(workflow.includes('/output/apps/${app}/src/workflows/index.js'));
appendFileSync(process.env.SWC_FIXTURE_LOG!, JSON.stringify({ pid: process.pid, value, shared, workflow }) + '\\n');
setInterval(() => {}, 1000);
`);
  // Only the fixture root/output changes. The real filenames, aliases, sourceRoot,
  // entryFile, native config and installed builder/restart implementation are used.
  const config = structuredClone(appConfig);
  config.compilerOptions.builder.options.outDir = outDir;
  config.compilerOptions.builder.options.swcrcPath = path.relative(cwd, path.join(root, 'scripts/dev/swc.json'));
  config.compilerOptions.tsConfigPath = 'tsconfig.json';
  write(at('tsconfig.json'), JSON.stringify({
    extends: path.join(root, `apps/${app}/tsconfig.swc.json`),
    compilerOptions: { baseUrl: '.', rootDir: '.', outDir: './output' },
    include: defaults.cliOptions.filenames.map((dir) => `${dir}/**/*.ts`),
  }));
  write(at('fixture.json'), JSON.stringify(config));
  const transcript = fs.openSync(at('watch.log'), 'w');
  const child = spawn(process.execPath, [__filename, '--watch-child', 'fixture.json'], {
    cwd, env: { ...process.env, SWC_FIXTURE_LOG: log }, stdio: ['ignore', transcript, transcript],
  });
  fs.closeSync(transcript);
  const exited = once(child, 'exit');
  try {
    await until(() => records(log).some((r) => r.value === 1 && r.shared === 7), `${app} initial runtime metadata/aliases`);
    // Let both installed output and source watchers finish their initial scan.
    await sleep(700);
    write(at(typeProbe), 'export const typedValue: string = 123;\n');
    await until(() => fs.readFileSync(path.join(outDir, typeProbe.replace(/\.ts$/, '.js')), 'utf8')
      .includes('123'), `${app} SWC emits deliberate type error`);
    const firstPid = records(log).at(-1).pid;
    fs.appendFileSync(at(`apps/${app}/src/main.ts`), '\n// Reversible app-source fixture edit.\n');
    await until(() => records(log).some((r) => r.pid !== firstPid), `${app} app change restarts Node`);
    const originalPid = records(log).at(-1).pid;
    write(at(helper), 'export class Helper {}\nexport const value = 2;\n');
    await until(() => records(log).some((r) => r.value === 2 && r.pid !== originalPid), `${app} helper change restarts Node`);
    write(at(shared), `import { backend } from '@gitroom/backend/probe';\nexport const shared = backend + 1;\n`);
    await until(() => records(log).some((r) => r.shared === 8), `${app} Nest library change restarts Node`);
    const beforeAdd = records(log).at(-1).pid;
    write(at('libraries/nestjs-libraries/src/added.ts'), 'export const added = true;\n');
    await until(() => fs.existsSync(path.join(outDir, 'libraries/nestjs-libraries/src/added.js'))
      && records(log).some((r) => r.pid !== beforeAdd && r.shared === 8), `${app} shared addition emits/restarts`);
    for (const file of [
      `apps/${app}/src/main.js`, `apps/${app}/src/main.js.map`, `apps/${app}/src/workflows/index.js`,
      'apps/backend/src/probe.js', 'libraries/helpers/src/probe.js',
      'libraries/nestjs-libraries/src/probe.js',
    ]) assert(fs.existsSync(path.join(outDir, file)), file);
    const emitted = fs.readFileSync(path.join(outDir, `apps/${app}/src/main.js`), 'utf8');
    assert(!emitted.includes('require("@gitroom/'));
    // Same bytes that produce TS2322 above are successfully transpiled by the
    // real SWC builder. Runtime success is deliberately NOT type correctness.
    assert.equal(require(path.join(outDir, typeProbe.replace(/\.ts$/, '.js'))).typedValue, 123);
    assert(!fs.readFileSync(at('watch.log'), 'utf8').includes('TS2322'));
  } finally {
    fs.writeFileSync(at(typeProbe), originalProbe);
    assert.deepEqual(fs.readFileSync(at(typeProbe)), originalProbe);
    child.kill('SIGTERM');
    await Promise.race([exited, sleep(5000).then(() => { throw new Error('Fixture cleanup timed out'); })]);
    await until(() => records(log).every(({ pid }) => {
      try { process.kill(pid, 0); return false; } catch (err) { return err.code === 'ESRCH'; }
    }), `${app} runtime children stopped`);
  }
  console.log(`PASS ${app}: tsc safe=0/fault=2 TS2322 in imported shared source, no emit/cache, exact fixture restore; SWC accepts same type error; native SWC, decorator metadata, DTO validation/serialization, aliases, layout, app/shared change/add restart, owned fixture cleanup`);
}

async function main() {
  assert.equal(Number(process.versions.node.split('.')[0]), 22, 'Run with repository Node 22');
  assert.equal(process.cwd(), root, 'Run from checkout root');
  const base = path.join(root, '.dev-state/swc-tests');
  fs.mkdirSync(base, { recursive: true });
  const runDir = fs.mkdtempSync(path.join(base, 'run-'));
  console.log(`Artifacts: ${path.relative(root, runDir)}; Node ${process.version}; SWC ${require('@swc/core').version}`);
  for (const app of ['backend', 'orchestrator']) await fixture(app, runDir);
  console.log('PASS both configs/scripts; 2 safe controls + 2 detected faults; no real apps, services, dotenv or full-app build/typecheck started');
}

(process.argv[2] === '--watch-child' ? watchChild() : main()).catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
