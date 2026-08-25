# ADR-035 — The shared pool's credentials stay in the environment

**Status:** accepted · Phase 6, Step 1 · 2026-08-26
**Implements:** `phase6.md` §3 D37 (against `project-overview.md` §6)
**Relates to:** [ADR-034](ADR-034-per-candidate-credential-resolution.md) (the resolver whose shared
branch this decides), [ADR-017](ADR-017-render-as-deploy-target.md) (where the environment actually
lives), [ADR-010](ADR-010-redis-fail-open-and-readiness.md) (what "boot depends on X" costs)

## Context

`project-overview.md` §6 describes `provider_keys` with `owner_type ∈ {system, user}` and says
"`system`-owned rows back the shared pool". Read literally, Phase 6 would migrate `GROQ_API_KEY`,
`GEMINI_API_KEY` and `OPENROUTER_API_KEY` out of `Settings` and into encrypted database rows, and
`registry.system_key` would become a query.

The schema is being created in this phase either way. The question is only whether anything writes
`owner_type='system'`.

## Decision

**`provider_keys` stores only `owner_type='user'` rows in v1.** The column and its CHECK are created
exactly as §6 specifies — with a companion CHECK tying `owner_id` to it (`NULL` exactly when
`owner_type='system'`) — so a later phase can populate it without a migration. `registry.system_key`
keeps reading `Settings`, and the resolver's shared branch calls it, which is precisely what that
function's pre-existing docstring predicted.

## Why

**The deployment platform is already the secret store.** Render and Vercel inject these values;
rotating one is a dashboard edit and a restart, with no gateway code in the path. Moving them into
Postgres would replace a mechanism that works with one we would have to build, operate and back up.

**It would make boot depend on Postgres.** `build_registry` fails at startup on a missing credential,
before a single request is served, and `registry.py`'s docstring says so as a promise. Reading rows
instead moves that check behind a database round trip — or, worse, defers it to the first 502
mid-demo. ADR-010 already argued the general form of this: a dependency added to the boot path is a
new way for the service to be down.

**It adds a place a shared credential can leak** — a database dump, a `SELECT *` in a support
session, a replica — for no capability the environment did not already provide.

**The encryption key would guard itself.** `ENCRYPTION_KEY` lives in the environment either way, so
encrypting the shared keys at rest with a key from the same environment defends against exactly one
attacker: one who has the database and not the environment. That is a real attacker, and it is still
not worth a boot-time dependency for three values that are already rotatable in a dashboard. The
calculus is different for *user* keys, which is why those are encrypted: there the database is the
only place they can live at all.

## Consequences

- The `owner_type`/`owner_id` columns and both CHECKs exist and are exercised by
  `tests/integration/test_repo_provider_keys.py`, but every row written in v1 has
  `owner_type='user'`. That is a deliberate seam, not dead schema — the alternative was a column
  `provider_keys` would need a migration to grow.
- The partial unique index is `(owner_id, provider) WHERE owner_type='user' AND is_active`, which is
  correct in a world with system rows too: a system row has a `NULL` `owner_id` and would not be
  constrained by it.
- **Key rotation is not built, and `MultiFernet` is the named seam for it.** `cryptography`'s
  `MultiFernet` decrypts with any key in an ordered list and encrypts with the first, which is
  exactly the shape a rotation needs: deploy with `[new, old]`, re-encrypt rows in the background,
  drop `old`. `crypto._fernet()` is a single `lru_cache`d constructor precisely so that becomes one
  function's change. Until then, losing or rotating `ENCRYPTION_KEY` makes every stored user key
  unreadable — the resolver falls back to the shared pool and logs a `key_id` per row, and
  `docs/deploy.md` says so under the variable.
- Anyone reading §6 and then the migration will find one row type missing. This ADR is the answer to
  that, and `alembic/versions/0005_provider_keys.py`'s own comment points here.
