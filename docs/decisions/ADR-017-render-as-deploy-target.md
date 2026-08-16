# ADR-017 — Render replaces Fly.io as the deploy target

**Status:** accepted · Phase 3 · 2026-08-16
**Supersedes:** the hosting half of Phase 1 Step 11 (`fly.toml`, now deleted)
**Relates to:** [ADR-009](ADR-009-readiness-probe-scope.md) and
[ADR-010](ADR-010-redis-fail-open-and-readiness.md) (both reason about `/readyz` in terms of what Fly
does with it; the reasoning survives the move, the platform behaviour does not),
[ADR-016](ADR-016-streaming-session-lifetime.md) (the Postgres pool this resizes)

## Context

The Fly.io free allowance ran out. The gateway needs a host; nothing else about the deployment moves —
Supabase, Upstash and Vercel stay exactly where they are, and the only thing they notice is a new
hostname for the gateway.

Render's free web-service tier is the obvious replacement, and it is not a drop-in. Three of its
constraints touch decisions this repo had already made and written down:

- **No pre-deploy command on free.** `fly.toml` ran `alembic upgrade head` as a `release_command`, in
  a one-off machine, with the rollout aborting on a non-zero exit. Render's equivalent is a paid
  feature, and free instances additionally have no shell and no one-off jobs — so there is no
  out-of-band place to put a migration at all.
- **The platform picks the port.** Render assigns `PORT` and expects the server to bind it on
  `0.0.0.0`. The image's `CMD` hardcoded `--port 8000` in exec form, where nothing expands.
- **No Tokyo region.** Render has five (Oregon, Ohio, Virginia, Frankfurt, Singapore).
  `fly.toml`'s `primary_region = "nrt"` existed specifically to sit next to a Supabase project in
  `ap-northeast-1`, on the argument that an app a continent from its database is slow in a way no
  tuning fixes. That co-location is no longer available at any price.

## Decision

**Render free, in Singapore, with migrations in the container's start command and deploys triggered
by CI through a deploy hook.**

- `render.yaml` is checked in and authoritative, the way `fly.toml` was.
- `dockerCommand` is `sh /srv/app/start.sh`, and the chain lives in [`start.sh`](../../start.sh).
  **Render does not run the Docker Command through a shell** — it interpolates environment variables
  into the string, splits the result into argv, and execs it. Neither obvious spelling survives that,
  and both were found by deploying them: `alembic upgrade head && exec uvicorn …` reaches alembic
  with `&&` as an argument (exit 2), and `/bin/sh -c "alembic upgrade head && …"` reaches an inner
  shell as one quoted word, because the quote characters are kept as literal text rather than
  consumed as syntax (`/bin/sh: 1: alembic upgrade head && …: not found`, exit 127).
- `healthCheckPath: /readyz`, unchanged in intent from ADR-009.
- `autoDeploy: false`. The `deploy` job in `.github/workflows/ci.yml` calls the service's deploy hook,
  so `needs: [lint, test, frontend]` stays in front of every deploy.
- One uvicorn worker (`WEB_CONCURRENCY=1`), down from two.
- `PORT` is read from the environment by both the image's default `CMD` and the Render command, and
  is *not* declared in `render.yaml` — Render assigns it and its own value wins over an `envVar` of
  the same name, so declaring one only creates a number that looks authoritative and is not.
- `--workers` and `--forwarded-allow-ips` are set as `WEB_CONCURRENCY` and `FORWARDED_ALLOW_IPS`
  environment variables rather than command-line flags. uvicorn reads both natively when the flags
  are absent, and `render.yaml` is a better home for a deployment's concurrency and trust settings
  than a string inside a script.

## Why

**The start command is where the migration goes because nowhere else exists.** The property worth
preserving was never "migrations run in a separate machine" — it was "a broken migration never reaches
something serving traffic." That survives the move intact, by a different route: `alembic/env.py`
resolves `DATABASE_URL` through `app/config.py`, so a bad migration or a missing variable fails before
uvicorn binds, the health check never passes, Render cancels the deploy, and the previous instance
keeps serving. The free plan runs a single instance, so the concurrency argument for a release
command — two machines racing to apply one revision — has no target either.

**A script rather than a one-liner, because the host's command field is not a shell.** Two deploys
were spent discovering that, and the second failure mode — a quoted string handed to `sh -c` with its
quotes intact — is the kind that reads as a broken image rather than a broken configuration. Moving
the sequence into `start.sh` reduces `dockerCommand` to two bare tokens with nothing left to
misparse, puts the reasoning next to the code it governs, and makes the exact production start path
runnable locally: `docker run … llm-gateway:local sh /srv/app/start.sh`. The image's own `CMD` stays
serve-only, so `make docker-run` and docker-compose still do not migrate a database as a side effect
of starting a container.

**One worker, because the arithmetic reversed.** Two was right for a whole shared core. On 0.1 CPU and
512MB, a second worker splits a tenth of a core, doubles the resident set, and doubles the Postgres
pool with it (`pool_size 5 + max_overflow 5` per process). The app is async throughout; one event loop
still serves concurrent requests while they wait on a provider, which is what this workload is.

**Singapore is a compromise, and naming it as one is the point.** It is the closest Render region to
`ap-northeast-1`, and it is still a different continent's worth of round trip. Every request makes
several trips to Postgres, so this is a real, permanent latency cost accepted in exchange for a host
that exists. The alternative — moving the Supabase project to `ap-southeast-1` — means a new project,
a re-run of every migration, and re-registering every user, which is a bigger change than the one this
ADR is about.

**Auto-deploy off is a correctness setting, not a preference.** Render's push-triggered deploys know
nothing about CI. Left on, they would ship past a red test suite, which is the one thing the `deploy`
job's `needs:` exists to prevent.

## Consequences

- **A failed migration now looks like a failed health check**, not an aborted release. Same outcome,
  different symptom, and the Render dashboard's deploy log is where the `alembic` traceback appears.
- **The migration re-runs on every cold start.** A no-op `upgrade head` is one Postgres round trip, but
  it is now on the critical path of waking up — and an unreachable Supabase during a wake-up means the
  service does not come back at all, rather than coming back and failing `/readyz`. The escape hatch is
  editing `dockerCommand` in the dashboard to drop the `alembic` half.
- **`/readyz` now restarts instances, not just drains them.** Render pauses traffic after 15s of
  consecutive failures and restarts the instance after 60s. ADR-010's argument for keeping Redis out of
  the probe gets stronger, not weaker: a fail-open dependency able to fail this probe would now
  manufacture a restart loop.
- **Cold starts got worse.** Fly suspended and resumed without paying Python's startup; Render spins
  down after 15 idle minutes and takes about a minute to come back. The risk register
  ([development-plan.md](../../doc/reference/development-plan.md) §5) already accepts cold starts in
  principle; this is the same acceptance at a worse number. Ping `/healthz` before a demo.
- **Rollback is shallower.** Render's free plan keeps the two previous deploys. `fly deploy --image`
  could reach any of them.
- **Environment variable values are visible in the dashboard**, where `fly secrets list` showed names
  and digests only. Nothing changes about what is stored; it changes who can read it back.
- **`docs/deploy.md` §3 and §6 were rewritten** and every Supabase-related warning in §1 kept verbatim,
  because none of it was ever about Fly.
