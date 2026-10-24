# Linux development environment

Make supervises host applications and an isolated persistent PostgreSQL/Redis/Temporal stack. It does not build application containers, install tools, or prepare the database schema. Start here; detailed compiler/controller notes follow. Historical trials are in the secondary [validation records](development-validation.md).

## Quickstart: lightweight local development

### 1. Prerequisites and local choices

Run from the checkout root in a POSIX-compatible shell (`sh` syntax below), with these tools already available on `PATH`:

- Linux, GNU Make, Python 3.9+ with `os.pidfd_open` and `signal.pidfd_send_signal` support.
- Node.js `>=22.12.0 <23` and pnpm **10.6.1** (see `package.json`); select them in every terminal using your preferred setup. No global version-manager changes are required here.
- Docker with Compose v2, the default. Alternatively, explicitly select a locally installed Podman with `podman-compose` or `podman compose`.

Create ignored `.make.local` only if absent (`test -e .make.local || cp .make.local.example .make.local`), then edit it for your machine:

```make
CONTAINER_ENGINE=docker
DEV_COMPILER=swc
```

For Podman change only the first line to `CONTAINER_ENGINE=podman`. The example file selects Podman, so review it rather than blindly accepting its engine choice. Shared defaults remain Docker and `DEV_COMPILER=tsc`; SWC is a local opt-in for backend/workers, **not a typechecker**. Production compilation is unchanged. Stop with `make down` before changing a running server's compiler or destinations.

### 2. Configure the isolated destinations and applications

The following copyable sample uses only the light stack's synthetic defaults. Use it in a dedicated development shell. Do not reuse production credentials or overwrite an existing `.env`. The controller never reads `.env`, but the original app/dotenv scripts still do; these exports override their non-override loading, **not sandbox all other settings**.

```sh
export LIGHT_POSTGRES_USER=postiz_light LIGHT_POSTGRES_PASSWORD=postiz-light-local-only
export LIGHT_POSTGRES_DB=postiz_light LIGHT_POSTGRES_PORT=15433
export LIGHT_REDIS_PORT=6380 LIGHT_TEMPORAL_PORT=7234 LIGHT_TEMPORAL_NAMESPACE=default
export DATABASE_URL='postgresql://postiz_light:postiz-light-local-only@127.0.0.1:15433/postiz_light'
export REDIS_URL='redis://127.0.0.1:6380/0'
export TEMPORAL_ADDRESS='127.0.0.1:7234' TEMPORAL_NAMESPACE=default
export TEMPORAL_TLS=false TEMPORAL_API_KEY=''
export PORT=3000 ORCHESTRATOR_PORT=3002
export FRONTEND_URL='http://localhost:4200'
export NEXT_PUBLIC_BACKEND_URL='http://localhost:3000' BACKEND_INTERNAL_URL='http://localhost:3000'
export STORAGE_PROVIDER=local UPLOAD_DIRECTORY="$PWD/var/uploads"
export NEXT_PUBLIC_UPLOAD_STATIC_DIRECTORY='./uploads'
# Generate privately once per development session; do not print/share the value.
export JWT_SECRET="$(node -e "process.stdout.write(require('node:crypto').randomBytes(32).toString('hex'))")"
mkdir -p var/uploads
pnpm install --frozen-lockfile
```

Installation generates Prisma Client. Do not indiscriminately enable omitted dependency scripts. Keep the same JWT secret for apps in a session; changing it invalidates existing sessions. A new clone can use these exports without creating `.env`; inspect `.env.example` for optional settings, leaving real provider/email/cron credentials disabled. Existing local configuration remains your responsibility. Do not copy the older private test harness's `NOT_SECURED=true`: it caused a credentials/CORS incompatibility. The subsequent browser trial explicitly exported `NOT_SECURED=''` (an empty value, **not** the truthy string `false`) so non-overriding dotenv could not restore it; with matching frontend origin, credentialed CORS and localhost secure-cookie login worked without disabling security. Blank provider keys do not guarantee zero external frontend traffic.

**Make precedence:** command-line assignments override plain `.make.local` assignments, then shell environment (without `make -e`). For this default-only sample, keep `.make.local` limited to engine/compiler and remove conflicting destination/port assignments there before proceeding. Standalone Compose below does **not** read `.make.local`; its file must match these exports. If customizing any `LIGHT_*` value, change both the matching URL/namespace and bootstrap file before starting anything. All six app/infra ports must be distinct. See [configuration details](#lightweight-infrastructure) for validation rules and the exact supported Make inputs.

### 3. First clone only: start infrastructure and explicitly prepare schema

Do this **before** the first `make up`. Use only a new/known light project for this checkout; if `postiz-light` already belongs to another checkout or has unexpected data/ports, stop and inspect, never recreate/reset it.

Create a private, controlled Compose interpolation file for the default-only sample (not the repository `.env`):

```sh
mkdir -p .dev-state
chmod 700 .dev-state
(umask 077; cat > .dev-state/light-bootstrap.env <<'ENV'
LIGHT_COMPOSE_PROJECT=postiz-light
LIGHT_POSTGRES_USER=postiz_light
LIGHT_POSTGRES_PASSWORD=postiz-light-local-only
LIGHT_POSTGRES_DB=postiz_light
LIGHT_POSTGRES_PORT=15433
LIGHT_REDIS_PORT=6380
LIGHT_TEMPORAL_PORT=7234
LIGHT_TEMPORAL_NAMESPACE=default
ENV
)
# Sanitized local engine routing and explicit --env-file; no ambient .env interpolation.
light_compose() {
  env -i PATH="$PATH" HOME="$HOME" XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-}" \
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-}" \
    COMPOSE_DISABLE_ENV_FILE=true DOCKER_CONTEXT=default \
    docker compose --env-file "$PWD/.dev-state/light-bootstrap.env" \
    -p postiz-light -f "$PWD/docker-compose.light.yaml" "$@"
}
```

For Podman, replace `docker compose` in the function with `podman-compose` (or `podman compose` if that is your installed provider). This must match the engine selected in `.make.local`. Then run:

```sh
# Docker default; infrastructure only, no apps or schema changes:
light_compose up -d --no-recreate postgres redis temporal
light_compose ps
```

Wait for **all three services healthy**, not just running. Confirm `ps` shows only this checkout's light services and loopback ports 15433/6380/7234. Verify database identity and existing tables:

```sh
light_compose exec -T postgres psql -U postiz_light -d postiz_light \
  -c "SELECT current_database(), current_user; SELECT tablename FROM pg_tables WHERE schemaname='public';"
```

For a fresh database expect `postiz_light` for database/user and no public application tables. Also verify the shell's synthetic `DATABASE_URL` is exactly the value above, including user/password, `127.0.0.1`, port and database; the container query alone does not prove the app URL. Only after both checks, prepare this verified **local** destination:

```sh
pnpm exec prisma db push --skip-generate --schema libraries/nestjs-libraries/src/database/prisma/schema.prisma
```

Do not use reset, force-reset or data-loss flags. Stop on unexpected existing schema/data or warnings; do not push to an unverified destination. Repeat schema preparation only deliberately after schema changes. `make up` never pushes/migrates schema. If abandoning bootstrap before any successful Make startup, stop its services with `light_compose stop postgres redis temporal`; `make down` has no recorded startup configuration yet. Keep volumes/data.

### 4. Daily background workflow

In the configured shell, with sufficient resource headroom:

```sh
make help
make up                 # default full: backend + orchestrator/workers + frontend
make status             # recorded process ownership/live member counts, not a health dashboard
make logs               # recent app logs in .dev-state/
make down               # same selected engine; stops owned apps/light services, retains data/logs
# Alternative starts after down (not frontend-only scopes):
make up SCOPE=backend   # backend + all light infrastructure
make up SCOPE=workers   # backend + orchestrator, no frontend
```

`up` waits for bounded readiness, then returns with apps in the background. Repeated `up` reuses matching live owned processes. Scopes are **additive**: narrowing does not stop workers/frontend; use `make down` first to narrow. No browser extension is included. Logs, schema, uploads and named volumes survive `down`; do not use `down -v` or prune. Existing unrelated services are not adopted or stopped.

Open **http://localhost:4200/auth** in full scope. Backend: **http://localhost:3000/auth/can-register**; workers health: **http://localhost:3002/health/status**. HTTP alone is not a browser-flow test; worker readiness additionally proves a recent owned Temporal `main` workflow poller. App servers may bind beyond loopback: use a trusted local network. The light infrastructure publishes loopback only.

## What is verified, and what remains pending

| Area | Current evidence / limitation |
| --- | --- |
| Controller safety suite | Independently reviewed; latest recorded run: 32 tests / 169 subcases, no failures/errors/skips. Fixtures are not application validation. |
| Podman + SWC backend/workers | Actual readiness, repeated up and backend/shared-library/orchestrator reload passed. No social publication/provider activity execution claimed. |
| Full scope | Default `make up` exit0 with both original Nest SWC watchers, original Next dev and light infra; **631.920-second bounded coexistence smoke passed**, 21 periodic health/poller checks, all initial owned PID/start identities preserved, repeated full `up` exit0 unchanged. Actual authenticated Calendar/Media checked in this full topology. Independently reviewed and accepted as bounded development verification, not a production/load/long-term leak guarantee. |
| Browser | Signup/autologin/Calendar passed previously. A subsequent private **compiled SWC backend (no backend watcher) + original Next dev + light infra, workers off** trial passed password login, authenticated Media, local PNG upload/record/preview/static GET, and reversible visible frontend HMR. Credentialed CORS worked with `NOT_SECURED=''`; only a Lit development warning was observed on Media. External frontend traffic remained. This is not default Make/full-scope validation or asynchronous job execution. |
| Docker | Default interface retained; compatibility is **assumed, not runtime-verified**. The user explicitly excluded Docker validation from both local work and CI; it is **out of scope, not pending acceptance**. Podman is the exercised engine. |
| Full type checking | Both complete app checks passed separately with root TypeScript 5.5.4: backend exit0 (32.11s), orchestrator exit0 (35.51s), zero diagnostics. Explicit per-checker V8 heap4GiB inside monitored common6GiB/swap0, no apps/infra running. This supersedes the earlier default-heap backend OOM; it is static checking under existing compiler settings, not runtime/full-stack acceptance. |

Integrated development verification is **independently reviewed and accepted** within the documented scope. The final approved default-full run supplies a bounded ten-minute smoke rather than only brief readiness. It reused warm caches and the same common 6 GiB hard cap, zero experiment swap and 3 GiB host floor, without substituting a compiled backend or disabling workers. No guard trip, health error or new OOM occurred during its 631.920-second window. Host available minimum was3.363GiB; sampled common usage approached6GiB and reclaim counters increased, so this is **not a comfortable-headroom hardware recommendation**. During subsequent intentional `make down`, the private guard incorrectly treated exiting worker members as a liveness failure and interrupted the runner; fallback cleanup and two further `make down` calls succeeded. Raw failure evidence is retained, not presented as an all-time clean harness exit. State{}, empty owned cgroup, restored max/max limits and unchanged original service identities were verified. These are private trial controls, **not an automatic Make resource guard**, production/load testing or long-term leak proof. Earlier resource-stop records remain historical; do not retry an actual resource stop without a new decision. Host swap/PSI/reclaim counters were diagnostic; exact outcomes and cleanup are retained in the [validation appendix](development-validation.md#p5-default-full-bounded-stability-smoke-passed).

## Opt-in SWC development compiler

The backend and orchestrator have separate opt-in Nest SWC watch scripts:

```sh
pnpm run dev:backend:swc
pnpm run dev:orchestrator:swc
```

Use Node.js `>=22.12.0 <23` and the installed workspace dependencies. These commands retain dotenv loading and the pnpm → Nest watcher → Node process contract. Per-app `dev:swc` changes to the checkout root before running Nest, so source paths and emitted relative imports have a consistent repository-relative layout. It loads `.env` there without overriding exported values. Configure safe local destinations before starting either app; these scripts do not start infrastructure or prepare schema.

This is the **direct SWC builder**, not webpack: the local Nest applications declare `monorepo: false` and `webpack: false`. Dedicated `nest-cli.swc.json` and `tsconfig.swc.json` files select it; the shared `scripts/dev/swc.json` retains CommonJS, legacy decorators, decorator metadata, class names and source maps. Its Node 22 preset explicitly downlevels classes and block scoping, along with the required class-feature/static-block and parameter transforms (the installed SWC otherwise panics on static blocks or emits an illegal strict function with default parameters): the first real integrated runtime exposed a circular `IntegrationService`/`RefreshIntegrationService` metadata read through SWC export getters before the class binding was initialized, despite the existing `forwardRef` injection. Function/`var` class output makes that early metadata read undefined (the explicit `forwardRef` still resolves injection), rather than throwing a temporal-dead-zone error. `jsc.target: null` explicitly clears Nest's merged ES2021 default so it cannot conflict with the `env` preset. This development-only compatibility adjustment does not change business source or production compilation; other syntax follows the Node 22 target. There are no configured Nest AST plugins. The builder uses synchronous native transforms to avoid SWC CLI's CPU-count-sized worker pool. SWC **transpiles without checking types**; no background typechecker is enabled. Separate explicit typecheck commands are documented below. Successful transpilation is not proof of type correctness or application readiness.

Outputs are isolated under ignored `.dev-state/swc/backend/` and `.dev-state/swc/orchestrator/`, with `apps/<app>/src/main.js`, `libraries/helpers/src/` and `libraries/nestjs-libraries/src/` preserved beneath each output root. No production `dist` is read, cleared or reused by these scripts. They do not delete the dev output directory on startup either. After source removal, watch deletes the corresponding emitted file; a removal made while the watcher is stopped can leave stale dev output. For a clean-layout investigation use a fresh ignored output directory (override both Nest's tsconfig outDir and the builder outDir together); never substitute old production output as evidence.

Both source lists include the app plus helpers and Nest libraries; orchestrator additionally includes backend sources because shared services import backend auth/exception code. Only `.ts`/`.js` are compiled: frontend, extension and React/TSX sources are not part of this server watch. Runtime aliases are limited to backend, orchestrator, helpers and Nest libraries. The root's `@langchain/langgraph/prebuilt` **declaration-file** alias is deliberately omitted so Node resolves the package's real runtime export. The inherited orchestrator `.swcrc` with another machine's absolute path is not used or changed. `jsc.baseUrl` comes from the parsed dev tsconfig (an absolute checkout root resolved at runtime), not a hardcoded host path.

Installed Nest CLI 11.0.21 passes all builder `filenames` to SWC CLI 0.3.14. SWC watches each source directory for additions/changes/removals and emits shared files in the same layout. Nest watches the entire dev output directory for `.js`/`.mjs` additions/changes and restarts its Node child, including after shared-library emissions. Nest's extra source-add watcher covers only the first source directory; shared additions are handled by SWC's own watcher. Orchestrator's emitted `require.resolve('./workflows')` continues to address its emitted sibling workflow directory. Small fixture checks exercise these installed implementations, including a circular decorated `forwardRef` pair and static/private class fields with default constructor parameters. Real backend/worker reload, Temporal bundling/polling and DI/DTO behavior were subsequently exercised in the [validation records](development-validation.md). A workers-off watcher-based browser trial verified signup/autologin and Calendar rendering before a host-memory-floor stop. A later separate workers-off trial with a freshly compiled SWC backend runtime (no Nest/SWC watcher) verified password login, local upload/preview and visible frontend HMR. That private command override is not a supported Make option or proof of sustained full feasibility. The subsequent default-full smoke used both original Nest SWC watchers and original Next dev, with current authenticated Calendar/Media browser checks; its 631.920-second bounded result and teardown-harness limitation are recorded separately above.

Run the small native/metadata/alias/layout/watch fixture without apps, dotenv or services:

```sh
node scripts/dev/tests/swc.cjs
```

It requires Node 22 and installed dependencies, retains isolated artifacts under `.dev-state/swc-tests/`, and never compiles or starts the real applications. It does not perform a full repository typecheck. Existing `dev:backend`, `dev:orchestrator`, per-app `dev`, production build/start commands and production configs remain unchanged as fallbacks. **Make supports `DEV_COMPILER=tsc|swc`**, defaulting to `tsc` in shared configuration. Only backend and orchestrator change scripts; frontend always uses `dev:frontend`. With explicit safe datastore exports (see the quickstart above), select SWC or the original fallback:

```sh
make up SCOPE=backend DEV_COMPILER=swc
make up DEV_COMPILER=swc                 # full scope, subject to runtime eligibility
make down                              # required before switching a live server compiler
make up SCOPE=backend DEV_COMPILER=tsc   # original fallback
```

Optionally add `DEV_COMPILER=swc` manually to ignored `.make.local` alongside your engine choice; `.make.local.example` documents it. This configures the selector only: it never writes `.env` or starts a checker. CLI assignments override local assignments, then environment values. Invalid compiler values fail before any engine command or app launch. No automatic fallback or replacement occurs.

New process records contain the actual per-app compiler (`tsc`/`swc` for server apps, fixed `next` for frontend), command and a destination/port/compiler fingerprint. Any owned live backend/orchestrator with a different compiler blocks `up` before infrastructure access, even when outside the requested scope; use explicit `make down` first. Scope narrowing remains additive. Legacy live records without a compiler are reusable only with their original destination hash and exact original pnpm command, proving the pre-selector `tsc` (or unchanged frontend) mode. Missing/ambiguous command metadata fails closed; no compiler is guessed or process killed. Legacy records are not relabeled during reuse. Stale records with no verified live members can be replaced, under the existing ownership checks. Failed expansion preserves previously live records and processes.

Controller tests exercise selection, literal inputs, ownership and rollback with harmless fixtures, not real Nest apps. The unchanged supervisor/session token and PID/listener/poller readiness proof is independent of `.dev-state/swc/` output paths. Following independent review, progressive runtime trials were performed; see the [validation records](development-validation.md). Fixture results alone are not integrated acceptance or a measured memory-saving claim.

## Explicit server type checking

From the checkout root, with Node 22 and the installed workspace dependencies:

```sh
pnpm run typecheck:backend
pnpm run typecheck:orchestrator
# Or check backend, then orchestrator; stop on the first failing app:
pnpm run typecheck
```

Each per-app script runs the **root TypeScript compiler** (`typescript` 5.5.4), not Nest CLI's internal checker: `tsc --noEmit -p apps/<app>/tsconfig.build.json --incremental false`. The original build configurations inherit the repository's type aliases, decorators and compiler settings. These commands check the app and its transitive shared-library TypeScript sources; they do not narrow checking to the smaller SWC watch file list. Existing `skipLibCheck` behavior is preserved (dependency declaration files are not fully checked). There is no emit, build-info cache, watch mode, dotenv loading, application execution or service startup. Production/CI scripts, dependencies and the original `tsc` watch commands are unchanged. This does not assert equivalence to another TypeScript version bundled with Nest CLI.

Type checking terminates with the compiler's diagnostics and nonzero status on errors. The combined script uses `&&`, runs strictly sequentially, and does not check orchestrator if backend fails; run the individual orchestrator command to inspect it separately. These are explicit developer checks, **not** an automatic prerequisite or background companion to `make up`; no Make input, process state or fingerprint is added.

A full type graph can be expensive even without emission. Run these checks separately from `make up`/watchers, one app at a time, with sufficient headroom. After the earlier default-heap backend OOM, a separately approved bounded run passed **both complete app checks**, with zero diagnostics, using root TypeScript 5.5.4 and explicit `node --max-old-space-size=4096 node_modules/typescript/bin/tsc --noEmit -p apps/<app>/tsconfig.build.json --incremental false`. That invocation is equivalent to the existing per-app script plus a scoped heap setting; package/global defaults are unchanged. See the [full-check results](development-validation.md#v1-complete-server-typechecks-both-passed). The experiment used a monitored common6GiB hard cap, zero experiment swap and host available floor3GiB; these are trial controls, not built-in script safeguards or a memory guarantee. Do not automatically retry a resource stop or repair a failed baseline with unrelated business/dependency changes.

Durable checks (no real apps, infrastructure, `.env`, credentials or publishing):

```sh
node --test scripts/dev/tests/typecheck.test.cjs
node scripts/dev/tests/swc.cjs
python3 scripts/dev/tests/run.py
```

The first checks exact pnpm script routing, sequential/first-failure behavior and exit propagation using a private fake compiler, plus parses both actual build configurations with root TypeScript to verify aliases and imported shared sources remain in scope. The SWC fixture additionally checks a small type-safe control and an intentional `TS2322` failure with the real compiler, using temporary includes extending each actual build configuration. The identical erroneous fixture source still transpiles and runs with SWC; its original bytes are restored. These reduced semantic fixtures are **not a successful full-app typecheck**. All fixture artifacts are retained under ignored `.dev-state/` on disk. These fixtures do not establish real-app DI, HTTP readiness, watch/reload, workers or browser behavior. See the separate [runtime records](development-validation.md); compile-only evidence is not runtime acceptance.

## Lightweight Make workflow details

The available targets are:

```sh
make help
make up                 # background infrastructure + backend, workers, frontend
make up SCOPE=backend   # infrastructure + backend; no workers or frontend
make up SCOPE=workers   # infrastructure + backend + workers; no frontend
make logs
make status
make down               # stop only this environment; preserve data and logs
```

The default full scope includes backend, orchestrator/workers, and frontend; it does not include the browser extension. Applications run on the host with their existing development scripts, Node.js `>=22.12.0 <23` and pnpm `10.6.1`. PostgreSQL, Redis, and Temporal run as a separate lightweight, persistent local stack; Temporal uses SQLite, not Elasticsearch or a Temporal PostgreSQL database.

Docker is the default container engine. A machine-specific Podman selection lives in ignored `.make.local`; copy `.make.local.example` and select `CONTAINER_ENGINE=podman`. Do not add secrets to that file. There is no automatic engine switching.

The stack uses its own fixed Compose project identity (`postiz-light`), named volumes, and configurable loopback-only host ports. It does not attach to existing Compose services, databases, Redis queues, Temporal state, or volumes. Data and logs are preserved on disk; `down` does not remove volumes. Do not overwrite an existing `.env`. Before any initial schema push, verify that `DATABASE_URL` targets only the new local development database. Schema preparation is separate and explicit; `make up` never runs schema push, reset, or migrations.

Applications use existing per-app pnpm watch scripts (no extension): full runs backend, orchestrator/workers, and frontend; backend runs only backend; workers runs backend plus orchestrator, without frontend. Scopes are additive: narrowing does not stop anything; use explicit `make down`. Controller state and app logs are under ignored `.dev-state/` on disk. A nonblocking lock rejects concurrent transitions. Supported controller inputs are exported as literal environment values, not interpolated into shell recipes or recursively expanded by Make. They are `CONTAINER_ENGINE`, `SCOPE`, `DEV_COMPILER`, `LIGHT_POSTGRES_USER`, `LIGHT_POSTGRES_PASSWORD`, `LIGHT_POSTGRES_DB`, `LIGHT_POSTGRES_PORT`, `LIGHT_REDIS_PORT`, `LIGHT_TEMPORAL_PORT`, `LIGHT_TEMPORAL_NAMESPACE`, `DATABASE_URL`, `REDIS_URL`, `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TLS`, `TEMPORAL_API_KEY`, `PORT`, and `ORCHESTRATOR_PORT`. Set them in the environment, as plain `NAME=value` assignments in ignored `.make.local`, or on the Make command line. Command-line assignments take precedence over `.make.local`, then the environment (normal Make precedence, without `-e`). Unset optional inputs stay unset so the controller's defaults and missing-destination checks still apply. Only this explicit list is exported by the Makefile; there is no global export of arbitrary Make variables. Other application settings retain the existing environment/dotenv route. `.make.local` is trusted Make syntax, not a sandbox: executable Make directives or immediate `:=` expressions in that file are outside the literal-value interface. `LIGHT_COMPOSE_PROJECT` is fixed by the controller, not a configurable Make input.

Linux Python 3.9+ with `os.pidfd_open` and `signal.pidfd_send_signal` is required. Each app has a small Python session supervisor; its selected pnpm command receives no extra arguments. The controller verifies the session, inherited random token, checkout cwd and Linux start time before signaling individual members using pidfds (never a reusable bare PID/process group). Children remaining after a leader exits are still checked and stopped. Ambiguous ownership, legacy state and reused identities fail closed for manual inspection. Existing watch scripts are not daemonized; descendants that deliberately create a new session are unsupported.

`make up` waits for bounded infrastructure and app readiness before returning; apps then remain in the background. On errors or interruption it stops only apps launched and light containers newly started in that invocation (including previously stopped containers it started). Previously running healthy owned components are not restarted or rolled back. Preexisting unhealthy infrastructure is rejected unchanged. Engine commands have deadlines; terminal signals are deferred/ignored during bounded rollback. `down` uses recorded startup configuration and stops only verified app members and exact owned container IDs; it does not require app URLs to remain exported. Select the same engine used for startup. No controller command removes containers, volumes, logs or data. Local engine routing variables are not forwarded to Compose, preventing accidental `DOCKER_HOST`/`CONTAINER_HOST` redirection.

Readiness requires a listener belonging to the recorded app session, not a preexisting foreign server: backend additionally probes `/auth/can-register`, frontend `/auth`, and orchestrator `/health/status`. Worker readiness also runs the pinned Temporal container's CLI `task-queue describe` for the `main` workflow queue and configured namespace. A recent poller must have the installed SDK's default identity `PID@hostname`, with that live PID verified in the owned worker session and its poll newer than the process start. HTTP health or Temporal TCP reachability alone is insufficient. This proves main workflow polling, not execution of all provider activities or real publishing.

### Controller safety tests

Run the durable, standard-library-only suite from the repository root:

```sh
python3 scripts/dev/tests/run.py
```

No package installation, Node.js, pnpm, running engine, application environment, or datastore is required. GNU Make is needed for the literal-input interface tests. The suite is Linux-specific (`/proc` and child-subreaper support); process-signaling cases explicitly skip with a reason if Python/kernel pidfds are unavailable. Skips are not ownership verification. Available memory and swap are sampled at test boundaries; the suite stops below 3 GiB available memory or above 256 MiB swap growth, after owned-process cleanup. This is a boundary check, not continuous profiling. The entry point prints totals, skips and duration, and preserves `unittest.log`, `report.json` (including per-case timings and subcase counts), private Make fixtures, CLI command transcripts, and process-cleanup evidence under a unique ignored `.dev-state/tests/<run-id>/` directory. Artifacts are never deleted. The old `python3 scripts/dev/probe_controller.py` entry point delegates to this suite; historical P3 artifacts remain under `.dev-state/probes/`.

Coverage includes bounded command/startup timeouts and KeyboardInterrupt rollback; occupied foreign ports; stale/reused process identities with real pidfd identity rechecks; concurrent-up locking; additive scope expansion/narrowing and repeated up/down; partial Compose startup and rollback of newly started components only; orphaned descendants and survival of unrelated sessions; Docker JSONL and Podman array inventories, labels, health/configuration drift and fail-closed errors; exact-ID stops and absence of destructive/volume-removal commands; explicit effective datastore configuration before engine access; all 18 supported Make inputs through environment, command line and a private `.make.local` (literal expansion rejection and precedence); compiler selection for every scope, fail-closed live compiler changes and conservative legacy reuse; and worker readiness requiring owned HTTP plus a recent owned workflow poller queried on `main` in the configured namespace.

Only allowlisted simulated Docker/Podman CLIs and harmless Python process sessions run. Tests use ephemeral loopback listeners, isolated state and a private Makefile copy; they do not read the checkout's `.make.local` or `.env`, overwrite application state/logs, bind real app/infra ports, run actual Compose/app watchers/migrations, or contact external services. Every registered dummy session is stopped and checked in cleanup, including on assertion failures. PID reuse is represented by stale identity metadata on a live harmless process; the suite does not force the kernel to recycle PIDs. Startup deadlines are shortened in failure fixtures while exercising the real wait/rollback code. The concurrent-up test holds the real controller lock around a test barrier, not a real deployment.

Passing this suite is controller-level implementation evidence, not full runtime verification. Real scopes, worker behavior and hot reload have separate [runtime evidence](development-validation.md). Browser signup/autologin/Calendar passed in an earlier workers-off trial; password login, local upload/preview and visible frontend HMR passed later with compiled backend + Next dev + light infra, workers off. That later trial did not newly verify Calendar rendering or completed Copilot chat. A subsequent default-full trial sustained all development processes for 631.920 seconds after readiness, passed 21 periodic health checks, and verified authenticated Calendar and Media in Chrome. This bounded development smoke passed final independent review; it does not establish long-term or load stability. Its private monitor misclassified intentional teardown and interrupted the first cleanup runner (exit 143); fallback cleanup and two subsequent `make down` calls succeeded. See the [runtime evidence](development-validation.md) for the preserved failure and cleanup details. No browser flow is started or verified by this suite.

## Lightweight infrastructure

The isolated infrastructure is available as `docker-compose.light.yaml`; it starts only PostgreSQL, Redis, and Temporal with its built-in SQLite store. It does not start applications, run schema preparation, or preload Temporal search attributes. The backend initializer owns creation of `organizationId` and `postId` as `Text` attributes.

The controller selects Docker Compose v2 by default, or Podman using `podman-compose`/`podman compose` when explicitly configured. Compose interpolation is supplied through the controller's generated `.dev-state/compose.env`, with explicit synthetic light-stack values; project `.env` is not loaded for Compose interpolation. Published ports are configurable with `LIGHT_POSTGRES_PORT` (15433), `LIGHT_REDIS_PORT` (6380), and `LIGHT_TEMPORAL_PORT` (7234). PostgreSQL defaults are `LIGHT_POSTGRES_USER=postiz_light`, `LIGHT_POSTGRES_PASSWORD=postiz-light-local-only`, and `LIGHT_POSTGRES_DB=postiz_light`; these synthetic local defaults are not production credentials. `LIGHT_TEMPORAL_NAMESPACE` defaults to `default`.

The Compose health checks probe PostgreSQL readiness, Redis `PING`, and Temporal's CLI cluster-health command. Temporal's namespace is created by `start-dev`; do not confuse this with search-attribute registration. Do not use `down -v` or prune the light stack volumes.

Before using the lightweight Make route, explicitly export `DATABASE_URL`, `REDIS_URL`, `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE` in the invoking shell. Missing/mismatched values are rejected **before any engine command** so the original dotenv-based app scripts cannot silently fall back to another datastore. `DATABASE_URL` must match `127.0.0.1`, the configured PostgreSQL port, database, user **and password**. Its only permitted query is `schema=public`; host/socket overrides are rejected. `REDIS_URL` must use unauthenticated `redis://127.0.0.1:<light-port>/0` (empty path or `/` also means database 0), with no query overrides. Temporal address and namespace must exactly match the light configuration. TLS and API keys are not used for this isolated server; the controller explicitly exports `TEMPORAL_TLS=false` and an empty `TEMPORAL_API_KEY` to block dotenv fallback. Contradictory exported TLS/API key settings are rejected.

For the synthetic defaults only (do not paste real credentials into shared logs):

```sh
export DATABASE_URL='postgresql://postiz_light:postiz-light-local-only@127.0.0.1:15433/postiz_light'
export REDIS_URL='redis://127.0.0.1:6380/0'
export TEMPORAL_ADDRESS='127.0.0.1:7234' TEMPORAL_NAMESPACE='default'
```

`LIGHT_*` configuration accepts letters, digits, `_`, `.` and `-` only, so it cannot inject/interpolate the private Compose env-file. URL values cannot contain dotenv expansion characters; percent-encode URL credentials if necessary. All infra and app ports must be distinct. The backend uses exported `PORT` (default 3000); the orchestrator uses `ORCHESTRATOR_PORT` (default 3002); the existing frontend script fixes port 4200. Exported app ports override repository dotenv values. Running app destination/port changes require an explicit `make down` first. Inspect labels, port bindings and datastore configuration must match this checkout's light Compose stack, including stopped containers; configuration drift is rejected rather than silently reusing or recreating an incompatible stack.

The controller never reads, prints, or overwrites `.env`; other app settings still use the existing scripts' dotenv behavior. Keep them suitable for local development, including frontend/backend public URLs. The controller never reduces worker concurrency or excludes provider queues. `make up` does not run `prisma-db-push`; initial schema preparation remains a separate manual action after verifying the destination database.

## Current manual development route

The existing root scripts remain available as an alternative. This route uses `docker-compose.dev.yaml`, which includes PostgreSQL, Redis, and Temporal with its PostgreSQL and Elasticsearch dependencies; it is not the lightweight stack described above.

1. Use Node.js `>=22.12.0 <23` and pnpm `10.6.1` from the repository root. Create `.env` only if it does not exist (`test -e .env || cp .env.example .env`); preserve existing settings and never commit secrets. Configure the existing local service URLs/credentials in `.env` for this Compose setup. Generate a private `JWT_SECRET`; configure local storage using absolute `UPLOAD_DIRECTORY` and `NEXT_PUBLIC_UPLOAD_STATIC_DIRECTORY=./uploads`.
2. Install dependencies with `pnpm install --frozen-lockfile` if needed.
3. Start the existing development infrastructure, e.g. `podman-compose -f docker-compose.dev.yaml up -d postiz-postgres postiz-redis temporal`. Wait for readiness. This also starts Temporal's PostgreSQL and Elasticsearch dependencies.
4. For initial schema preparation only, verify that `DATABASE_URL` points exclusively to your intended local development database, then run `pnpm run prisma-db-push`. Never use reset or accept data loss to resolve errors. Do not repeat unless schema changes require it.
5. In separate terminals, with Node 22 active, run the scripts that already exist:

   ```sh
   pnpm run dev:backend
   pnpm run dev:frontend
   pnpm run dev:orchestrator
   ```

Open `http://localhost:4200/auth`. The backend and orchestrator have their own configured ports; do not assume that HTTP reachability alone proves application readiness. The root `pnpm dev` also starts the browser extension, while `pnpm dev-backend` starts frontend as well as backend; neither represents the Make scopes. Existing application servers may listen on network interfaces beyond loopback, so account for that on shared networks.

Stop application processes with Ctrl+C in their own terminals. Stop only the services started for this Compose environment, for example:

```sh
podman-compose -f docker-compose.dev.yaml stop temporal postiz-postgres postiz-redis temporal-postgresql temporal-elasticsearch
```

This preserves data. Do not use `down -v`, prune volumes, or terminate unrelated processes. For browser-flow checks, use a dedicated development tab and inspect UI, console errors, and network requests; never use personal tabs or production. Avoid real external publishing during development checks.
