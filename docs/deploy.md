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

   **Settle on the password before building the URL.** Rotating it afterwards invalidates the
   secret you already set, and the deploy that reveals this is three minutes long. Read it with
   `Read-Host -AsSecureString` rather than pasting into a double-quoted string: PowerShell expands
   `$` inside `"..."`, so a password containing `$&` becomes a different password, silently.

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
             GROQ_API_KEY='x', GEMINI_API_KEY='x', OPENROUTER_API_KEY='x',
             ENCRYPTION_KEY='x')
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
| `InvalidPasswordError: password authentication failed for user "postgres"` | wrong password — including a password rotated after the URL was built. Not a username problem: the tenant resolved, and Supavisor strips the `.<ref>` suffix before passing the request upstream, which is why the message names plain `postgres` |
| `Tenant or user not found` | username *is* plain `postgres` — the request never reached Postgres at all |
| `getaddrinfo failed` | wrong region, or an unencoded `@` truncated the host |

Run this probe again after **any** change to the password or the URL. It costs two seconds; the
deploy that would otherwise tell you the same thing costs three minutes and leaves a failed release
in the app's history.

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
fly apps create llm-gateway-sed
```

App names are a **global** namespace on Fly — plain `llm-gateway` is taken, which is why this one
carries a suffix. Whatever you pick must match `app = ` in `fly.toml`, or `fly deploy` targets the
wrong app (or none).

Everything below assumes the app exists. `fly secrets set` against a name Fly does not know fails
with `Could not find App "<name>"`, which is what you get for running step 3 out of order.

### Set every secret before the first deploy

This is the step that catches people. `fly.toml` runs `alembic upgrade head` as a release command,
and [`alembic/env.py`](../alembic/env.py) resolves `DATABASE_URL` through `app/config.py` — which
validates the *whole* settings object. A missing `GROQ_API_KEY` fails the migration exactly as
loudly as it would fail the app, before any machine starts.

> **Phase 2 added two.** `GEMINI_API_KEY` and `OPENROUTER_API_KEY` are required `Settings` fields
> as of Phase 2 Step 1, even though both providers are `enabled: false` in `config/providers.yaml`
> until Step 6. On an app that already exists, set them **before** the deploy that carries this
> change — otherwise the release command fails config validation and the rollout aborts, which looks
> like a broken migration and is not one.

Nine secrets, and **only six of them are the values from your local `.env`**. Copying that file
wholesale points production at your laptop:

| Secret | Value |
|---|---|
| `SUPABASE_URL`, `SUPABASE_JWT_AUDIENCE`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | same as `.env` |
| `DATABASE_URL` | the Supabase pooler string from step 1 — `.env` holds `127.0.0.1:5432`, the compose container |
| `REDIS_URL` | Upstash, from step 2 — `.env` holds `localhost:6379` |
| `ENCRYPTION_KEY` | **generate a fresh one.** Nothing is encrypted with it until Phase 6, so a new key is free now; sharing dev's means a leaked dev `.env` also decrypts production, and rotating later is a migration |
| `REQUIRE_VERIFIED_EMAIL` | type `true` **literally**. It has a default in `Settings`, so it is frequently absent from `.env` — scripting it out of that file yields an empty string, and an empty string is not a boolean |

> That last row is a real failure, not a hypothetical. A shell that interpolates a missing key as
> `""` produces `fly secrets set REQUIRE_VERIFIED_EMAIL=""`, and the release command dies with
> `Input should be a valid boolean, unable to interpret input`. Anything with a default in
> `app/config.py` is a candidate for this; only the five in the first row are safe to read out of
> `.env` programmatically, because only those are always present.

`ENV` is deliberately absent — it lives in `fly.toml`'s `[env]` block as `prod`, and a secret of the
same name would override it.

```bash
fly secrets set \
  DATABASE_URL='postgresql+asyncpg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?ssl=require' \
  REDIS_URL='rediss://default:<password>@<host>.upstash.io:6379' \
  SUPABASE_URL='https://<ref>.supabase.co' \
  SUPABASE_JWT_AUDIENCE='authenticated' \
  REQUIRE_VERIFIED_EMAIL='true' \
  GROQ_API_KEY='gsk_...' \
  GEMINI_API_KEY='...' \
  OPENROUTER_API_KEY='sk-or-v1-...' \
  ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

Then confirm all nine names landed — Fly never shows values back:

```bash
fly secrets list
```

Names and digests only, so `fly secrets list` cannot tell you a value is *wrong* — only that it is
present. An empty string looks identical to a correct one here. The release command is what catches
it, which is the argument for having migrations run there rather than at app start.

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
| `GATEWAY_URL` | `https://llm-gateway-sed.fly.dev` | **Server only.** |

**Set these in the Vercel dashboard, not in `frontend/.env.local`.** That file is gitignored and
never leaves your machine — it configures `make frontend-dev` and nothing else. Filling it in does
not configure the deployment.

> **Get `GATEWAY_URL` exactly right.** Fly's app namespace is global, so a near-miss hostname is
> usually a *live app belonging to somebody else* rather than a DNS error you would notice.
> [`lib/api.ts`](../frontend/lib/api.ts) attaches the user's Supabase JWT to every request that goes
> through the rewrite, so a wrong host here sends valid session tokens for your project to a
> stranger's server, silently, on every authenticated request. Verify before pasting:
> `curl https://<app>.fly.dev/healthz` must return `{"status":"ok"}` — a 404 means it is not yours.

`GATEWAY_URL` is read server-side by [`next.config.ts`](../frontend/next.config.ts)'s rewrite. The
browser only ever calls `/api/gw/*` on the Vercel origin, which keeps every request same-origin —
no preflight, no `Access-Control-Allow-*` to get wrong, and no CORS middleware in
[`app/main.py`](../app/main.py). Do not "fix" this by pointing the browser at the Fly URL directly;
that trades a working setup for a CORS configuration.

Never set `SUPABASE_SERVICE_ROLE_KEY` here. The frontend has no use for it and Vercel env vars are
readable by anyone with project access.

---

## 5. Supabase redirect URLs

**Authentication → URL Configuration.**

**Site URL:** `https://<your-app>.vercel.app`

**Redirect URLs:**

```
https://<your-app>.vercel.app/**
http://localhost:3000/**
```

Keep the localhost entry so `make frontend-dev` signups keep working. Use the `/**` wildcard rather
than a bare `/auth/callback`: [`LoginForm.tsx`](../frontend/components/LoginForm.tsx) appends
`?next=…` to the redirect, and an exact-path entry rejects the querystring. If Vercel Preview
deployments are enabled, add `https://<project>-*-<username>.vercel.app/**` too — preview hostnames
are generated per branch and the auth flow builds its redirect from the request origin.

**The symptom when this is missed** is a confirmation link landing on
`http://localhost:3000/?code=…`. Two things to read off that URL: the host is Supabase's default
Site URL, and the path is `/` rather than `/auth/callback` — Supabase does not merely swap the host
on a non-allowlisted redirect, it discards the requested target entirely and substitutes the Site
URL. A wrong *host* with the right path would mean something else.

It looks like broken email delivery and is not; the mail arrived correctly, carrying the wrong
destination.

**Recovering an account stuck this way** needs no re-registration. In the same browser that signed
up, open `https://<your-app>.vercel.app/auth/callback?code=<the code from the bad link>` — the PKCE
verifier is in a cookie on that origin, so the exchange completes normally.

---

## 6. GitHub Actions

Everything up to here was deployed by hand. This is what makes the next deploy automatic.

### 6.1 Push the repository

Nothing in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) can run until GitHub has the
code:

```bash
git push -u origin main
```

The first run will show `deploy` failing — the token does not exist yet. `lint`, `test` and
`frontend` should be green.

### 6.2 Mint a deploy token

Run this from the repo root so it picks the app up from `fly.toml`:

```bash
fly tokens create deploy -x 8760h
```

It prints one line starting `FlyV1 ...`. **Copy the whole thing, including the `FlyV1 ` prefix and
the trailing space-separated parts** — it is one value, and truncating it produces an
authentication error at deploy time rather than a parse error now.

`-x` is the expiry. A year is a reasonable default for a portfolio project; the token is scoped to
this one app and can only deploy, so a leak cannot read secrets or touch other apps. It is shown
exactly once.

### 6.3 Store it as a repository secret

GitHub → the repo → **Settings → Secrets and variables → Actions → New repository secret**.

| Field | Value |
|---|---|
| Name | `FLY_API_TOKEN` |
| Secret | the whole `FlyV1 …` string |

The name must match exactly — [`ci.yml`](../.github/workflows/ci.yml) reads
`${{ secrets.FLY_API_TOKEN }}`, and a missing secret becomes an empty string rather than an error,
so a typo surfaces as `Error: No access token available` in the deploy job.

Use a **repository** secret, not an environment or organization one, unless you have a reason —
environment secrets need a matching `environment:` key in the job, which this workflow does not set.

### 6.4 What now happens on every push

| Job | Runs on | Does |
|---|---|---|
| `lint` | push + PR | `ruff check`, `ruff format --check`, `mypy` |
| `test` | push + PR | `pytest --cov` against a `postgres:16` service container |
| `frontend` | push + PR | `npm ci`, lint, typecheck, `vitest` |
| `deploy` | **push to `main` only** | `flyctl deploy --remote-only` |

`deploy` has `needs: [lint, test, frontend]`, so a red test suite stops the deploy rather than
shipping past it. `--remote-only` builds the image on Fly's builder, so the runner needs no Docker
daemon. Migrations run inside that deploy, through `fly.toml`'s `release_command` — the same path
step 3 used, with the same abort-on-failure behaviour.

Verify by pushing anything to `main` and watching the **Actions** tab. A green `deploy` job means
`fly releases` has a new entry.

Vercel is not deployed from here — its own Git integration builds the frontend on push, which is one
fewer long-lived token for this repository to hold.

---

## 7. Smoke test

PowerShell — use `Invoke-RestMethod`/`Invoke-WebRequest` rather than `curl`, which is an alias for
`Invoke-WebRequest` in Windows PowerShell 5.1 and the real `curl.exe` in PowerShell 7. The
`-SkipHttpErrorCheck` flag is what stops a deliberate 401 or 404 from throwing:

```powershell
$API = 'https://llm-gateway-sed.fly.dev'

Invoke-RestMethod "$API/healthz"            # status : ok
Invoke-RestMethod "$API/readyz"             # status : ok, database : ok

$r = Invoke-WebRequest "$API/v1/me" -SkipHttpErrorCheck
$r.StatusCode                               # 401
$r.Headers['x-request-id']                  # present
$r.Content                                  # the error envelope

(Invoke-WebRequest "$API/docs" -SkipHttpErrorCheck).StatusCode   # 404 — ENV=prod hides it
```

bash:

```bash
API=https://llm-gateway-sed.fly.dev

curl -s  $API/healthz                       # {"status":"ok"}
curl -s  $API/readyz                        # {"status":"ok","database":"ok"}
curl -si $API/v1/me | head -20              # 401, error envelope, X-Request-ID header
curl -s  $API/docs -o /dev/null -w '%{http_code}\n'   # 404 — ENV=prod hides it
```

Four things are being checked, and the third is the interesting one. `/healthz` proves the process
is up; `/readyz` proves it reached Supabase through the pooler, which is the only part local testing
cannot demonstrate; `/docs` returning 404 proves `ENV=prod` took effect. The `/v1/me` call proves the
error envelope survived deployment — and the `x-request-id` header must equal the `request_id`
inside the body, which is the promise that makes a user's bug report traceable to a log line.

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
`curl https://llm-gateway-sed.fly.dev/healthz`.

**Rolling back.** `fly releases` then `fly deploy --image <previous>`. Note that this does **not**
roll back a migration; Alembic downgrades are separate and deliberate.

**Reading logs.** Everything is JSON with `request_id` and `user_id` bound. To trace one user report:
`fly logs | grep <request-id>` — the id in the error envelope they quote is the same one.

**Rotating a provider key.** `fly secrets set GROQ_API_KEY=...` (or `GEMINI_API_KEY` /
`OPENROUTER_API_KEY`) restarts the machines. A revoked key surfaces as a clean 502 with a `requests`
row at `status='error'`, never a traceback — and from Phase 2 Step 5 onward, as a failover to
whichever provider is still live rather than as an error at all.

**Database migrations on a live deploy.** They run before the new version takes traffic, which means
a migration must be compatible with the *old* code for the length of the rollout. Additive changes
only, or a two-deploy expand/contract.
