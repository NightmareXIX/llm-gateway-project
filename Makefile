# Task runner. `make <target>`; see docs/deploy.md for the deployment ones.
#
# Windows: `make` is not installed by default and PowerShell will report it as
# an unrecognized cmdlet. One of:
#
#     winget install ezwinports.make      # 4.4.1, standalone, no MSYS runtime
#     choco install make
#
# then open a new shell — the installer edits PATH and the running session does
# not see it. The recipes below are plain commands, so they run the same under
# cmd.exe as under sh; nothing here needs a POSIX shell.
#
# Targets that touch the database (`test`, `coverage`, `migrate`) need Postgres
# up: `docker compose up -d postgres`. Without it the unit suite still passes
# and every integration test errors on a refused connection.

.PHONY: install dev test coverage lint typecheck migrate revision record-fixtures \
        docker-build docker-run deploy \
        frontend-install frontend-dev frontend-build frontend-lint frontend-test

PYTHON ?= python
MSG ?= change me
ARGS ?=

install:
	$(PYTHON) -m pip install -e ".[dev]"

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest

# Separate from `test` on purpose: the report is for when someone is asking
# "what is untested?", not a gate every run has to clear.
coverage:
	pytest --cov --cov-report=term-missing

lint:
	ruff check app tests scripts
	ruff format --check app tests scripts

typecheck:
	mypy

migrate:
	alembic upgrade head

# make revision MSG="add users table"
revision:
	alembic revision --autogenerate -m "$(MSG)"

# The ONLY thing in this repo that calls a live provider. Run it once with working
# keys; the committed fixtures serve every test forever after. A provider whose key
# is still the .env.example placeholder is skipped rather than failing the run.
# make record-fixtures ARGS="--force"
# make record-fixtures ARGS="--provider gemini --only success"
record-fixtures:
	$(PYTHON) -m scripts.record_fixtures $(ARGS)

# --------------------------------------------------------------------------- #
# Deployment — see docs/deploy.md.
# --------------------------------------------------------------------------- #
IMAGE ?= llm-gateway:local

# The production image differs from what `make dev` runs — two workers, no
# reload, non-root, no working-tree mounts. An image nobody builds until deploy
# day is an image that fails on deploy day.
docker-build:
	docker build -t $(IMAGE) .

# The built image against the compose database. `host.docker.internal` because
# this container is not on the compose network; DATABASE_URL from .env points at
# 127.0.0.1, which inside a container means the container itself.
docker-run:
	docker run --rm -p 8001:8000 --env-file .env \
	  -e DATABASE_URL=postgresql+asyncpg://gateway:gateway@host.docker.internal:5432/gateway \
	  $(IMAGE)

# Migrations run inside this, via fly.toml's release_command.
deploy:
	flyctl deploy --remote-only

# --------------------------------------------------------------------------- #
# frontend/ — Next.js. Needs the API running (`make dev`) to be useful.
# --------------------------------------------------------------------------- #
frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build

frontend-lint:
	cd frontend && npm run lint && npm run typecheck

frontend-test:
	cd frontend && npm test
