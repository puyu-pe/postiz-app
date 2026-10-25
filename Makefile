CONTAINER_ENGINE ?= docker
-include .make.local
SCOPE ?= full
DEV_COMPILER ?= tsc
# Supported controller/private app inputs only: no global export. Freeze raw values into
# simple variables before export, preserving CLI > .make.local > environment.
# Leave undefined inputs absent so Python retains its defaults/required checks.
# Delay $(value ...) until eval parses :=; its result is never expanded again.
$(foreach input,CONTAINER_ENGINE SCOPE DEV_COMPILER \
  LIGHT_POSTGRES_USER LIGHT_POSTGRES_PASSWORD LIGHT_POSTGRES_DB \
  LIGHT_POSTGRES_PORT LIGHT_REDIS_PORT LIGHT_TEMPORAL_PORT LIGHT_TEMPORAL_NAMESPACE \
  DATABASE_URL REDIS_URL TEMPORAL_ADDRESS TEMPORAL_NAMESPACE TEMPORAL_TLS TEMPORAL_API_KEY \
  PORT ORCHESTRATOR_PORT FRONTEND_URL NEXT_PUBLIC_BACKEND_URL BACKEND_INTERNAL_URL \
  MAIN_URL NEXT_PUBLIC_OVERRIDE_BACKEND_URL STORAGE_PROVIDER UPLOAD_DIRECTORY \
  NEXT_PUBLIC_UPLOAD_STATIC_DIRECTORY JWT_SECRET NOT_SECURED,\
  $(if $(filter undefined,$(origin $(input))),,\
    $(eval override $(input) := $$(value $(input)))\
    $(eval export $(input))))

.PHONY: help up down logs status
help:
	@printf '%s\n' 'Postiz lightweight Linux development (host Node.js >=22.12 <23, pnpm 10.6.1)' \
	  '  make up [SCOPE=full|backend|workers]  Start isolated light services and apps in background (default: full)' \
	  '  make down                              Stop only controller-owned apps and light services; preserve data/logs' \
	  '  make logs                              Show recent app logs' \
	  '  make status                            Show recorded app process ownership' \
	  'Scopes: backend=backend; workers=backend+orchestrator, no frontend; full=backend+orchestrator+frontend.' \
	  'No extension. Scope narrowing never stops running apps. make up never prepares or migrates the database.' \
	  'Set CONTAINER_ENGINE=podman in ignored .make.local to select Podman explicitly.' \
	  'Optional private app settings: copy .dev.env.example to ignored .dev.env.local (loaded only by up).' \
	  'DEV_COMPILER=tsc|swc (default: tsc) selects backend/workers only; SWC does not typecheck.' \
	  'Use make down before changing a live server compiler; frontend is unchanged.'

up:
	@python3 scripts/dev/controller.py up --scope-env

down:
	@python3 scripts/dev/controller.py down

logs:
	@python3 scripts/dev/controller.py logs

status:
	@python3 scripts/dev/controller.py status
