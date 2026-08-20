# Deploying the gateway

Phase 1, Step 11. Four managed services, all on free tiers: **Supabase** (Postgres + Auth),
**Upstash** (Redis), **Render** (the FastAPI service), **Vercel** (the Next.js frontend).

Run the steps in order. Several of them fail in ways that are only obvious in hindsight, and each
of those has a note saying so.

> The gateway ran on Fly.io until the free allowance ran out.
> [ADR-017](decisions/ADR-017-render-as-deploy-target.md) records what the move to Render changed and
> what it cost; this document is the current runbook and does not describe the old one.

---

## 0. Before you start

Postgres must be up for the integration suite (`docker compose up -d postgres`), and on Windows
`make` needs installing first — `winget install ezwinports.make`, then open a new shell so the PATH
change takes effect.

```bash
make lint && make typecheck && make test    # must be green before anything ships
make docker-build                           # the image the deploy will build, built locally first
```

No CLI to install for the deploy itself. Render builds from the connected GitHub repo, and everything
below is either the dashboard or `curl`.

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

**Note the region in the pooler hostname** (`aws-0-<region>.pooler.supabase.com`). Step 3 picks the
Render region from it — as close as Render's five will get, which for an `ap-northeast-1` project is
not close at all.

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
             ENCRYPTION_KEY='x', FILES_STORAGE_BACKEND='local')
# FILES_STORAGE_BACKEND='local' sidesteps Phase 4's SUPABASE_SERVICE_ROLE_KEY
# requirement — this probe is testing DATABASE_URL, not Storage.
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

Read since Phase 2 Step 2: the circuit breaker keeps its state here so every instance skips the same
dead provider. Quota and caching join it in Phase 3.

`/readyz` reports Redis but is never failed by it — an instance that cannot reach Redis serves every
request correctly, just without the breaker's memory, and taking a healthy machine out of rotation
(or blocking a rollout) over an Upstash blip would be a self-inflicted outage. See
[ADR-010](decisions/ADR-010-redis-fail-open-and-readiness.md). The practical consequence for
operating this: a green `/readyz` does not mean Redis is up. Check the body's `redis` field, or the
`redis.unreachable` warning in the logs.

---

## 3. Render — the gateway

**Set `region` in [`render.yaml`](../render.yaml) to match the Supabase region** you noted in step 1,
before anything else. Render has five: `oregon`, `ohio`, `virginia`, `frankfurt`, `singapore`.
`us-east-*` → `virginia`, `us-west-*` → `oregon`, `eu-*` → `frankfurt`, anything in Asia-Pacific →
`singapore`.

There is **no Tokyo region**, so a Supabase project in `ap-northeast-1` cannot be co-located the way
it was on Fly. Singapore is the closest available and still costs roughly 60–90ms per Postgres round
trip, several times per request. That is a permanent, accepted cost of the move
([ADR-017](decisions/ADR-017-render-as-deploy-target.md)), not something tuning fixes.

**Render cannot change a service's region after creation.** Getting this wrong means deleting the
service and making a new one, under a new URL — which then has to be re-pasted into Vercel and
`config/providers.yaml`. Decide before you click Create.

### Create the service

`render.yaml` is committed and authoritative. At [dashboard.render.com](https://dashboard.render.com):
**New → Web Service → connect this repository**. Render detects `render.yaml` and offers to create
the service from it; take that path rather than filling the form in by hand, and the region, health
check, start command and non-secret environment variables all come from the file instead of from
memory.

Creating it by hand instead means setting, at minimum: Runtime **Docker**, Instance type **Free**,
Region as above, Health check path **`/readyz`**, Auto-Deploy **Off**, Docker command
**`sh /srv/app/start.sh`**, and the three environment variables from `render.yaml`'s `envVars`. A
hand-made service that disagrees with the file is the failure this whole document exists to prevent,
so mirror any dashboard change back into it.

The name becomes the hostname — `https://<name>.onrender.com` — and `onrender.com` is a **shared**
namespace, so a near-miss is somebody else's live service rather than a DNS error. Two files hardcode
it and must match whatever you pick:

- `config/providers.yaml` → `options.HTTP-Referer` (OpenRouter attribution)
- Vercel's `GATEWAY_URL` (step 4)

### Turn Auto-Deploy off

**Settings → Build & Deploy → Auto-Deploy → Off**, if the Blueprint did not already set it from
`render.yaml`.

This is a correctness setting, not a preference. Render's own push-triggered deploys know nothing
about CI, so leaving it on ships every push to `main` whether or not the tests passed — which is
exactly what the `deploy` job's `needs: [lint, test, frontend]` exists to prevent. Step 6 wires the
deploy hook that replaces it.

### Set every variable before the first deploy

This is the step that catches people. The start command ([`start.sh`](../start.sh), invoked by
`render.yaml`'s `dockerCommand`) runs `alembic upgrade head`
before uvicorn binds, and [`alembic/env.py`](../alembic/env.py) resolves `DATABASE_URL` through
`app/config.py` — which validates the *whole* settings object. A missing `GROQ_API_KEY` fails the
migration exactly as loudly as it would fail the app, before the service ever becomes healthy.

> **Where this differs from a release command.** On Fly the migration ran in a one-off machine and a
> failure aborted the rollout. Here it runs in the container itself: a failure means the container
> exits, the health check never passes, Render cancels the deploy, and the previous instance keeps
> serving. Same protection, different symptom — look in the **deploy log**, not the service log, and
> expect "health check failed" as the headline with the real cause above it.

> **Phase 2 added two.** `GEMINI_API_KEY` and `OPENROUTER_API_KEY` are required `Settings` fields
> as of Phase 2 Step 1, even though both providers are `enabled: false` in `config/providers.yaml`
> until Step 6. On a service that already exists, set them **before** the deploy that carries this
> change — otherwise the start command fails config validation and the deploy is cancelled, which
> looks like a broken migration and is not one.

> **Phase 4 Step 1 added one more.** `SUPABASE_SERVICE_ROLE_KEY` is required whenever
> `FILES_STORAGE_BACKEND` is `supabase` — its default, and unset anywhere in `render.yaml`, so
> production needs it the moment this step's commit deploys. Same failure shape as the row above:
> set it **before** that deploy, or the start command's config validation fails and the deploy is
> cancelled rather than the service coming up degraded.

Ten variables, and **only seven of them are the values from your local `.env`**. Copying that file
wholesale points production at your laptop:

| Variable | Value |
|---|---|
| `SUPABASE_URL`, `SUPABASE_JWT_AUDIENCE`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | same as `.env` — the service-role key bypasses row-level security on the whole project, so handle it exactly like a provider key: never logged, never in an error message |
| `DATABASE_URL` | the Supabase pooler string from step 1 — `.env` holds `127.0.0.1:5432`, the compose container |
| `REDIS_URL` | Upstash, from step 2 — `.env` holds `localhost:6379` |
| `ENCRYPTION_KEY` | **generate a fresh one.** Nothing is encrypted with it until Phase 6, so a new key is free now; sharing dev's means a leaked dev `.env` also decrypts production, and rotating later is a migration |
| `REQUIRE_VERIFIED_EMAIL` | type `true` **literally**. It has a default in `Settings`, so it is frequently absent from `.env` — scripting it out of that file yields an empty string, and an empty string is not a boolean |

> That last row is a real failure, not a hypothetical. Anything scripted that interpolates a missing
> key yields an empty string, and the start command dies with `Input should be a valid boolean,
> unable to interpret input`. Anything with a default in `app/config.py` is a candidate for this;
> only the six in the first row are safe to read out of `.env` programmatically, because only those
> are always present. Typing them into the dashboard sidesteps it — paste each value, and do not
> paste a trailing space.

Set them at **the service → Environment → Environment Variables**, one at a time or via *Add from
.env* (which takes `KEY=value` lines pasted into a box). Generate the Fernet key first:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

`ENV`, `WEB_CONCURRENCY` and `FORWARDED_ALLOW_IPS` are **not** in that list and should not be added
here. They live in [`render.yaml`](../render.yaml)'s `envVars` block, because none of them is a
secret and all three belong in version control — `ENV=prod` is what disables the `/docs` route. A
dashboard variable of the same name shadows the file's, silently.

`PORT` is set by neither. Render assigns it, its value wins over anything declared alongside it, and
`dockerCommand` binds `$PORT` rather than a number — so there is nothing to set and nothing that can
drift.

Render shows variable values back, unlike Fly's names-and-digests. Useful for catching a truncated
paste; also worth knowing before treating the dashboard as a place secrets are hidden.

### Deploy

**Manual Deploy → Deploy latest commit**, or push to `main` and let CI's hook fire once step 6 is
done. Watch the **Logs** tab.

A healthy first deploy shows `alembic` applying `0001_initial_schema` and `0002`, then exactly one
`startup.complete` JSON line carrying the loaded slot table — one, not two, because
`WEB_CONCURRENCY=1`. Then the health check turns green and the service takes traffic.

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
| `GATEWAY_URL` | `https://llm-gateway-sed.onrender.com` | **Server only.** |
| `DEMO_MAIL` | the shared demo account's address | **Server only.** Optional. |
| `DEMO_PASSWORD` | its password | **Server only.** Optional. |

**Set these in the Vercel dashboard, not in `frontend/.env.local`.** That file is gitignored and
never leaves your machine — it configures `make frontend-dev` and nothing else. Filling it in does
not configure the deployment.

> **Get `GATEWAY_URL` exactly right.** `onrender.com` is a shared namespace, so a near-miss hostname
> is usually a *live service belonging to somebody else* rather than a DNS error you would notice.
> [`lib/api.ts`](../frontend/lib/api.ts) attaches the user's Supabase JWT to every request that goes
> through the rewrite, so a wrong host here sends valid session tokens for your project to a
> stranger's server, silently, on every authenticated request. Verify before pasting:
> `curl https://<name>.onrender.com/healthz` must return `{"status":"ok"}` — a 404 means it is not
> yours. Give it a minute if the service has been idle; a free instance spinning up serves a holding
> page first.

`GATEWAY_URL` is read server-side by [`next.config.ts`](../frontend/next.config.ts)'s rewrite. The
browser only ever calls `/api/gw/*` on the Vercel origin, which keeps every request same-origin —
no preflight, no `Access-Control-Allow-*` to get wrong, and no CORS middleware in
[`app/main.py`](../app/main.py). Do not "fix" this by pointing the browser at the Render URL
directly; that trades a working setup for a CORS configuration.

> **`rewrites()` runs at build time, so editing the variable is only half the change.** The value is
> baked into the deployment; until you redeploy, the old target is still in use — and *promoting* an
> existing deployment re-uses its baked value too, so it has to be a fresh build.
>
> The symptom of a stale or wrong target is a **502 whose body is not the gateway's error envelope**
> — `ROUTER_EXTERNAL_TARGET_HANDSHAKE_ERROR` from Vercel's edge, surfacing in the UI as
> `unexpected_response`, because [`lib/api.ts`](../frontend/lib/api.ts) refuses to mangle a foreign
> body into a fake error code. Confirm which leg is broken before touching the gateway:
> `/healthz` on the Render host answering 200 while `…vercel.app/api/gw/v1/me` returns 502 means the
> request never left Vercel. A working rewrite answers that path with **401** and an error envelope.

Never set `SUPABASE_SERVICE_ROLE_KEY` here. The frontend has no use for it and Vercel env vars are
readable by anyone with project access.

**`DEMO_MAIL` / `DEMO_PASSWORD` — the "Try the demo account" button.** Both are deliberately without
a `NEXT_PUBLIC_` prefix: [`app/auth/demo/route.ts`](../frontend/app/auth/demo/route.ts) signs in with
them server-side and returns only the session cookies, so the credentials never enter the client
bundle. Leave either unset and the button does not render — the login page reads them at render time
purely to decide that. The account must already have a confirmed email; the gateway runs with
`REQUIRE_VERIFIED_EMAIL=true` and will reject its token otherwise.

> **`/login` is statically prerendered, so this is the same build-time trap as `GATEWAY_URL`.** The
> button's presence is baked into the deployment. Adding the variables to an already-built project
> changes nothing until a *fresh build* runs — and promoting an existing deployment re-uses the old
> one. Push a commit, or use Redeploy, after setting them.
>
> Its conversations are visible to every visitor who presses the button. That is what a shared demo
> account is; the copy under the button says so, and nothing sensitive belongs in it.

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

The first run will show `deploy` failing — the hook URL does not exist yet. `lint`, `test` and
`frontend` should be green.

### 6.2 Copy the deploy hook URL

Render dashboard → the service → **Settings → Deploy Hook**. It is a URL of the form
`https://api.render.com/deploy/srv-<id>?key=<secret>`.

**The URL is the credential** — the `key` query parameter is the whole of its authentication, and
anyone holding it can deploy this service. Treat it exactly like a token: never paste it into a
commit, an issue, or a log line. It can be regenerated from the same page if it leaks.

Unlike a Fly deploy token it does not expire, and it cannot do anything but deploy this one service —
it cannot read your environment variables or touch anything else in the workspace.

### 6.3 Store it as a repository secret

GitHub → the repo → **Settings → Secrets and variables → Actions → New repository secret**.

| Field | Value |
|---|---|
| Name | `RENDER_DEPLOY_HOOK_URL` |
| Secret | the whole `https://api.render.com/deploy/srv-…?key=…` URL |

The name must match exactly — [`ci.yml`](../.github/workflows/ci.yml) reads
`${{ secrets.RENDER_DEPLOY_HOOK_URL }}`, and a missing secret becomes an empty string rather than an
error.

**If this secret is missing, the job fails in a way that does not mention it.** That is what took
down run #18 — the secret had never been created, `${{ secrets.RENDER_DEPLOY_HOOK_URL }}` expanded to
an empty string, and `curl` died in two seconds with exit **3**, `URL rejected: Malformed input to a
URL function`. Nothing in the log named the secret, because a secret is masked even inside the error
that quotes it.

**Do not diagnose that exit code against your local `curl`.** The runner's is older than a developer
machine's and the two disagree about the empty-URL case specifically:

| `curl` exit | On the runner (8.5, Ubuntu 24.04) | On a current local curl (≥ 8.6) |
|---|---|---|
| 2 | — | Secret unset or empty (`blank argument where content is expected`) |
| 3 | **Secret unset or empty**, *or* set but malformed | Set but malformed — usually stray whitespace from the paste |
| 22 | URL well-formed, Render rejected it — usually a stale `key` after a hook regeneration | same |

curl 8.6.0 started rejecting blank option arguments before they reach the URL parser; 8.5 hands the
empty string to the parser, which calls it malformed. So on the runner, exit 3 alone cannot tell
"never set" from "set wrong" — which is the entire reason the workflow no longer relies on it. The
step checks the value itself and fails with a message naming this secret and this section, and it
strips surrounding whitespace first so a URL pasted with a trailing newline still works.

Use a **repository** secret, not an environment or organization one, unless you have a reason —
environment secrets need a matching `environment:` key in the job, which this workflow does not set.

### 6.4 What now happens on every push

| Job | Runs on | Does |
|---|---|---|
| `lint` | push + PR | `ruff check`, `ruff format --check`, `mypy` |
| `test` | push + PR | `pytest --cov` against a `postgres:16` service container |
| `frontend` | push + PR | `npm ci`, lint, typecheck, `vitest` |
| `deploy` | **push to `main` only** | `POST` to the Render deploy hook, pinned to the tested commit |

`deploy` has `needs: [lint, test, frontend]`, so a red test suite stops the deploy rather than
shipping past it — which only holds because Render's own Auto-Deploy is **off** (step 3). Turning it
back on quietly restores push-deploys that ignore this table entirely.

The job posts `ref=<the commit CI tested>` and returns as soon as Render queues the build. **A green
`deploy` job means "Render accepted it", not "it shipped."** The image build, the `alembic upgrade
head` inside the start command, and the health check all happen afterwards, and the Render dashboard
is the only place they report.

Verify by pushing anything to `main`, watching the **Actions** tab go green, then watching a new
deploy appear in Render's **Events** tab.

Vercel is not deployed from here — its own Git integration builds the frontend on push, which is one
fewer long-lived token for this repository to hold.

### 6.5 When the `deploy` job fails

Everything green except `deploy` means the code is fine and the *trigger* was not accepted. The step
reads the HTTP status Render answered with and fails with a message naming it, so the log says which
of these it was rather than `curl: (22)`:

| Status | What it means | What to do |
|---|---|---|
| 200 / 202 | Queued. 202 means another deploy is already running and this one is behind it. | Nothing. Watch Render's **Events** tab. |
| 400 | Render rejected the `ref`. | A workflow bug, not a secret problem. |
| 401 | The `key` in the hook URL is wrong or was regenerated. | Re-copy the hook (§6.2) into the secret (§6.3). |
| 404 | No such service, or Render cannot see the commit. | The service was deleted or re-created — its `srv-<id>` changed, so the old hook names nothing. Copy the new one. |
| 409 | The service is suspended, or the workspace may not deploy. | Free plan: the workspace's 750 monthly instance hours are gone. Deploys resume at the start of the next month, or immediately on any paid instance type. |
| 5xx / 000 | Render broke, or the request never completed. | Retried three times automatically (15s, then 30s). If it still fails, check [status.render.com](https://status.render.com) and the service's **Events** tab, then re-run the job. |

**A failed `deploy` job ships nothing and breaks nothing.** Whatever instance was live stays live —
the only consequence is that `main` is ahead of what is deployed until the job is re-run. Re-running
it is safe and idempotent: it posts the same `ref`, so it deploys the commit that run tested, not
whatever `main` has moved on to.

**Check the service itself before assuming the hook is the problem.** `curl -sI
https://llm-gateway-sed.onrender.com/healthz` and read the `x-render-routing` header:
`no-deploy` means the service has no live deploy at all — the deploy hook is a symptom then, not the
cause, and the answer is in Render's Events tab.

---

## 7. Smoke test

PowerShell — use `Invoke-RestMethod`/`Invoke-WebRequest` rather than `curl`, which is an alias for
`Invoke-WebRequest` in Windows PowerShell 5.1 and the real `curl.exe` in PowerShell 7. The
`-SkipHttpErrorCheck` flag is what stops a deliberate 401 or 404 from throwing:

```powershell
$API = 'https://llm-gateway-sed.onrender.com'

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
API=https://llm-gateway-sed.onrender.com

curl -s  $API/healthz                       # {"status":"ok"}
curl -s  $API/readyz                        # {"status":"ok","database":"ok"}
curl -si $API/v1/me | head -20              # 401, error envelope, X-Request-ID header
curl -s  $API/docs -o /dev/null -w '%{http_code}\n'   # 404 — ENV=prod hides it
```

Four things are being checked, and the third is the interesting one. `/healthz` proves the process
is up; `/readyz` proves it reached Supabase through the pooler, which is the only part local testing
cannot demonstrate; `/docs` returning 404 proves `ENV=prod` reached the container from `render.yaml`.
The `/v1/me` call proves the error envelope survived deployment — and the `x-request-id` header must
equal the `request_id` inside the body, which is the promise that makes a user's bug report traceable
to a log line.

**Run the first request twice if the service has been idle.** A free instance spun down after 15
quiet minutes takes about a minute to come back, and the first call can time out or land on Render's
holding page while it does. That is the cold start, not a failure.

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

### 7.1 Streaming, against the real proxy (Phase 2)

`make dev` proves the SSE framing is correct. It cannot prove the response is not buffered, because
nothing between `uvicorn` and a local browser can buffer it — the one place that trap (`phase2.md` §5
Trap 1) actually shows up is a real proxy hop, and this deployment has two: Render's edge in front of
the gateway, and Vercel's rewrite in front of that. **Re-run this after the move off Fly**: Render's
proxy is a different implementation with its own buffering behaviour, and nothing about Fly streaming
correctly carried over. Test against the deployed URL, never against `make dev`, and watch the
*timing* of the output, not just its content:

```bash
API=https://llm-gateway-sed.onrender.com

curl -N -s "$API/v1/chat/completions" \
  -H "Authorization: Bearer $GW_KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"count to five"}]}'
```

`-N` disables curl's own output buffering — without it, a buffered response and a genuinely streamed
one both print identically, which is exactly the false negative worth avoiding. A working stream prints
`event: meta`, then each `event: delta` arriving perceptibly one at a time as the model generates; a
buffered one prints nothing at all until the whole response is ready and then dumps every event at
once. `X-Accel-Buffering: no` (`app/streaming/sse.py`) is what defeats an nginx-family proxy doing this;
it does nothing for a proxy that buffers for a different reason, which is why the timing check matters
more than the header's presence.

**The Vercel leg needs the same test, through the rewrite** — `next.config.ts`'s `rewrites()` is a
separate hop with its own buffering behavior, and Render streaming correctly says nothing about
whether Vercel's edge does:

```bash
curl -N -s "https://<your-app>.vercel.app/api/gw/v1/chat/completions" \
  -H "Authorization: Bearer $GW_KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"count to five"}]}'
```

If this one buffers while the direct Render call above does not, the rewrite is the culprit, not the
gateway — worth knowing before spending time re-reading `orchestrator.py`.

Render caps a single request at 100 minutes, which no answer this gateway produces comes close to, so
the connection lifetime is not a constraint on streaming here.

**A pre-stream failure should still look like an ordinary error**, not a stalled connection — this is
D13's payoff, checkable without a live provider outage:

```bash
curl -si "$API/v1/chat/completions" \
  -H "Authorization: Bearer $GW_KEY" -H "Content-Type: application/json" \
  -d '{"model":"nonexistent-slot","stream":true,"messages":[{"role":"user","content":"hi"}]}'
# a 4xx JSON error envelope with a request_id, not a 200 that hangs or says "failed" inside itself
```

---

## Operating notes

**Cold starts, and they are worse than Fly's.** A free instance spins down after 15 minutes with no
inbound traffic and takes about a minute to come back — a full container start, not a resume, and it
now runs `alembic upgrade head` on the way up. Accepted deliberately; it is in the risk register
([development-plan.md](../doc/reference/development-plan.md) §5). Before a demo, wake it and wait for
the answer: `curl https://llm-gateway-sed.onrender.com/healthz`.

**A spin-down mid-stream drops the connection, not the request.** Render's idle timer does not know a
`StreamingResponse` is open — a long streamed answer arriving just as the instance would otherwise
have gone quiet can end before `done` is sent, and the client sees a connection error rather than an
in-band failure ([docs/limitations.md](limitations.md)). Low-probability in ordinary use, since the
traffic itself resets the timer, but worth ruling out first if a chaos demo shows an unexplained
dropped stream with no matching provider-side fault in the logs. The only fix is a paid instance type
that does not spin down.

**A restart loop is a new failure mode.** Render restarts an instance after 60 seconds of failing
health checks, and `/readyz` fails when Postgres is unreachable — so a Supabase outage now cycles the
service rather than quietly draining it. Add the migration in the start command and an outage during
a cold start leaves it down entirely. If that ever needs breaking into: change the Docker Command in
the dashboard to `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers` — which skips
`start.sh` and therefore the migration — deploy, and put `sh /srv/app/start.sh` back afterwards.
That replacement is a single command with no chaining, which is the only shape Render's command field
can express (it execs argv directly rather than running a shell; see
[ADR-017](decisions/ADR-017-render-as-deploy-target.md)).

**Rolling back.** The service's **Events** tab → the deploy you want → **Rollback**. The free plan
keeps only the **two previous deploys**; past that, re-deploy the commit by pushing or by using the
hook's `ref` parameter. Note that a rollback does **not** roll back a migration; Alembic downgrades
are separate and deliberate.

**Reading logs.** Everything is JSON with `request_id` and `user_id` bound. To trace one user report,
search the **Logs** tab for the request id — the one in the error envelope they quote is the same one.
Free instances have no shell, so the dashboard (or `render logs` from the CLI, if you install it) is
the only way in.

**Rotating a provider key.** Edit `GROQ_API_KEY` (or `GEMINI_API_KEY` / `OPENROUTER_API_KEY`) in the
service's Environment tab; saving triggers a redeploy. A revoked key surfaces as a clean 502 with a
`requests` row at `status='error'`, never a traceback — and from Phase 2 Step 5 onward, as a failover
to whichever provider is still live rather than as an error at all.

**Database migrations on a live deploy.** They run in the new container before it passes its health
check and takes traffic, while the old one is still serving — so a migration must be compatible with
the *old* code for the length of the rollout. Additive changes only, or a two-deploy
expand/contract.
