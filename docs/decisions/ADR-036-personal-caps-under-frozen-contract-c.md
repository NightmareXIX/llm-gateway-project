# ADR-036 — Personal caps on the shared pool, and the second amendment to Contract C

**Status:** accepted · Phase 6, Step 7 · 2026-08-26
**Implements:** `phase6.md` §3 D39 (`project-overview.md` §9.4)
**Amends:** Contract C (`app/cache/keys.py`) — one new key builder, with sign-off
**Relates to:** [ADR-022](ADR-022-our-own-rate-limiting.md) (the first amendment, and the shape this
one follows), [ADR-027](ADR-027-perception-quota-under-frozen-contract-c.md) (the same sub-counter
argument, made for the perception lane), [ADR-018](ADR-018-quota-fails-closed.md) (why this fails
closed and D20 fails open), [ADR-019](ADR-019-quota-window-model.md) (the window model this adds a
fourth reset kind to)

## Context

§9.4 says the **shared** path must check "the user's personal daily cap (`user_quota_allocations`)
*and* the global shared-pool remaining". The private path needs no such cap — nothing is being
shared, and it falls out for free: `scope = str(user_id)` and the existing `q:{scope}:…` counters do
the rest.

The cap does not fall out. Nothing in the system counts *one user's slice of a shared counter*, and
Contract C is frozen.

## Decision

**A named sub-counter under the same prefix, at a ceiling Postgres supplies and Redis counts.** One
new builder:

```python
def user_allocation(user_id: str | UUID, provider: str, model: str) -> str:
    """``q:{user_id}:{provider}:{model}:alloc:rpd`` — §9.4's personal daily cap."""
```

- `user_quota_allocations(user_id, provider, model, daily_cap, …)` stores the **ceiling only**.
- `config/limits.yaml`'s `gateway:` block gains `shared_pool_daily_cap` per tier as the default; a
  row overrides it for one (provider, model) triple; no row and no configured default means **no
  cap**, and behaviour is exactly pre-Phase-6.
- `app/quota/allocations.py::shared_pool_grants(spec, *, user_id, cap)` builds zero or one
  `WindowGrant`. `QuotaTracker.reserve` gained `extra_grants: tuple[WindowGrant, ...] = ()`, appended
  before the call to `reserve_windows`.
- The grant is built **only when `resolved.pool == "shared"`**, by `router._extra_grants`.

Three options were on the table, and two are wrong:

**(a) Count in Postgres** — `UPDATE user_quota_allocations SET daily_used = daily_used + 1` on the
hot path. This is exactly the naive post-hoc counting `quota/tracker.py`'s whole Lua argument exists
to refuse, plus a row lock per request.

**(b) Reuse `q:{user_id}:{provider}:{model}:rpd`** — the key already exists, and this is the subtly
wrong one. It would mean "your slice of the shared pool" on the shared path and "your own key's full
daily budget" on the private path — *the same key, two meanings, two different ceilings*. A user who
adds a key at noon inherits a morning's shared-pool count as if it were their own key's usage, and is
throttled against their own paid quota for traffic that never touched it. Wrong, and silently so.

**(c) A sub-counter, at a ceiling its caller computes.** Chosen.

## Why

**This is the shape `quota_perception_lane` already established** (D8, ADR-027): a named sub-counter
under `q:{scope}:{provider}:{model}:`, at a ceiling the caller supplies rather than one
`_effective_limit` derives from `limits.yaml`'s published provider numbers. The precedent is not an
excuse — it is the reason this cost one module and one key instead of a new subsystem.

**`reserve_windows`'s Phase 3 generalization paid for itself a second time.** It takes an explicit
`tuple[WindowGrant, ...]`, each carrying its own key and its own limit, and reserves across all of
them in one atomic script call. The shared path builds the grants it always built, appends one, and
hands the lot to the same function. There is no second Lua script, no second round trip, and no
window where the pool counter moved and the personal one did not.

**The table stores the cap, not the count.** §6 lists `daily_used` and `window_reset_at` on
`user_quota_allocations`; both are omitted. Those two *are* the live count, the live count is Redis,
and §6 itself already describes `provider_quota_state` as "Redis-backed" while listing it as a table
— the precedent for "the overview names a table, Redis holds the counter" was set four phases back.
Two perpetually-null columns would be worse than absent ones. That also makes this an **override**
table, which is what "allocation" means when the default is a policy: the feature is demoable with
zero rows in it.

**A fourth `ResetKind`: `rolling_daily`.** The personal cap is a policy of *this gateway*, with no
provider midnight to converge on — so its TTL is set once, day-wide, and never refreshed. That is the
same non-refreshed treatment `rolling_60s` gets, for the same reason (D16 trap 1, scaled up), and it
stays out of `tracker.py`'s `_CONVERGING_RESETS` by simply not being added to it. Finding this is
what pulled `quota/windows.py` into the step's file list.

**Reusing the `"rpd"` window label rather than inventing a fifth `QuotaWindow`** — again the trick
`quota_perception_lane` already plays. Two grants sharing that label inside one reservation do share
a hash field in `reserve.lua`'s bookkeeping hash, which is harmless here and the module docstring
says why: `commit` never inspects a non-token-cost window's hash field, and `release` is never called
on a router-built reservation in this codebase.

**Fails closed, with the rest of quota** (ADR-018). Redis unreachable means the reservation is not
confirmed and the candidate is skipped.

**And it is not D20 again** (trap 13). `rl:{user_id}:rpd` limits how many requests one user may make
*of the gateway*, across all providers, and **fails open** (ADR-022). This limits how much of one
*provider's shared free tier* one user may consume, per model, and **fails closed**. Different key,
different question, opposite failure rule — said in both module docstrings, because they will look
like duplicates to anyone who meets one first.

## The amendment itself

Contract C is frozen, and this adds one key to it. **Signed off, recorded here rather than absorbed
silently** — the second such amendment, after ADR-022's `rl:` window segment, and deliberately the
same shape:

- What was added: `q:{user_id}:{provider}:{model}:alloc:rpd`, via `keys.user_allocation`.
- What changed in an existing format: **nothing.** Every other builder is byte-identical.
- What had to be migrated: **nothing had ever written this key.** There is no old data under it and
  no reader to update — the same clean position ADR-022 was in.
- It is the **only** key builder Phase 6 adds. `cache/keys.py`'s own module docstring records the
  amendment beside ADR-022's, so a reader who never opens `docs/decisions/` still finds it.

## Consequences

- The key is always built on the caller's **real** `user_id`, even though the shared path's spending
  scope is `SYSTEM_SCOPE`. `ResolvedKey` therefore carries `user_id` alongside `scope`: the scope
  cannot name whose cap is being checked, because on this path it deliberately does not name a user
  at all.
- `ResolvedKey` also carries `shared_daily_cap`, resolved by the resolver rather than looked up by
  the router — which keeps the router free of database access, the property ADR-034 depends on.
  `UserCredentials._load_once` batch-loads every allocation row in the same session as the provider
  keys, so the cap costs nothing beyond the round trip ADR-034 already pays for.
- `ProviderCredentials.for_provider` takes `(provider, model)` because of this decision: the cap is
  per (provider, model), finer than the per-provider credential question.
- A capped user hitting the ceiling is skipped with `blocked_window="rpd"` — the same treatment any
  exhausted candidate gets, so it fails over rather than erroring, and `/v1/models` reports it. A
  second user on the same model and the same shared counter is unaffected, which is the test that
  proves this is a *per-user* fence rather than a smaller pool.
- With `shared_pool_daily_cap` unset and no rows, nothing about quota behaves differently than it did
  in Phase 5. The committed `limits.yaml` sets real demo values (`free: 50`, `plus: 200`) so the
  feature is not null out of the box.
