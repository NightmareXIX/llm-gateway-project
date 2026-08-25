# ADR-034 — The credential and the quota scope are one answer, resolved per candidate

**Status:** accepted · Phase 6, Steps 4–6 · 2026-08-26
**Implements:** `phase6.md` §3 D36 and D38 (`project-overview.md` §9.3, §9.5, §9.6)
**Relates to:** [ADR-016](ADR-016-streaming-session-lifetime.md) (why the router cannot hold a
session), [ADR-020](ADR-020-quota-reservation-placement.md) (where in the loop a reservation
happens), [ADR-026](ADR-026-file-storage-and-ownership.md) (the `session_factory` precedent, set by
`PerceptionResolver`), [ADR-007](ADR-007-auth-model.md) (quota keys on `user_id`, never
`api_key_id`)

## Context

Before this phase the router asked two questions in two places, once per candidate and once per
request:

```python
key = registry.system_key(spec.provider)        # inside the candidate loop
... quota.reserve(spec, scope=scope, ...)       # `scope`, a per-request parameter, always SYSTEM_SCOPE
```

§9.5 makes BYOK **per provider, not per user**: a user can hold their own Gemini key and stay on the
shared pool for Groq. A single failover chain crosses both — candidate 1 is Gemini on the user's own
credential, candidate 2 is Groq on ours — so "which key" and "which counters" change *within* one
request, and they change together. Two mechanisms that must agree, in two places, at two different
granularities, is a bug waiting for the first person who edits one of them.

The second constraint is where the answer can be looked up from. The private branch needs a database
row, and the router has no session and must not be given one: D14/ADR-016 established that the
streaming path's generator outlives FastAPI's request-scoped `yield` dependency, so a session handed
in would already be closed by the time a mid-stream restart resolved a second provider.

## Decision

**One injected object, `ProviderCredentials`, answering both questions per candidate — and the
`scope` parameter deleted rather than carried alongside it.**

- `ResolvedKey(provider, key, pool, scope, key_id, shared_daily_cap, user_id)` is the single answer.
  `router.route`, `route_stream`, `stream_completion`, `PerceptionResolver` and `extract_with_llm`
  all dropped `scope: keys.Scope = SYSTEM_SCOPE` and gained
  `credentials: ProviderCredentials | None = None`.
- Inside every candidate loop, `registry.system_key(spec.provider)` and `scope=scope` became
  `resolved = await credentials.for_provider(spec.provider, spec.model)` and `resolved.scope`.
- `SystemCredentials(registry)` is the default when a caller passes `None`: every provider resolves
  to the environment's key at `SYSTEM_SCOPE`, which is exactly today's behaviour.
- `UserCredentials` takes a **`session_factory`**, loads *all* of the caller's active rows in one
  query on first use, memoizes for the life of the request behind an `asyncio.Lock`, and falls back
  to the shared pool per provider with no row.
- `get_credentials` lives in `app/deps.py` as a plain factory; `api/v1/chat.py` and
  `api/v1/models.py` compose it into `CredentialsDep`.

## Why

**Because §9.4's branch is *driven by* `pool`.** The private path scopes the existing counters to the
user; the shared path additionally checks that user's personal cap. That branch cannot be written at
all if the credential and the scope arrive from two unrelated accessors — the code would have to
re-derive "is this private?" from a comparison it did not make. Returning both from one call means
the two can never disagree, because there is only one of them.

**Per candidate, not per request, because §9.5 says so.** A per-request answer would have to pick one
pool for a chain that crosses two, and either bill a user's Gemini traffic to the shared budget or
bill the shared Groq attempt to the user. The unit suite drives exactly this case — Groq into Gemini,
two scopes, two keys, neither leaking into the other's counters — because it is the case the design
exists for and the one nothing else would catch.

**A `session_factory`, not a session** (D38, ADR-016). The resolver opens and closes its own session
inside `for_provider`, so it survives a mid-stream restart that happens long after the request-scoped
dependency was torn down. `PerceptionResolver` hit this same wall in Phase 4 and took the same shape;
this is that precedent being reused rather than a new pattern.

**One query per request, memoized — not one per candidate.** §9.6's "removing a key takes effect on
your very next message" is a payoff of resolving per *request*, and a new request is a new resolver,
so a per-request snapshot satisfies it completely. A naive per-candidate `SELECT` would issue up to
nine per turn under failover, on a free-tier connection pool, for an answer that cannot change inside
one request. `last_used_at` is touched for every row in the same load, which trades precision (a
provider this turn never reached still counts as "handed out") for the single round trip — the same
trade `api_keys_repo.touch_last_used` already makes, and it answers the only question the settings UI
asks of that column.

**`None` defaulting to `SystemCredentials` keeps every existing test honest.** A router test that
constructs no resolver gets exactly the pre-Phase-6 behaviour, so the entire Phase 2–5 suite proved
the router still worked without a line of rewriting. That is not laziness: a phase that has to edit
every existing test to stay green has lost its regression net at precisely the moment it most needs
one.

**`ResolvedKey.key` is a plain `str`, not a `SecretStr`.** Contract A's `complete(payload, key: str)`
is frozen and takes a `str`; wrapping it here would mean a `.get_secret_value()` at the one call site
that matters and a false sense of safety everywhere else. Safety comes from the field never entering
a log call, which `tests/integration/test_credential_leakage.py` enforces directly rather than by
type.

**An undecryptable row falls back to the shared pool rather than failing the request.** A rotated
`ENCRYPTION_KEY` makes every stored row unreadable (see `docs/deploy.md`); refusing to answer would
turn a key-management mistake into a total outage. `decrypt_provider_key` raises a typed
`CredentialUnreadable` — never `None`, never a partial string — which is what lets the resolver tell
"no key stored" apart from "this row was written under a different key" and log the second with a
`key_id` and no ciphertext.

## Consequences

- `grep -n "scope=" app/routing/router.py` now shows only `resolved.scope` and `_reconcile_hint`'s
  own pass-through parameter. That grep is the invariant, and trap 1 is why: `commit`, `release` and
  `_reconcile_hint` all take a scope, and `_reconcile_hint` alone appears four times across the two
  loops. A hint applied under the wrong scope corrects a counter the request never touched, in the
  optimistic direction, silently.
- `for_provider` takes `(provider, model)`, not `(provider)` as D36 first sketched. D39's personal
  cap is per (provider, model), a finer grain than the credential question, and resolving them
  together is the whole point of this ADR — so the signature moved to the finer grain rather than
  spawning a second lookup.
- `get_credentials` and `get_resolver` are plain factories rather than FastAPI dependencies, and
  `ResolverDep` moved from `deps.py` into `api/v1/chat.py`. Building either needs the authenticated
  principal, and `app.auth.dependency` already imports `app.deps` — so a `Depends(get_principal)`
  inside `deps.py` is the import cycle `get_rate_limiter`'s docstring already documents (trap 2).
  Composition happens at the endpoint, exactly as `RateLimitDep` does.
- A D19 cache hit resolves nothing, opens no session and touches no `last_used_at`. It never reaches
  a candidate loop, so there is no credential question to answer.
- **D42 (`key_pool`, the eighth disclosure field) gets no ADR of its own.** It is the fifth
  application of a pattern established by `degraded`, `extraction_tier`, `messages_dropped` and
  `warning` — stored `meta`, both response shapes, the `done` event, the frontend indicator — and
  `docs/decisions/` should not fill with entries for the obvious. The same call ADR-032 made about
  D33. Where the value comes *from* is this ADR's business: the winning attempt's `ResolvedKey.pool`,
  which is why `AttemptRecord` carries it per attempt too.
