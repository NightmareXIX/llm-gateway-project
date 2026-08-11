# Deploying the gateway

Phase 1, Step 11. Four managed services, all on free tiers: **Supabase** (Postgres + Auth),
**Upstash** (Redis), **Fly.io** (the FastAPI service), **Vercel** (the Next.js frontend).

Run the steps in order. Several of them fail in ways that are only obvious in hindsight, and each
of those has a note saying so.

---

## 0. Before you start

Postgres must be up for the integration suite (`docker compose up -d postgres`), and on Windows
`make` needs installing first — `winget install ezwinports.make`, then open a new shell so the PATH
change takes effect.

```bash
make lint && make typecheck && make test    # must be green before anything ships
make docker-build                           # the image the deploy will build, built locally first
```

Install [`flyctl`](https://fly.io/docs/flyctl/install/) and sign in:

```bash
fly auth login
```

---

## 1. Supabase — Postgres and Auth

Create a project at [supabase.com](https://supabase.com). Then, in order:

**Auth → Signing keys.** Confirm the project uses **asymmetric** keys (ES256 or RS256). The gateway
fetches the JWKS and refuses HS256 outright — [`app/auth/jwt.py`](../app/auth/jwt.py) checks the
algorithm *before* looking up a key, because accepting a symmetric algorithm alongside a
JWKS-sourced public key is the classic algorithm-confusion hole (ADR-007). A legacy shared-secret
project will 401 every request with no obvious cause.

**Auth → Providers → Email.** Turn **Confirm email** on. It is the standing configuration in every
environment.

**Auth → SMTP.** Configure a custom SMTP sender. Supabase's built-in one is rate-limited to a
handful of messages an hour and you will hit it during ordinary testing, not in production.

**Project Settings → Database → Connection string → URI.** Take the **session pooler** string
(port `5432`) — not the direct connection, which is IPv6-only without the paid IPv4 add-on — and
make three edits:

```
postgresql+asyncpg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?ssl=require
^^^^^^^^^^^^^^^^^^^^                ^^^^^^^^^^                                                  ^^^^^^^^^^^
1. scheme                           2. percent-encoded                                          3. ssl, NOT sslmode
```

1. **Scheme:** `postgresql://` → `postgresql+asyncpg://`.

2. **Password:** percent-encode it. A raw `@` truncates the host and you get a DNS failure naming a
   host you never typed. `[uri]::EscapeDataString($pw)` in PowerShell, `urllib.parse.quote` in
   Python. Leave the username as `postgres.<ref>` — the dotted form is how Supavisor identifies the
   tenant, and "tidying" it to plain `postgres` gives `Tenant or user not found`.

3. **`?ssl=require`, and delete any `?sslmode=require` Supabase gave you.** SQLAlchemy passes
   unknown query parameters straight through to the driver, and asyncpg has no `sslmode` keyword —
   it has `ssl`. Verified against SQLAlchemy 2.0.51:

   | Query string | Reaches asyncpg as | Result |
   |---|---|---|
   | `?sslmode=require` | `sslmode=` | `TypeError: connect() got an unexpected keyword argument 'sslmode'` |
   | `?ssl=require` | `ssl=` | correct — TLS required |
   | *(omitted)* | — | connects, but asyncpg defaults to `prefer`: silent plaintext fallback |

   This does **not** fail when the engine is built. It fails on the first connection — which means it
   fails inside the release command, during the deploy.

> **If you use the transaction pooler (port 6543) instead**, you must also set
> `DB_DISABLE_PREPARED_STATEMENTS=true`. That pooler hands each transaction a different upstream
> connection, which breaks asyncpg's prepared-statement cache outright —
> `DuplicatePreparedStatementError` on every concurrent request, not a graceful degradation. See
> [`app/config.py`](../app/config.py).

**Note the region in the pooler hostname** (`aws-0-<region>.pooler.supabase.com`). Step 3 has to put
the Fly app next to it.

### Test the string before it becomes a secret

Exercises the real path — `create_db_engine`, the dialect, TLS, the pooler — so a failure here is one
you would otherwise meet as a dead release command:

```powershell
$env:PROBE_URL = $dbUrl
.venv\Scripts\python.exe -c @"
import asyncio, os
from sqlalchemy import text
from app.config import Settings
from app.db.session import create_db_engine
s = Settings(DATABASE_URL=os.environ['PROBE_URL'], REDIS_URL='redis://x',
             SUPABASE_URL='https://x.supabase.co', SUPABASE_JWT_AUDIENCE='authenticated',
             GROQ_API_KEY='x', ENCRYPTION_KEY='x')
async def main():
    e = create_db_engine(s)
    async with e.connect() as c:
        print('connected:', (await c.execute(text('select version()'))).scalar_one()[:40])
    await e.dispose()
asyncio.run(main())
"@
Remove-Item Env:\PROBE_URL
```

| Failure | Cause |
|---|---|
| `TypeError: ... unexpected keyword argument 'sslmode'` | `?sslmode=require` left on |
| `password authentication failed` | wrong password, or `@`/`:` not percent-encoded |
| `getaddrinfo failed` | wrong region, or an unencoded `@` truncated the host |
| `Tenant or user not found` | username is plain `postgres` instead of `postgres.<ref>` |

---

## 2. Upstash — Redis

Create a database at [upstash.com](https://upstash.com) and copy the `rediss://` URL.

Nothing reads it in Phase 1 — quota, caching and circuit breakers land in Phase 3. It is set now so
the configuration surface never shifts underneath a deploy, and so `/readyz` never has to change
shape later. See [ADR-009](decisions/ADR-009-readiness-probe-scope.md) for why the readiness probe
does not check it.

---

## 3. Fly.io — the gateway

**Set `primary_region` in `fly.toml` to match the Supabase region** you noted in step 1, before
anything else. `ap-northeast-1` → `nrt`, `us-east-1` → `iad`, `eu-central-1` → `fra`
(`fly platform regions` lists them all). Every request makes several round trips to Postgres; an app
a continent away from its database is slow in a way no amount of tuning fixes.

`fly.toml` is committed and authoritative, so create the app with `apps create` rather than
`fly launch` — `launch` rewrites `fly.toml` from its own template and drops the comments explaining
why each setting is what it is:

```bash
fly apps create llm-gateway
```

App names are a **global** namespace on Fly, so `llm-gateway` may be taken. If it is, pick another
and change `app = ` in `fly.toml` to match — the two must agree or `fly deploy` targets the wrong
app.

Everything below assumes the app exists. `fly secrets set` against a name Fly does not know fails
with `Could not find App "<name>"`, which is what you get for running step 3 out of order.

### Set every secret before the first deploy

This is the step that catches people. `fly.toml` runs `alembic upgrade head` as a release command,
and [`alembic/env.py`](../alembic/env.py) resolves `DATABASE_URL` through `app/config.py` — which
validates the *whole* settings object. A missing `GROQ_API_KEY` fails the migration exactly as
loudly as it would fail the app, before any machine starts.

```bash
fly secrets set \
  DATABASE_URL='postgresql+asyncpg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres' \
  REDIS_URL='rediss://default:<password>@<host>.upstash.io:6379' \
  SUPABASE_URL='https://<ref>.supabase.co' \
  SUPABASE_JWT_AUDIENCE='authenticated' \
  REQUIRE_VERIFIED_EMAIL='true' \
  GROQ_API_KEY='gsk_...' \
  ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

`ENV=prod` is not here — it is in `fly.toml`'s `[env]` block, because it is not a secret and it
belongs in version control. It is what disables the `/docs` route.

Single quotes around every value: connection strings contain `$` and `&`, and your shell will eat
them otherwise.

### Deploy

```bash
fly deploy
fly logs
```

A healthy first deploy shows the release command applying `0001_initial_schema` and `0002`, then one
`startup.complete` JSON line per uvicorn worker carrying the loaded slot table.

---

## 4. Vercel — the frontend

Import the repository at [vercel.com](https://vercel.com/new).

- **Root directory:** `frontend`
- **Framework preset:** Next.js (auto-detected)

Environment variables:

| Variable | Value | Exposure |
|---|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | `https://<ref>.supabase.co` | Browser. Public by design. |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | the anon key | Browser. Public by design. |
| `GATEWAY_URL` | `https://llm-gateway.fly.dev` | **Server only.** |

`GATEWAY_URL` is read server-side by [`next.config.ts`](../frontend/next.config.ts)'s rewrite. The
browser only ever calls `/api/gw/*` on the Vercel origin, which keeps every request same-origin —
no preflight, no `Access-Control-Allow-*` to get wrong, and no CORS middleware in
[`app/main.py`](../app/main.py). Do not "fix" this by pointing the browser at the Fly URL directly;
that trades a working setup for a CORS configuration.

Never set `SUPABASE_SERVICE_ROLE_KEY` here. The frontend has no use for it and Vercel env vars are
readable by anyone with project access.

---

## 5. Supabase redirect URLs

**Auth → URL Configuration.** Set the site URL to the Vercel origin and add both to redirect URLs:

```
https://<your-app>.vercel.app
https://<your-app>.vercel.app/auth/callback
```

Skip this and confirmation emails link to `localhost:3000`, which looks like broken email delivery
and is not.

---

## 6. GitHub Actions

```bash
fly tokens create deploy -x 999999h
```

Add the output as the `FLY_API_TOKEN` repository secret (Settings → Secrets and variables →
Actions). `.github/workflows/ci.yml` then runs lint, tests and frontend checks on every push and
PR, and deploys on `main`.

Vercel deploys itself through its own Git integration — there is no Vercel token in this repo.

---

## 7. Smoke test

```bash
API=https://llm-gateway.fly.dev

curl -s  $API/healthz                       # {"status":"ok"}
curl -s  $API/readyz                        # {"status":"ok","database":"ok"}
curl -si $API/v1/me | head -20              # 401, error envelope, X-Request-ID header
curl -s  $API/docs -o /dev/null -w '%{http_code}\n'   # 404 — ENV=prod hides it
```

Then in the browser, against the Vercel URL — this is the Phase 1 definition of done:

1. Register, confirm the email, log in.
2. Send a message; an answer comes back with the model indicator under it.
3. Refresh; the conversation is still there.
4. In a second browser (or a private window with another account), that conversation is invisible.

Finally, confirm the usage row exists — Supabase SQL editor:

```sql
select provider, model, tokens_in, tokens_out, latency_ms, status, created_at
from requests order by created_at desc limit 5;
```

---

## Operating notes

**Cold starts.** `min_machines_running = 0` lets machines suspend when idle, so the first request
after a quiet spell pays a wake-up. Accepted deliberately — it is in the risk register
([development-plan.md](../doc/reference/development-plan.md) §5). Before a demo:
`curl https://llm-gateway.fly.dev/healthz`.

**Rolling back.** `fly releases` then `fly deploy --image <previous>`. Note that this does **not**
roll back a migration; Alembic downgrades are separate and deliberate.

**Reading logs.** Everything is JSON with `request_id` and `user_id` bound. To trace one user report:
`fly logs | grep <request-id>` — the id in the error envelope they quote is the same one.

**Rotating the Groq key.** `fly secrets set GROQ_API_KEY=...` restarts the machines. A revoked key
surfaces as a clean 502 with a `requests` row at `status='error'`, never a traceback.

**Database migrations on a live deploy.** They run before the new version takes traffic, which means
a migration must be compatible with the *old* code for the length of the rollout. Additive changes
only, or a two-deploy expand/contract.
