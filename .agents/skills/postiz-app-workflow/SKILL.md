---
name: postiz-app-workflow
description: "Operate Postiz's local Make development stack. Use for local setup, starting or stopping backend/workers/frontend, status and logs, or troubleshooting hot reload and Temporal readiness."
---

# Postiz local development workflow

## Establish root, intent, and authority

- Work from the repository root, not the skill directory. Resolve it with `git -C <skill-directory> rev-parse --show-toplevel`; this skill directory is `.agents/skills/postiz-app-workflow`, so `../../..` from it also reaches the root. All repository paths below are **root-relative**.
- Read `AGENTS.md` and [docs/development.md](../../../docs/development.md), especially its quickstart, before initial setup or an unprepared start. The guide is the operational reference; historical validation is not permission to repeat experiments.
- Use this workflow only when needed and authorized. Informational requests and ordinary code edits do not require starting services. Do not turn loading this skill into automatic setup, startup, shutdown, or resource-policy changes.
- Preserve existing dirty work and local configuration. Never read or print `.env` or secrets, overwrite `.env`/`.make.local`, install tools, stop unrelated services, or publish externally without explicit authorization.

## Preflight without engine rediscovery

1. Start with `make help`: it prints the supported interface without Python, engine access, or service startup. Normal lifecycle operations use **Make**, not ad hoc Compose/controller commands or manually launched pnpm apps.
2. Before startup, require Linux, GNU Make, Python 3.9+ with `os.pidfd_open` and `signal.pidfd_send_signal`, Node.js `>=22.12.0 <23`, pnpm `10.6.1`, and installed workspace dependencies (`package.json`). `make up` verifies Node/pnpm versions from `PATH` and root `node_modules` before engine access or app launch. Report missing prerequisites; do not install automatically.
3. Respect the existing engine/provider choice: shared `Makefile` defaults to Docker with Compose v2; ignored `.make.local` may select Podman with its installed Compose provider. Check only non-secret selectors as needed. Do not probe both engines, autodetect, hardcode Podman, or override `CONTAINER_ENGINE` unless the user requests engine configuration. Docker compatibility is assumed, not runtime-verified.
4. Make passes supported inputs internally: command-line assignments override plain `.make.local` assignments, then environment values (without `make -e`). For supported application/datastore keys, an unset Make/environment value can come from ignored `.dev.env.local`, then defaults; explicit empty higher-priority values remain set. The private file is parsed as literal data only by `up`, with no sourcing/evaluation/interpolation. `.make.local` remains trusted Make syntax for engine/compiler selection, not a sandbox; do not add secrets or executable directives.
5. Verify setup is prepared using the guide: `DATABASE_URL`, `REDIS_URL`, `TEMPORAL_ADDRESS`, and `TEMPORAL_NAMESPACE` must explicitly match the isolated light configuration. Missing/mismatched destinations fail before engine access; do not dump their values. Other app/dotenv settings are not sandboxed by Make.
6. First-clone dependency installation (`pnpm install --frozen-lockfile`), private application configuration (template `.dev.env.example`, ignored runtime file `.dev.env.local`), infrastructure bootstrap, and initial schema preparation are **separate authorized setup actions**, not remedies for an ordinary status/log request. The private settings file is optional; missing file preserves environment-only setup. Only its documented allowlist is supported; do not put credentials or machine selectors outside that contract. Follow the quickstart rather than inventing bootstrap commands. Before any explicitly approved schema push, verify the actual local database destination and existing schema/data. Never reset, force data loss, or migrate automatically; `make up` never prepares schema. `help`, `status`, `logs`, and `down` do not need the private file or Node.
7. Check the user's current resource budget before heavy work. Make has **no automatic 6 GiB guard** or universal memory guarantee. Private trial caps/floors are not current policy. On resource exhaustion/OOM or a policy stop, inspect evidence and report; do not automatically retry, raise limits/heaps, or stop unrelated services to make room.

## Choose and operate the supported scope

Every start includes the three isolated infrastructure services: PostgreSQL, Redis, and Temporal.

| Command | Host application watch processes |
| --- | --- |
| `make up` (default `SCOPE=full`) | Backend + orchestrator/workers + frontend |
| `make up SCOPE=backend` | Backend only |
| `make up SCOPE=workers` | Backend + orchestrator/workers; no frontend |
| `make status` | Recorded process ownership/live-member counts; **not** a health dashboard |
| `make logs` | Recent backend, workers, and frontend application logs |
| `make down` | Stop verified owned applications and light containers; retain data/logs |

- There is no frontend-only or orchestrator-only Make scope and no browser extension in full scope. Do not invent scopes, spawn child apps manually, or substitute root `pnpm dev`/`dev-backend` (their selections differ).
- Starts are additive and reuse matching live owned components. A narrower `up` does **not** stop existing workers/frontend. If the user wants an actually narrower environment, confirm disruption of other active work before explicit `make down` followed by the desired `make up`.
- Stop before changing a live server compiler, destinations, ports, or public URL settings. URL fingerprint identity includes `MAIN_URL`, `FRONTEND_URL`, `NEXT_PUBLIC_BACKEND_URL`, `BACKEND_INTERNAL_URL`, and `NEXT_PUBLIC_OVERRIDE_BACKEND_URL`; missing and empty are distinct. Legacy live records without URL identity require explicit `make down` then `make up`. Keep the startup engine selected for `make down`; it uses recorded configuration and identity-verified engine `stop` by exact container IDs, **not Compose down**.
- Shutdown terminates only verified owned host processes and light containers. Preserve containers, networks, named volumes, schema, uploads, and logs. Never use `down -v`, prune, port-based killing, `pkill`, or kill foreign listeners.
- `.dev-state/` is ignored private controller state and logs, including SWC output. Do not edit/delete ownership metadata or clear evidence to force a restart. Redact secrets from any logs quoted in reports.

## Understand readiness, isolation, and failures

- `make up` waits for bounded readiness, then returns with apps running in the background. Check its exit/result, then use `make status` and `make logs` as appropriate; a process count alone does not establish continued health.
- Infrastructure health and listeners must belong to this checkout's owned stack/app sessions. Backend probes `/auth/can-register`, frontend `/auth`, workers `/health/status`. Worker readiness additionally proves a recent **owned Temporal `main` workflow poller** in the configured namespace. Temporal TCP/HTTP reachability alone is insufficient; polling is not proof of all activities or social publishing.
- On failure, inspect `make logs`/`make status` and report the failing stage. Startup rollback stops only components newly started by that invocation; previously healthy owned components may remain running. Do not assume everything stopped, silently retry, or bypass ownership/configuration/foreign-port checks.
- `docker-compose.light.yaml` and `scripts/dev/config.py`, `scripts/dev/infra.py`, `scripts/dev/controller.py` define the implementation. The fixed Compose identity is `postiz-light`; a conflicting checkout/configuration must be inspected, not adopted or recreated.
- Light infrastructure publishes loopback defaults: PostgreSQL **15433**, Redis **6380**, Temporal **7234**. Defaults for backend/workers are **3000/3002**, configurable through `PORT`/`ORCHESTRATOR_PORT`; frontend's existing script fixes **4200**. Match actual configured values; do not stop original PostgreSQL **15432** or Redis **6379** to resolve conflicts. App servers may bind beyond loopback; use a trusted local network.
- Temporal uses persistent SQLite in this **development-only** stack, not extra Temporal PostgreSQL/Elasticsearch services. Workers are real, not mocked; this is not a production deployment recipe. Explicit namespace configuration must match `LIGHT_TEMPORAL_NAMESPACE` (default `default`). The backend initializer owns the `organizationId` and `postId` **Text** search attributes; no extra manual registration is normally needed.

## Hot reload and explicit type checking

- Shared default `DEV_COMPILER=tsc` uses the original server watch scripts. `DEV_COMPILER=swc` is an opt-in for backend/workers only; frontend remains Next dev and production compilation is unchanged. A checkout's local Podman/SWC choice is not the default for all checkouts.
- SWC transpiles without type checking; it does not launch a background checker. App/shared-library reload and real readiness must be tested separately from compilation. Use reversible edits only within authorized work; restore test markers exactly.
- For requested server type checks, consult the current resource budget first and run separately from watchers, sequentially: `pnpm run typecheck:backend`, then `pnpm run typecheck:orchestrator` (or `pnpm run typecheck`, which stops on the first failure).
- Both full checks have passed with an explicitly scoped 4 GiB V8 heap; that is not permission to increase memory automatically. Only with an approved budget, examples are `NODE_OPTIONS=--max-old-space-size=4096 pnpm run typecheck:backend` followed by `NODE_OPTIONS=--max-old-space-size=4096 pnpm run typecheck:orchestrator`. Never set a global heap override or claim a 6 GiB safety guarantee. A typecheck/build does not prove runtime behavior.

## Verify affected flows and report

- For affected browser flows, follow `AGENTS.md`: use Chrome DevTools MCP in a dedicated **development** tab, check the UI, console errors, and network requests. Use configured local URLs (default frontend `http://localhost:4200/auth`); never inspect personal tabs or production. Report unavailable tooling as a blocker, not a passing check.
- Use only authorized synthetic accounts/files for login/upload checks; do not trigger external social publishing. Expect possible external frontend traffic; do not claim isolation from all external requests merely because light datastores are local.
- Report exact commands/scope/compiler, readiness and worker evidence, reload/typecheck results, UI/console/network findings or checks not run, blockers/resource stops, and changed files/configuration. Distinguish static checks from runtime evidence.
- State what remains **running versus stopped**, including reused components and partial failures. Do not silently shut down an environment the user wanted left running.
