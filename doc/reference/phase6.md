# Phase 6 — BYOK Settings

Implementation plan. Derived from `development-plan.md` §3 Phase 6, which says only "§9 implemented end to
end" — so the specification is `project-overview.md` §9 in full, read against the code Phases 1–5 actually
shipped rather than against the skeleton it was planned from.

**Read this first, because it changes where the work is.** Phase 6 reads like seven tasks of roughly equal
size. It is not. Four of the seven are largely built already, and one of the remaining three is the whole
phase:

- **Key validation (task 5) is done.** `validate_key` is implemented on all three adapters, with §9.2's
  exact semantics — a 401/403 returns `valid=False` with wording written for a human, and an unreachable
  provider *raises* rather than telling a user their key is bad (`groq.py:436`, `gemini.py:564`,
  `openrouter.py`). Gemini's 400-means-invalid quirk is already special-cased with a comment naming §9.2.
  What is missing is an endpoint in front of it and a rate limit on that endpoint.
- **Encryption (task 2) is two function bodies.** `core/crypto.py` carries
  `encrypt_provider_key`/`decrypt_provider_key` as typed seams; `Settings.ENCRYPTION_KEY` is already a
  *required* `SecretStr`; `.env.example:59` already documents generating a Fernet key. `cryptography` is
  already importable (via `python-jose[cryptography]`) but is not a declared direct dependency, and should
  become one.
- **The migration (task 1) is one new table plus one small one.** `requests.quota_scope` already exists
  and has been written on every row since Phase 3 Step 1 — it is simply constant. `provider_quota_state`
  is Redis and always was.
- **`/v1/models` (task 6) already authenticates** and already carries the seam comment naming this phase
  (`api/v1/models.py:81`).

The phase's real work is the one task that sounds like plumbing:

> **`resolve_provider_key(user_id, provider)` called on every request.**

Because BYOK is **per provider, not per user** (§9.5), and a single request's candidate chain crosses
providers. A user with their own Gemini key and no Groq key must have their Gemini attempt billed to their
own credential and counted under `q:{user_id}:gemini:…`, while the Groq attempt in the *same failover
chain* uses the shared key and counts under `q:system:groq:…`.

Every call site in the gateway today takes a single `scope` for the whole request
(`router.route(scope=…)`, `route_stream(scope=…)`, `stream_completion(scope=…)`,
`PerceptionResolver(scope=…)`, `extract_with_llm(scope=…)`) and reads its credential from a second,
unrelated accessor (`registry.system_key(provider)`, called *inside* the candidate loop). Those two facts
are one fact, they are per candidate, and Phase 6 is the phase that says so. Phase 5's handoff note called
`scope` "the one constant Phase 6 replaces"; it is more accurate to say Phase 6 replaces **a constant and
an accessor with one object**.

---

## 1. Scope

**Goal:** a user pastes their own Gemini key into Settings, and their very next message — mid-conversation,
no reload — is served by that key, counted against their own budget, and says so. They delete it, and the
message after that is back on the shared pool. A garbage key never gets stored, and the plaintext appears
in no log line anywhere.

**In scope**

- `alembic/versions/0005_provider_keys.py` — `provider_keys` (§9.9's columns) and
  `user_quota_allocations`. `db/models.py` and `db/repo/provider_keys.py` alongside.
- `core/crypto.py`'s two seams, implemented with Fernet, validated at boot.
- **`app/keys_resolution/resolver.py`** — §9.3's resolver, per request, per provider, memoized, never
  cached at login. The phase's centre of gravity.
- **Per-candidate credential and scope threading** through both lanes: `routing/router.py` (both loops),
  `streaming/orchestrator.py`, `api/v1/chat.py`, `perception/lane.py`, `perception/extractors.py`,
  `deps.py`.
- **§9.4's quota branching.** The private path scopes the existing counters to the user. The shared path
  additionally checks that user's personal daily cap.
- `requests.quota_scope` written with the real value, and `key_pool` disclosed on the wire, on the stored
  `meta`, and in the UI — the eighth disclosure field, following the pattern established five times over.
- `POST/GET/DELETE /v1/provider-keys` in `api/keys.py`, with the add flow validating live before storing
  anything, rate-limited per §9.8.
- **§9.7's personalization.** `/v1/models` computed under each caller's real per-provider scope, plus a
  slot that only exists for a user holding a private key for it.
- Settings UI: one row per provider, masked, Add/Remove, and the plain-language data-terms disclosure
  §9.8 asks for.
- ADR-034 … ADR-039, `docs/limitations.md`, `docs/architecture.md`, `README.md`, `docs/deploy.md`.

**Explicitly NOT in Phase 6**

- **No idempotency (D6).** `keys.idempotency` stays unwritten; Phase 7.
- **No `pricing.yaml`, no simulated cost, no `/metrics`, no usage dashboard.** Phase 7.
- **No message pagination.** Phase 7 task 6, and `list_for_conversation` stays unpaginated when it lands.
- **No summarization, no tool calls.** `memory/summarize.py` stays the §2.2.7 seam; `RESERVED_BLOCK_TYPES`
  stays frozen; `pin_target`'s tool branch stays reachable only from a unit test.
- **No admin surface for `user_quota_allocations`.** The table is read; rows are written by hand or by a
  seed script. An admin UI is Phase 7's `api/admin.py`, and building one here would be the second time
  this phase invented a settings page.
- **No key rotation UI, no multi-key-per-provider.** One active key per (user, provider), enforced by a
  partial unique index. Replacing a key is remove-then-add. `MultiFernet` is named as the rotation seam in
  ADR-035 and not built.
- **No migration of the shared pool's credentials into the database.** See D37 — this is a decision, not
  an omission.
- **No change to any frozen contract signature.** Contract A is untouched (`validate_key` was always
  there). Contract B gains one additive optional key on `MessageMeta`, which `from_jsonb` already
  tolerates. **Contract C gains one key builder** (`q:{user_id}:{provider}:{model}:alloc:rpd`), amended
  with sign-off rather than silently — see D39, and ADR-022 for the precedent.

**Definition of done — one session, demoed live:**

1. Start a conversation on `general`. Get an answer. `/v1/models` shows what everyone else sees; the
   response's provenance says the shared pool served it.
2. Open Settings → API Keys. Every provider reads *Using shared pool*. Paste a deliberately broken Gemini
   key: a clear error naming Gemini, and `GET /v1/provider-keys` still returns nothing — **nothing was
   stored**.
3. Paste a real Gemini key. The row flips to *Using your key* with `••••{last 4}` beside it.
4. Send the **next message in the same conversation**, no reload: it is answered, the provenance says the
   private pool served it, and the `requests` row carries `quota_scope = <user_id>`. In Redis,
   `q:{user_id}:gemini:gemini-3.6-flash:rpd` has moved and `q:system:gemini:gemini-3.6-flash:rpd` has not.
5. Ask something that fails over to Groq in the same chain: that attempt's counters move under
   `q:system:groq:…`. One request, two scopes, because §9.5 is per provider.
6. `/v1/models` now shows a slot nobody else has (`pro`, on a Gemini Pro model the shared free-tier key
   cannot reach). Ask for it: it answers. A second account asking for the same slot gets the 400 an
   unknown slot gets.
7. Remove the key. The next message — again, no reload — is back on the shared pool, and `/v1/models` has
   dropped the extra slot.
8. `grep` the whole log stream for the key: zero hits. This is a **test**, not a manual grep — see Step 8.

---

## 2. What Phases 1–5 left, and what Phase 6 does to each seam

| Seam | Where | State today | Phase 6 |
|---|---|---|---|
| `registry.system_key(provider)` | `providers/registry.py:244` | the only credential accessor; docstring names this phase | stays, becomes the *shared-pool branch* of the resolver (Step 4) |
| `scope: keys.Scope = SYSTEM_SCOPE` | `router.route`, `route_stream`, `stream_completion`, `PerceptionResolver`, `extract_with_llm` | constant at every call site | replaced by a per-candidate resolver (Steps 5–6) |
| `keys_resolution/` | package | **empty** | `resolver.py`, this phase's first new module (Step 4) |
| `encrypt_provider_key` / `decrypt_provider_key` | `core/crypto.py` | typed seams, raise | Fernet bodies (Step 2) |
| `Settings.ENCRYPTION_KEY` | `config.py` | required `SecretStr`, unread | read, and validated at boot (Step 2) |
| `adapter.validate_key` | all three adapters | **fully implemented**, §9.2 semantics | called by an endpoint (Step 3); adapters untouched |
| `KeyValidation.models` | `providers/types.py:235` | populated, unread | still unread — see D41 |
| `requests.quota_scope` | `db/models.py:312` | column written, constant `"system"` | carries the real scope (Step 7) |
| `QuotaTracker.reserve_windows` | `quota/tracker.py:211` | generalized in Phase 3 Step 5 for a second caller with its own keys and ceilings | the personal cap is its third caller (Step 7) |
| `RateLimiter` | `deps.py` | two-bucket sliding window, refund, fail-open | reused for the validation endpoint (Step 3) |
| `GET /v1/models` `PrincipalDep` | `api/v1/models.py:81` | authenticated, `del principal` | reads the principal (Step 9) |
| `Slot.internal` | `config.py` | one visibility flag exists | a second joins it: `requires_private_key` (Step 9) |
| `AccountDialog` | `frontend/components/` | docstring says BYOK belongs to a later phase | gains the API-keys section (Step 10) |
| `MessageMeta` | `memory/canonical.py:173` | 12 fields | gains `key_pool: str \| None = None` (Step 7) |
| `api/keys.py` | module | gateway `gw_live_` routes; docstring says BYOK "lands in Phase 6" | second router in the same module (Step 3) |

**One new application module** (`keys_resolution/resolver.py`), because `development-plan.md` §1 and
`CLAUDE.md` §3 both reserve that slot for exactly this. If a step is producing a *second* new file under
`app/`, re-read this table.

---

## 3. Decisions to settle before writing code

Continuing the numbering (D35 is spent) and `docs/decisions/` (ADR-033 is the last written).

### D36 — The credential and the scope are one answer, per candidate

Today the router asks two questions in two places:

```python
key = registry.system_key(spec.provider)       # inside the candidate loop
... quota.reserve(spec, scope=scope, ...)      # `scope` a per-request parameter
```

§9.3 and §9.4 make these the same question — the resolver returns `{key, pool, quota_scope}` as one
object, and §9.4's branch is *driven by* `pool`. Keeping them apart guarantees the bug where a request
spends a user's private key against the system counters, or the reverse.

They must also be answered **per candidate**, not per request. §9.5 is explicit that a user can hold a
Gemini key and stay shared on Groq, and one failover chain crosses both.

**Decision: one injected object, resolved per provider, replacing both.**

```python
# app/keys_resolution/resolver.py
@dataclass(frozen=True, slots=True)
class ResolvedKey:
    provider: str
    key: str                       # plaintext, in memory only, never logged
    pool: Literal["shared", "private"]
    scope: keys.Scope              # SYSTEM_SCOPE, or str(user_id)
    key_id: UUID | None            # the provider_keys row, private pool only

class ProviderCredentials(Protocol):
    async def for_provider(self, provider: str) -> ResolvedKey: ...

class SystemCredentials:            # the shared-pool-only implementation
    """Every provider resolves to the environment's key at SYSTEM_SCOPE."""

class UserCredentials:              # §9.3, the real one
    """A user's own keys first, the shared pool second, memoized per request."""
```

`route`, `route_stream`, `stream_completion`, `PerceptionResolver` and `extract_with_llm` **drop their
`scope` parameter** and gain `credentials: ProviderCredentials | None = None`, defaulting to
`SystemCredentials(registry)` when `None`. Inside each candidate loop, the two lines above become:

```python
resolved = await credentials.for_provider(spec.provider)
... quota.reserve(spec, scope=resolved.scope, ...)
... await adapter.complete(payload, resolved.key, timeout=timeout_s)
```

Three consequences, all wanted:

- **`None` keeps every existing test honest.** A unit test that constructs no resolver gets exactly
  today's behaviour, so the router suite does not need rewriting to prove the router still works.
- **`scope` is deleted, not carried alongside.** Two parameters that must agree are two parameters that
  will eventually disagree. Only `QuotaTracker`'s own methods keep a bare `scope` — they are one level
  down and genuinely take one.
- **`ResolvedKey.key` is a plain `str`, not `SecretStr`.** Contract A's `complete(payload, key: str)` is
  frozen and takes a `str`; wrapping it here would mean an `.get_secret_value()` at the one call site that
  matters and a false sense of safety everywhere else. Safety comes from the field never entering a log
  call, which D42's test enforces directly.

### D37 — The shared pool stays in the environment

`project-overview.md` §6 describes `provider_keys` with `owner_type` ∈ {`system`, `user`}, and says
"`system`-owned rows back the shared pool". Taken literally, Phase 6 would migrate `GROQ_API_KEY`,
`GEMINI_API_KEY` and `OPENROUTER_API_KEY` out of `Settings` and into encrypted database rows.

**Decision: don't. `provider_keys` stores only `owner_type='user'` rows in v1.** The `owner_type` column
and its CHECK are created exactly as §6 specifies — the schema is right and a later phase can populate it
— but `registry.system_key` keeps reading `Settings`.

Why:

- **The deployment platform is already the secret store.** Render and Vercel inject these; rotating one is
  a dashboard edit and a restart, with no gateway code in the path.
- **It would make boot depend on Postgres.** `build_registry` currently fails at startup on a missing
  credential, before a request is served (`registry.py`'s docstring says so explicitly). Reading rows
  instead moves that check behind a database round trip, or defers it to the first 502 mid-demo.
- **It adds a place a shared credential can leak** — a database dump — for no capability the environment
  did not already provide.
- **The encryption key would guard itself.** `ENCRYPTION_KEY` lives in the environment either way, so
  encrypting the shared keys at rest with a key from the same environment buys defence against exactly one
  attacker: one who has the database and not the environment. Real, but not worth a boot-time dependency.

The resolver's shared branch therefore calls `registry.system_key(provider)`, which is precisely what its
existing docstring predicted.

### D38 — When the resolver queries, and whose session it uses

§9.6's live effect is a direct payoff of resolving **per request, not per session**. It is not a payoff of
resolving per *candidate attempt*, and a naive implementation would issue one `SELECT` per provider per
attempt — up to nine per turn under failover, on a free-tier connection pool.

Two constraints shape this:

1. **The router has no session and must not be given one.** D14: the streaming path's generator outlives
   FastAPI's request-scoped `yield` dependency, so a session handed in would already be closed by the time
   a mid-stream restart resolved a second provider. `PerceptionResolver` hit this exact wall in Phase 4
   and took a `session_factory`.
2. **Removing a key must take effect on the very next message** (§9.6) — but "the next message" is a new
   request, so a per-request snapshot satisfies it completely.

**Decision: `UserCredentials` takes a `session_factory`, loads *all* of the user's active rows in one
query on first use, and memoizes for the life of the request.**

```python
async def for_provider(self, provider: str) -> ResolvedKey:
    await self._load_once()                    # one SELECT, guarded by an asyncio.Lock
    row = self._by_provider.get(provider)
    if row is None:
        return ResolvedKey(provider, self._registry.system_key(provider), "shared", SYSTEM_SCOPE, None)
    return ResolvedKey(provider, decrypt_provider_key(row.encrypted_key), "private",
                       str(self._user_id), row.id)
```

One query per request, at most, and only when a provider is actually reached — a D19 cache hit resolves
nothing and opens no session. The `asyncio.Lock` matters because nothing forbids two concurrent
`for_provider` calls (Phase 4's resolver memo uses the same guard for the same reason).

**Decryption is lazy per row and cached.** Fernet is cheap, but decrypting a key for a provider this turn
never touches puts a plaintext credential in memory for no reason.

**`last_used_at` is written at resolve time, not at success time,** throttled in the WHERE clause exactly
as `api_keys_repo.touch_last_used` does — and for the same reason that function is called from
`auth/dependency.py:108` before the request has succeeded. "When did this credential last get handed out"
is the question the settings UI asks; threading a post-success callback through both lanes to answer it
more precisely would be machinery for a column nobody sorts on.

### D39 — The personal cap: Postgres holds the ceiling, Redis holds the count

**This decision adds one key to Contract C. Signed off — proceed.** It is the second deliberate amendment
to that contract, after Phase 3 Step 10's `rl:` window segment (ADR-022): recorded in an ADR, made with
sign-off rather than silently, and — as with that one — nothing has ever written the key, so there is
nothing to migrate.

§9.4's shared path must check "the user's personal daily cap (`user_quota_allocations`) *and* the global
shared-pool remaining". The private path needs no such cap — nothing is being shared.

The private path falls out for free: `scope=str(user_id)` and the existing `q:{scope}:…` counters do the
rest. The personal cap does not. Three options:

- **(a) Count in Postgres** — `UPDATE user_quota_allocations SET daily_used = daily_used + 1` on the hot
  path, which is exactly the naive post-hoc counting `quota/tracker.py`'s whole Lua argument exists to
  refuse, plus a row lock per request.
- **(b) Reuse `q:{user_id}:{provider}:{model}:rpd`** — the key already exists. But it would mean "your
  slice of the shared pool" on the shared path and "your own key's full daily budget" on the private path,
  with a *different ceiling*, on the same key. A user who adds a key at noon inherits a morning's
  shared-pool count as if it were their own key's usage. Wrong, and silently so.
- **(c) A sub-counter under the same prefix, at a ceiling the table supplies.**

**Decision: (c).** One new builder in `cache/keys.py`:

```python
def user_allocation(user_id: str | UUID, provider: str, model: str) -> str:
    """``q:{user_id}:{provider}:{model}:alloc:rpd`` — §9.4's personal daily cap."""
```

This is the **same shape `quota_perception_lane` already established** — a named sub-counter under
`q:{scope}:{provider}:{model}:`, at a ceiling its caller computes rather than one `_effective_limit`
derives (D8/D26, ADR-027). And it reuses the machinery Phase 3 Step 5 built for precisely this:
`reserve_windows` takes an explicit `tuple[WindowGrant, ...]`, each carrying its own `key` and its own
`limit`, and reserves across all of them in one atomic script call. The shared path builds the system
grants it builds today, appends one more, and hands the lot to the same function.

`app/quota/allocations.py` is the new module, sibling of `lanes.py`, holding the cap lookup and the grant
builder. It is the mirror image of `lanes.py`: that one *subtracts* a fence from a provider's ceiling,
this one *adds* a ceiling of our own.

**The table stores the cap, not the count.** `user_quota_allocations` gets `(user_id, provider, model,
daily_cap, created_at, updated_at)` and **not** §6's `daily_used` / `window_reset_at`. Those two are the
live count, the live count is Redis, and §6 itself already describes `provider_quota_state` as
"Redis-backed" while listing it as a table — the precedent for "the overview names a table, Redis holds
the counter" is set in this codebase four phases back. Two perpetually-null columns would be worse than
absent ones.

**Absent row → the tier default → no cap.** `config/limits.yaml`'s `gateway:` block gains
`shared_pool_daily_cap` per tier. A `user_quota_allocations` row overrides it for one (user, provider,
model); with no row and no configured default, there is no personal cap and behaviour is exactly today's.
That makes the feature demoable with zero rows and keeps the table meaningful — it is an *override* table,
which is what "allocation" means when the default is a policy.

**This is not D20 again.** `rl:{user_id}:rpd` limits how many requests one user may make *of the gateway*,
across all providers, and fails open. This limits how much of one *provider's shared free tier* one user
may consume, per model, and fails closed with the rest of quota. Different question, different failure
rule; say so in the module docstring, because they will look like duplicates to a reader.

### D40 — A private key that fails is not laundered through the shared pool

A user's own Gemini key is revoked, or hits its own rate limit. The candidate raises `AuthFailed` or
`RateLimited`. Should the router retry the same candidate on the shared key?

It is tempting — the request succeeds, the user never notices. **Refuse it.** Three reasons:

- **It hides a broken key indefinitely.** The user's Settings page says *Using your key*, every answer
  comes from the shared pool, and nothing ever tells them. That is the same failure §9.2 forbids at the
  add step ("silently saving a bad key just means it fails later, mid-conversation, in a much more
  confusing way") — arriving one layer down.
- **It misbills.** They opted into their own provider account precisely so the traffic would not come out
  of the shared budget.
- **It needs no new mechanism to do the right thing.** Contract A already marks `AuthFailed` and
  `RateLimited` `failover_eligible`, so the chain moves to the *next candidate* — a different provider,
  where the resolver answers independently. The user's Gemini being spent means Groq answers, which is the
  gateway's whole premise.

**Decision: a private key's failure fails that candidate, and the chain proceeds normally.** The one
addition is disclosure: on `AuthFailed` from a private-pool attempt, flip that row's `validation_status`
to `'invalid'` (a fire-and-forget write in its own session, never blocking the request) so the settings UI
can say *This key was rejected by Gemini — re-add it*. Not on `RateLimited`: a spent key is a working key.

### D41 — What makes a slot private-key-only (§9.7)

§9.7 says a private key "can unlock extra model slots… this falls out of the design for free — the model
registry just also checks the user's own `provider_keys` capabilities". It does not fall out for free,
because nothing in `providers.yaml` can express "this slot needs a key the shared pool does not have".

Two candidate mechanisms:

- **Derive it from `KeyValidation.models`** — the validation call already returns the model ids a key can
  reach, so a slot could be shown when the user's key lists its model. Rejected as the *gate*: that list
  is a snapshot taken at add time, it goes stale silently, and a key whose entitlements changed would
  either hide a working slot or advertise a broken one. It stays unpersisted; §9.7's real mechanism is
  simpler.
- **Declare it in config.**

**Decision: `Slot` gains `requires_private_key: bool = False`**, joining `internal` as the second
visibility flag. A slot carrying it is:

- **hidden** from `GET /v1/models` unless the caller's resolver reports `pool == "private"` for every
  provider in its candidate list;
- **refused** by `api/v1/chat.py::_validate_slot` for anyone else, with the same 400 an unknown slot gets
  — the same treatment `internal: true` already receives, and for the same reason: a slot you cannot be
  served is not a slot you should be told about.

`config/providers.yaml` gains a real `pro` slot on a Gemini Pro model, with the matching
`config/limits.yaml` block. §9.7's own example, verbatim: "a paid Gemini Pro key when the shared pool only
carries free-tier Flash". The shared key genuinely cannot reach it, which is what makes the demo honest
rather than staged.

### D42 — `key_pool`, the eighth disclosure field

Phase 5's handoff named this: "Phase 6 adds an eighth concern (which key pool served this) and the pattern
for adding it is now established three times over."

**Decision: follow it exactly, and do not invent a variation.** `key_pool: Literal["shared","private"] |
None`:

1. `MessageMeta.key_pool` (`to_jsonb`/`from_jsonb` via the existing string helper; absent reads `None`);
2. `ChatCompletionResponse.key_pool` and `DoneEvent.key_pool`, both optional, sourced the way
   `extraction_tier` is — `_Turn` on the streaming path, `outcome` on the other;
3. `provenance.ts`'s four constructors;
4. one line in `ModelIndicator.tsx`, in the *disclosure* register, not the error one.

`None` on a D19 cache hit — no key was spent, so there is nothing to disclose, exactly as
`extraction_tier` is `None` there (phase5 trap 3).

**Where it comes from:** the winning attempt's `ResolvedKey.pool`. That means `RouterOutcome` gains
`key_pool` and `AttemptRecord` gains it too — the trail should say which pool each attempt used, because a
turn that failed over from a private Gemini key to the shared Groq pool is exactly the row someone will
open `requests.attempts` to understand.

No ADR for this one. It is the fifth application of an established pattern, and `docs/decisions/` should
not fill with entries for the obvious — the same call ADR-032 made about D33.

### D43 — Rate-limiting the validation endpoint

§9.8: "Rate-limit the key-validation endpoint itself, since it's an obvious target for abuse."
`development-plan.md` suggests 5/hour/user.

`deps.RateLimiter` already has everything — a two-bucket sliding window, a refund on rejection, fail-open,
and `_retry_after_s`. What it does not have is an hourly window: `keys.GatewayWindow` is
`Literal["rpm","rpd"]` and `RATE_LIMIT_WINDOW_S` maps exactly those two.

**Decision: add `"rph"` to `GatewayWindow` and `3600` to `RATE_LIMIT_WINDOW_S`, and give `RateLimiter` a
second entry point** — `enforce_one(user_id, window, limit)` — that the validation route calls with
`("rph", 5)`. The chat path's `enforce(principal)` is untouched.

This is **not** a Contract C key-format change. `rl:{user_id}:{window}:{window_start}` is unchanged; a
third legal value in an existing segment is what that segment is for, and ADR-022 amended the *format*
precisely so the segment could carry more than one window. Note it in ADR-039 anyway, so the next reader
does not have to re-derive that it was considered.

The limit is a constant in `api/keys.py`, not YAML: `limits.yaml`'s `gateway:` block is per-tier user
throughput policy, and an anti-abuse floor on one endpoint is neither per-tier nor throughput.

---

## 4. Implementation steps

Ten steps, three milestones. Each is a commit, each leaves the suite green, and each names the files it is
allowed to touch.

**Milestone A — the key exists, encrypted, and can be added and removed** (Steps 1–3). Nothing uses it yet;
`GET /v1/provider-keys` reflects reality and the shared pool still serves every request.

**Milestone B — the key is used** (Steps 4–7). The resolver, both lanes threaded, quota branched, and the
disclosure on the wire.

**Milestone C — visible and honest** (Steps 8–10). The leak test, personalization, the UI, and the
documentation.

Before starting: `make test`, `make lint`, `make typecheck`, `make frontend-test`, `make frontend-lint` all
green on `main`. If any is red, that is the first commit and it is not part of this phase.

---

### Step 1 — The tables *(1 day)*

Touches `alembic/versions/0005_provider_keys.py` (new), `app/db/models.py`,
`app/db/repo/provider_keys.py` (new), `app/db/repo/allocations.py` (new), and tests.

1. **`provider_keys`** — §9.9's columns plus §6's:

   | column | type | notes |
   |---|---|---|
   | `id` | uuid pk | `_pk()` |
   | `owner_type` | text | CHECK `in ('system','user')`. Only `'user'` is written in v1 (D37) |
   | `owner_id` | uuid FK `users.id` ON DELETE CASCADE, nullable | null exactly when `owner_type='system'` |
   | `provider` | text | matches a `providers.yaml` key; deliberately **not** a FK to anything |
   | `encrypted_key` | text | Fernet token. Never logged, never returned |
   | `last_4` | text | the only part of the credential that ever leaves this table |
   | `nickname` | text, nullable | §9.9 |
   | `validation_status` | text | CHECK `in ('valid','invalid','unverified')` |
   | `last_validated_at` | timestamptz, nullable | |
   | `last_used_at` | timestamptz, nullable | D38 |
   | `is_active` | bool, default true | remove is a soft delete |
   | `created_at` | timestamptz | |

   Two constraints carry the semantics:

   - `CHECK ((owner_type = 'system' AND owner_id IS NULL) OR (owner_type = 'user' AND owner_id IS NOT NULL))`
   - a **partial unique index** `(owner_id, provider) WHERE owner_type = 'user' AND is_active` — one live
     key per user per provider (§9.5's granularity, and the reason "replace" is remove-then-add). Partial
     so a revoked row does not block re-adding.

   Plus `Index("ix_provider_keys_owner_id_provider", "owner_id", "provider")` for the resolver's one
   query.

   **No `last_4` uniqueness, no index on `encrypted_key`.** Nothing looks a provider key up by its value;
   the only read path is by owner.

2. **`user_quota_allocations`** — `(id, user_id FK CASCADE, provider, model, daily_cap int > 0,
   created_at, updated_at)`, unique on `(user_id, provider, model)`. **No `daily_used`, no
   `window_reset_at`** — D39 says why, and the migration's docstring should say it too, because their
   absence contradicts `project-overview.md` §6 read literally and the next reader deserves the reason in
   the file rather than in an ADR.

3. `db/models.py` gains both classes, in the file's existing style: a class docstring that says what the
   table is *for*, per-column docstrings on anything non-obvious, constraints declared in
   `__table_args__`. Put them after `FileExtraction`.

4. `db/repo/provider_keys.py` — every function ownership-scoped in the SQL, mutations returning `bool`,
   never committing. Follow `db/repo/api_keys.py` line for line:
   - `list_for_user(session, user_id) -> Sequence[ProviderKey]` — active and revoked, ordered by provider.
   - `get_active(session, *, user_id, provider) -> ProviderKey | None`
   - `list_active_for_user(session, user_id) -> Sequence[ProviderKey]` — the resolver's one query (D38).
   - `upsert(session, *, user_id, provider, encrypted_key, last_4, nickname, validation_status,
     last_validated_at) -> ProviderKey` — deactivates any existing active row for that (user, provider)
     and inserts, in one transaction. The partial unique index makes the deactivate mandatory, not
     optional.
   - `deactivate(session, *, user_id, provider) -> bool`
   - `touch_last_used(session, *, key_id, clock)` — the throttled UPDATE, copied from
     `api_keys.py:115` including the reasoning for putting the predicate in the WHERE clause.
   - `mark_invalid(session, *, key_id)` — D40's disclosure write.
5. `db/repo/allocations.py` — `get_cap(session, *, user_id, provider, model) -> int | None`. One
   function; the table is read-only in this phase.
6. Tests: `tests/integration/test_repo_provider_keys.py` (new) covering the partial unique index (two
   actives refused, a revoked row not blocking), ownership scoping on every function, `upsert` replacing,
   `deactivate` idempotent, and the `owner_type`/`owner_id` CHECK rejecting an inconsistent row. Plus
   `test_repo_allocations.py`.

**Done when:** `make migrate` runs clean up and down, and no application code reads either table.

---

### Step 2 — Encryption at rest *(0.5 day)*

Touches `pyproject.toml`, `app/core/crypto.py`, `app/config.py`, and tests.

1. Add `cryptography>=44` as an explicit direct dependency. It arrives today via
   `python-jose[cryptography]`, which is an accident of another library's extra — depending on a transitive
   for a security primitive is how a `jose` upgrade silently removes your encryption.
2. Implement both seams with `Fernet`, keyed from `get_settings().ENCRYPTION_KEY`. Build the `Fernet`
   instance once behind an `lru_cache`, not per call — key derivation is not free and the settings object
   is already cached.
3. `decrypt_provider_key` raises a **typed** error on a bad token (`InvalidToken` → a
   `CredentialUnreadable(RuntimeError)` defined in this module), never returns `None` and never returns a
   partial string. A row that will not decrypt is a row written under a rotated `ENCRYPTION_KEY`, and the
   caller must be able to tell that apart from "no key stored".
4. **Validate the key at boot.** `config.validate_startup_config()` gains a call that constructs the
   `Fernet` and round-trips a constant. A malformed `ENCRYPTION_KEY` currently surfaces on the first
   user's first paste; it should kill the process at startup naming the variable, like every other config
   failure in this codebase.
5. Docstrings carry two things the code cannot: that the plaintext must never be logged, returned, or put
   in an error message (`last_4` exists so the UI never needs it back), and that **key rotation is
   `MultiFernet` and is not built** — name the seam, do not implement it.
6. Tests in `tests/unit/test_crypto.py`: round trip; ciphertext differs across two encryptions of the same
   plaintext (Fernet is randomized — assert it, because a deterministic scheme here would leak which users
   share a key); a tampered token raises `CredentialUnreadable`; a token from a *different* key raises the
   same; and `validate_startup_config` fails on a malformed key.

**Done when:** `make typecheck` is green, `pytest tests/unit/test_crypto.py` passes, and no other module
imports Fernet.

---

### Step 3 — The settings endpoints *(1.5 days)*

Touches `app/schemas/keys.py`, `app/api/keys.py`, `app/cache/keys.py`, `app/deps.py`, `app/main.py`, tests.

The BYOK routes go in `app/api/keys.py` as a **second router**, because `development-plan.md` §1 and
`CLAUDE.md` §3 both designate that file for them and its docstring already says so. Prefix
`/v1/provider-keys`, tag `provider-keys`.

1. **Schemas** (`schemas/keys.py`):
   - `ProviderKeyCreateRequest` — `provider: str`, `key: SecretStr`, `nickname: str | None`. **`SecretStr`
     is load-bearing**: a pydantic validation error's `repr` of the model, and any accidental
     `model_dump()` in a log call, both render `**********`.
   - `ProviderKeyOut` — `provider`, `masked` (`"••••a91c"`, built from `last_4`), `nickname`,
     `validation_status`, `last_validated_at`, `last_used_at`, `is_active`, `created_at`. **No `id`
     needed** — the (user, provider) pair addresses the row and keeps the URL readable
     (`DELETE /v1/provider-keys/gemini`).
   - `ProviderKeyStatus` — one row of the settings page for *every* configured provider, including the
     ones with no key: `provider`, `pool: "shared" | "private"`, and the optional `key: ProviderKeyOut`.
     This is what the UI renders; a client should not have to know the provider list to draw an empty row.
2. **Routes**, all requiring a session via the existing `_require_session` (an API-key principal minting or
   listing provider credentials is the same escalation `api/keys.py`'s docstring already refuses):
   - `GET /v1/provider-keys → list[ProviderKeyStatus]` — one entry per **enabled** provider in
     `providers.yaml`, joined against the user's rows. Providers, not slots.
   - `POST /v1/provider-keys → ProviderKeyStatus` — §9.2's add flow, in this order and no other:
     1. rate limit (below);
     2. `registry.adapter_for(provider)` — an unknown or disabled provider is a 400 before anything else;
     3. `await adapter.validate_key(key)`;
     4. `valid=False` → **422 `invalid_provider_key`** carrying `KeyValidation.detail` verbatim, and
        **nothing is written**;
     5. an `Unavailable` raised by the adapter → **503 `provider_unavailable`** with wording that says we
        could not *check* the key, not that it is bad (§9.2's whole point, and the reason `validate_key`
        raises instead of returning `valid=False`);
     6. only now: `encrypt_provider_key`, `provider_keys_repo.upsert(validation_status='valid',
        last_validated_at=now)`, commit.
   - `DELETE /v1/provider-keys/{provider} → 204` — `deactivate`; a provider the user has no key for is a
     **404**, matching every other ownership miss in the codebase.
   - `POST /v1/provider-keys/{provider}/validate → ProviderKeyStatus` — re-check a stored key without
     re-pasting it. Same rate limit. Updates `validation_status` and `last_validated_at` either way; this
     is the one path that may write `'invalid'` from a user action, and it is the settings page's "check
     again" button.
3. **Rate limiting** (D43): `keys.GatewayWindow` gains `"rph"`; `deps.RATE_LIMIT_WINDOW_S` gains
   `{RPH: 3600}`; `RateLimiter.enforce_one(user_id, window, limit)` is factored out of `enforce` so both
   share `_count`, `_refund` and `_retry_after_s`. `api/keys.py` defines
   `KEY_VALIDATION_LIMIT_PER_HOUR = 5` and applies it to `POST /v1/provider-keys` and the `validate`
   route, and to nothing else. A `None` limiter (`RATE_LIMIT_ENABLED=false`) skips it, same as the chat
   path.
4. `main.py` mounts the new router beside the existing one.
5. Tests (`tests/integration/test_provider_keys_endpoints.py`, new):
   - add with a scripted-valid transport → 201-shaped body, row stored, `validation_status='valid'`;
   - add with a scripted 401 → 422, **`list_active_for_user` still empty** (the exit criterion "submit a
     garbage key → nothing persisted", as an assertion);
   - add while the provider is scripted 500 → 503, distinct code from the 422, nothing stored;
   - add twice → one active row, the first deactivated;
   - `GET` lists every enabled provider, `pool` correct for both states;
   - `DELETE` an absent provider → 404; a present one → 204 and the next `GET` reads `shared`;
   - the sixth validation call in an hour → 429 with `Retry-After`;
   - an API-key principal → 403 `session_required`;
   - **the response body of every route in this module contains neither the plaintext nor the ciphertext.**
     Assert on the raw response text, not on parsed fields — a field added later without thinking is
     exactly what this catches.

**Done when:** a user can add, list and remove a provider key, and no request is served differently because
of it.

---

### Step 4 — The resolver *(1 day)*

Implements D36 and D38. Touches `app/keys_resolution/resolver.py` (new), `app/deps.py`, tests.

1. Write the module per D36's sketch: `ResolvedKey`, the `ProviderCredentials` protocol,
   `SystemCredentials`, `UserCredentials`.
2. `SystemCredentials(registry)` is three lines and exists so the router's default is a real object rather
   than a `None` branch inside the loop. Every existing test path goes through it.
3. `UserCredentials(user_id, registry, session_factory)`:
   - `_load_once` — one `list_active_for_user` in a session it opens and closes itself, under an
     `asyncio.Lock`, setting a `_loaded` flag. Never called from `__init__`; `deps` is synchronous.
   - decryption memoized per provider. A `CredentialUnreadable` from Step 2 **falls back to the shared
     pool and logs an error** — a row written under a rotated key must not take the user's gateway down,
     and the log line is the operator's signal. Log the `key_id`, never the ciphertext.
   - `touch_last_used` in the same session as the load, throttled (D38).
   - the module docstring carries §9.3's pseudocode and §9.6's "why per request and not per session".
4. `deps.get_credentials(request) -> ProviderCredentials` — but note the ordering problem: it needs the
   principal, and `app.auth.dependency` imports `deps`, so `Depends(get_principal)` here is the same
   import cycle `get_rate_limiter`'s docstring already documents. **Compose it at the call site**, exactly
   as `api/v1/chat.py` composes `RateLimitDep`. Add a `CredentialsDep` in `api/v1/chat.py` alongside it,
   and export a plain factory from `deps.py` that takes the principal as an argument.
5. Tests (`tests/unit/test_key_resolver.py`, new — it needs a session factory, so an integration-flavoured
   unit test; put it in `tests/integration/` if it needs the real database fixture):
   - no rows → shared pool, `SYSTEM_SCOPE`, the registry's key, `key_id=None`;
   - a Gemini row → private, `scope == str(user_id)`, the decrypted key, `key_id` set;
   - **the mixed case**: a Gemini row and no Groq row, one resolver, two providers, two different scopes.
     This is §9.5 and it is the test that matters most in this step;
   - two `for_provider` calls issue **one** query (count via an instrumented factory);
   - a revoked row is invisible;
   - a row that will not decrypt → shared pool, and an error was logged;
   - `SystemCredentials` answers identically for every provider.

**Done when:** the resolver is complete and unit-tested, and nothing calls it yet.

---

### Step 5 — Threading the answer lane *(1.5 days)*

Touches `app/routing/router.py`, `app/streaming/orchestrator.py`, `app/api/v1/chat.py`, and tests.

Mechanical, wide, and the step most likely to be got subtly wrong. Do it in exactly this order.

1. `route` and `route_stream`: **delete `scope: keys.Scope = keys.SYSTEM_SCOPE`**, add
   `credentials: ProviderCredentials | None = None`. At the top of each function,
   `credentials = credentials or SystemCredentials(registry)`.
2. Inside each candidate loop, replace `key = registry.system_key(spec.provider)` with
   `resolved = await credentials.for_provider(spec.provider)` and use `resolved.key` at the `complete` /
   `stream` call and `resolved.scope` at **every** `quota.*` call in that iteration.
   **There are more of these than the reserve.** `commit`, `release` and `_reconcile_hint` all take a
   scope, and `_reconcile_hint(quota, spec, scope=scope)` appears **four times** across the two loops
   (`router.py:459, 551, 823, 935`). A hint reconciled under the wrong scope corrects a counter the
   request never touched — silently, and in the direction that makes the gateway over-optimistic. Grep for
   `scope=` in this file after the edit; the only survivors should be `resolved.scope`.
3. `RouterOutcome` gains `key_pool: Literal["shared","private"]` — the *winning* attempt's.
   `AttemptRecord` gains `key_pool: str | None = None`, included in `to_json()` (D42).
4. `stream_completion` in `orchestrator.py`: same parameter swap, passed straight through. `_Turn` gains
   `key_pool`, assigned where `extraction_tier` is assigned, read at **both** `done` construction sites —
   the success path and the failure path. Phase 5 trap 8 and Phase 4's `extraction_tier` both hit this;
   a failed stream must report the pool that actually served the last attempt, or `None`, never a stale
   value from an earlier one.
5. `api/v1/chat.py`: both call sites drop `scope=keys.SYSTEM_SCOPE` and pass `credentials=credentials`
   from the new `CredentialsDep`. Delete the two "Constant until Phase 6" comments — they have come true,
   and a stale seam comment is worse than none.
6. Tests:
   - existing router unit tests keep passing **unchanged** (that is what the `None` default buys). If one
     needs editing, the default is wrong.
   - new: a scripted `ProviderCredentials` returning private for one provider and shared for another,
     driven through `route` with a chain that crosses both, asserting `quota.reserve` was called with two
     different scopes and each `complete` got the matching key.
   - new: the same through `route_stream`.
   - integration: `test_chat_endpoint.py` gains a turn served under a stored private key, asserting the
     outbound request carried that key (the mock transport can see the `Authorization` header) and the
     shared key was never sent.

**Done when:** `grep -n "SYSTEM_SCOPE" app/routing app/streaming app/api/v1/chat.py` returns nothing, and
the full suite is green.

---

### Step 6 — Threading the perception lane *(1 day)*

Touches `app/perception/lane.py`, `app/perception/extractors.py`, `app/deps.py`, and tests.

Phase 5's handoff promised "Phase 6 replaces one constant in two lanes rather than one". This is the second
lane, and it is not optional: a user's own Gemini key should read their own documents, on their own budget.

1. `PerceptionResolver.__init__` drops `scope: keys.Scope = SYSTEM_SCOPE` and gains
   `credentials: ProviderCredentials`. `self._scope` disappears; the tier-2 path resolves per candidate.
2. `extract_with_llm` does the same swap. Its candidate loop calls `registry.system_key` at
   `extractors.py:412`; that becomes `credentials.for_provider(spec.provider)`, and `lanes.*` reservation
   calls take `resolved.scope`.
3. `deps.get_resolver` gains `credentials` as a sub-dependency — and hits the same import-cycle
   constraint as Step 4, so it is composed in `api/v1/chat.py` with the rest. Delete its "`scope` is left
   at its default… so Phase 6 replaces one constant in two lanes" docstring paragraph and replace it with
   what is now true.
4. **The extraction cache stays global and unscoped.** `file_extractions` is keyed on `file_hash` alone
   (D24) and `extract:{file_hash}` has no scope segment. Whose key paid for an extraction does not change
   what the bytes say, and scoping the cache per user would re-spend the scarcest budget in the fleet to
   compute the same string twice — which is the exact reasoning D22/D24 already wrote down. Do not touch
   it, and say so in the module docstring so the next reader does not "fix" it.
5. Tests: `test_perception_lane.py` gains a case where the answering candidate resolves private and the
   extraction runs under the user's scope; and one asserting a cached extraction produced under the shared
   pool is still served to a private-key user (point 4, as an assertion).

**Done when:** both lanes resolve per candidate, and `grep -rn "SYSTEM_SCOPE" app/` returns only
`cache/keys.py` (its definition) and `keys_resolution/resolver.py` (its one use).

---

### Step 7 — Quota branching, `quota_scope`, and the disclosure *(1.5 days)*

Implements D39 and D42. Touches
`app/cache/keys.py`, `app/quota/allocations.py` (new), `app/quota/tracker.py`, `app/config.py`,
`config/limits.yaml`, `app/usage/logger.py`, `app/db/repo/requests.py`, `app/memory/canonical.py`,
`app/schemas/chat.py`, `app/streaming/sse.py`, `app/streaming/collector.py`, `app/api/v1/chat.py`, tests.

1. **The key** (D39): `keys.user_allocation(user_id, provider, model)` →
   `q:{user_id}:{provider}:{model}:alloc:rpd`.

   **Not `UNTIL_PROVIDER_RESET`.** That constant means "the provider decides when this window ends", and
   this cap is ours. Give it a rolling-day TTL — the same semantics `RATE_LIMIT_WINDOW_S[RPD]` uses for
   D20 — and say in the docstring why it does not follow `fixed_daily_pt` like the `rpd` counter beside
   it: the cap is a policy of this gateway, and aligning one user's personal allowance to Google's
   Pacific midnight would reset it at an hour neither they nor we chose.

   The docstring also carries the amendment note, in the shape `keys.rate_limit`'s already does for
   ADR-022: this key is **an addition to a frozen Contract C, made with sign-off**, ADR-036 records why,
   and nothing had ever written it so there was nothing to migrate. That paragraph is the reason the next
   reader does not have to wonder whether the rule was followed.
2. **The ceiling**: `config/limits.yaml`'s `gateway:` block gains `shared_pool_daily_cap` per tier;
   `GatewayLimits` gains the field (`int | None = None`, so the YAML can omit it and nothing changes).
   `db/repo/allocations.get_cap` overrides it per (user, provider, model).
3. **`app/quota/allocations.py`** — one public function:

   ```python
   async def shared_pool_grants(
       spec: ModelSpec, *, user_id: UUID, cap: int | None
   ) -> tuple[WindowGrant, ...]:
       """§9.4's extra ceiling on the shared path. Empty when there is no cap."""
   ```

   Returns zero or one `WindowGrant` at `keys.user_allocation(...)`, `cost_is_tokens=False`. The caller
   appends it to the grants `QuotaTracker.reserve` would have built and calls `reserve_windows` — which is
   why Phase 3 Step 5 pulled that method out in the first place. Module docstring carries D39's "this is
   not D20 again" paragraph.
4. **Where it plugs in.** `QuotaTracker.reserve` gains `extra_grants: tuple[WindowGrant, ...] = ()`,
   appended to the ones it derives. The router passes them only when `resolved.pool == "shared"` — the
   private path has no cap by construction (§9.4), and the branch is one `if`. Fetching the cap needs a
   session, so the cap is resolved **by the credentials resolver alongside the key** and carried on
   `ResolvedKey.shared_daily_cap: int | None`. That keeps the router free of database access and the
   number arrives on the same object as the decision it belongs to.
5. **`requests.quota_scope`**: `usage/logger.py`'s `record_success`, `record_failure` and
   `record_stream_failure` gain `quota_scope: str = "system"` and pass it to `requests_repo.create`, which
   already accepts it. `chat.py` and `Collector` source it from the outcome/result. `record_cache_hit`
   keeps `"system"`… no — a cache hit spent no scope; give it `quota_scope="system"` explicitly with a
   comment, matching how `messages_dropped=0` and `extraction_tier=None` are handled on that path
   (phase5 trap 3).
6. **`key_pool` on the wire** (D42): `MessageMeta.key_pool`, `ChatCompletionResponse.key_pool`,
   `DoneEvent.key_pool`, populated exactly the way `extraction_tier` is on both paths. `None` on a cache
   hit.
7. Tests:
   - unit, `test_quota_allocations.py`: grants built and not built; the cap from YAML; the row override
     winning.
   - unit, `test_cache_keys.py`: the new builder's format and its segment validation.
   - integration, `test_chat_endpoint.py`: a shared-pool turn increments both `q:system:…:rpd` and
     `q:{user}:…:alloc:rpd`; a private-pool turn increments `q:{user}:…:rpd` and **neither** of those two;
     a user at their personal cap is skipped on the shared path with `skipped_quota` while another user is
     not — the whole point of the cap, asserted.
   - integration: `requests.quota_scope` reads `system` for one turn and `<user_id>` for the next, on the
     same conversation, with a key added in between. **This is exit criterion 4.**
   - unit + integration for `key_pool` on both response shapes and the stored `meta`, including the
     absent-key-reads-`None` round trip.

**Done when:** one conversation, two consecutive turns, two different `quota_scope` values in `requests`,
and the counters moved where they should have.

---

### Step 8 — The key never reaches a log *(0.5 day)*

Touches `tests/` only, plus any leak it finds.

Exit criterion "grep all logs for the key string → zero hits" is a manual step in `development-plan.md`. It
should be a test, because a manual grep passes once and a test passes forever.

1. Add `tests/integration/test_credential_leakage.py`. Capture structlog's output for the whole flow
   (`tests/unit/test_logging.py` already establishes how) with a sentinel key value distinctive enough to
   grep for, and drive: add the key → list keys → send a non-streaming turn → send a streaming turn →
   force an `AuthFailed` on the private key so D40's failure path runs → remove the key.
2. Assert the sentinel appears in **no** captured log record, in no response body, and in no
   `requests.attempts` JSON. Assert the *ciphertext* does not appear either — an encrypted credential in a
   log is still a credential in a log the day the encryption key leaks.
3. Assert the same for an unhandled exception's path: force a provider adapter to raise with the key in
   scope and confirm `unhandled_exception_handler`'s output carries neither. This is the realistic leak —
   a traceback with local variables, or a `repr` of a payload.
4. If it finds a leak, **fix the leak in this commit** and say where it was. The likely candidates, in
   order: an adapter logging request headers on a retry, `_serializable_errors` (already safe — it drops
   pydantic's `input` field; verify it stays that way), and `AttemptRecord.to_json`.

**Done when:** the test is green and names, in its docstring, exactly which surfaces it covers and which it
cannot (a provider's own logs, and anything written before this test existed).

---

### Step 9 — `/v1/models` personalization *(1 day)*

Implements D41. Touches `app/config.py`, `config/providers.yaml`, `config/limits.yaml`,
`app/providers/registry.py`, `app/api/v1/models.py`, `app/api/v1/chat.py`, tests.

Two halves, and the first is a correctness fix the second depends on.

1. **Per-caller scope.** `list_models` currently computes every candidate's status at `SYSTEM_SCOPE`. Once
   scopes diverge that is simply wrong: a user with a private Gemini key sees "rate_limited" for a budget
   they are not spending. Resolve per candidate through `CredentialsDep` and pass `resolved.scope` to
   `quota.remaining`. The memo key becomes `(provider, model, scope)`.
   Delete the `del principal` line and the seam comment.
2. **Private-key-only slots.** `Slot` gains `requires_private_key: bool = False`.
   - `ProviderRegistry` gains `requires_private_key(slot) -> bool`, built the same way `internal_slots` is
     — a `frozenset` computed in `build_registry`.
   - `list_models` includes such a slot only when the caller resolves `private` for **every** provider in
     its candidate chain. In practice that is one provider; write the check for the general case anyway,
     because a two-provider private slot is a config edit away and a check that assumes one is a trap.
   - `chat.py::_validate_slot` refuses it for anyone else with the existing unknown-slot 400 — same
     treatment `internal` gets, and reuse that code path rather than adding a second refusal shape.
   - `selection.candidates` needs no change: an unroutable slot never reaches it.
   - **`auto` must not include a private slot's candidates** for a user who has the key. `auto`'s promise
     is "the gateway picks"; silently routing to a model only one user can reach makes their `auto`
     unreproducible and their cache entries unshareable. Assert it.
3. `config/providers.yaml` gains the `pro` slot — one Gemini Pro candidate, `requires_private_key: true`,
   with the matching `config/limits.yaml` block. Comment it the way `perception` is commented: what it is
   for, why it exists, and that the shared key genuinely cannot serve it.
4. Tests (`test_models_endpoint.py`): a user with no keys sees today's list exactly; a user with a Gemini
   key sees `pro` and the other user does not; the same user's `general` status reflects *their* counters;
   `POST /v1/chat/completions` with `"model": "pro"` answers for the key holder and 400s for everyone
   else; `auto` never selects a `pro` candidate.

**Done when:** two accounts hit `/v1/models` in the same test and get different, correct answers.

---

### Step 10 — Frontend, ADRs, and docs *(2 days)*

Touches `frontend/lib/types.ts`, `frontend/lib/api.ts`, `frontend/lib/hooks.ts`,
`frontend/lib/provenance.ts`, `frontend/components/ModelIndicator.tsx`,
`frontend/components/AccountDialog.tsx`, `frontend/components/ProviderKeysSection.tsx` (new),
`frontend/tests/`, plus `docs/`.

**Frontend:**

1. `types.ts`: `ProviderKeyStatus`, `ProviderKeyOut`; `key_pool?` on `ChatCompletionResponse` and
   `DoneEvent` (optional, like `extraction_tier`); `key_pool` on `MessageMeta`.
2. `api.ts`: `listProviderKeys`, `addProviderKey`, `removeProviderKey`, `revalidateProviderKey`.
3. `hooks.ts`: `useProviderKeys` (SWR). On a successful add or remove, **mutate `/v1/models` too** —
   §9.7's slot appears or disappears, and a stale picker is the one place the user would notice the
   feature not working.
4. `provenance.ts`: `keyPool: "shared" | "private" | null` in all four constructors. `fromMetaEvent`
   reports `null` (the `meta` event does not carry it); `fromMessageMeta` reads the stored value.
5. `ModelIndicator.tsx`: a seventh rule, in the disclosure register — *"served by your own Gemini key"*
   when `keyPool === "private"`. Say nothing when it is `"shared"`: the shared pool is the default and a
   badge on every message is noise.
6. `ProviderKeysSection.tsx`, rendered inside `AccountDialog` (whose docstring already anticipates it —
   update that docstring). Per §9.2 and §9.8:
   - one row per provider from `GET /v1/provider-keys`, so an empty state is still a full list;
   - status text *Using shared pool* / *Using your key*, the masked value, and Add / Remove;
   - a masked input (`type="password"`), never pre-filled, cleared on success **and on failure**;
   - the 422 rendered inline as the provider's own wording, not as a toast that vanishes;
   - the 429 rendered as a wait, reusing whatever `ErrorState` does with `isRateLimited`;
   - a `validation_status === 'invalid'` row rendered as a warning with a re-check action (D40's payoff);
   - **the §9.8 disclosure, in plain language, above the rows**: requests made with your own key are
     billed to and governed by that provider's terms, not ours. Not a tooltip. The perception lane's
     third-party-extraction disclosure (Phase 4 Step 11) is the tone to match.
7. Frontend tests: `ProviderKeysSection.test.tsx` covering the six states (no key, adding, invalid key
   error, rate-limited, active key, invalid stored key); `ModelIndicator.test.tsx` gains the private-pool
   case and asserts silence on `"shared"`; `provenance.test.ts` adds `keyPool` to the shared `facts`
   object of the two-transport agreement test, so a field added to `DoneEvent` and forgotten on the
   response fails there.

**ADRs** (`context / decision / consequences / why`, matching the existing 33):

- **ADR-034 — Per-candidate credential resolution** (D36 + D38). Why the credential and the scope are one
  object, why it is per candidate and not per request, why the router gets a `session_factory`-backed
  resolver rather than a session, and why `None` defaults to the system implementation.
- **ADR-035 — The shared pool stays in the environment** (D37). The four reasons, and `MultiFernet` named
  as the rotation seam.
- **ADR-036 — Personal caps under a frozen Contract C** (D39). The three options, why `q:{user_id}:…:rpd`
  reuse is the subtly wrong one (the mid-day pool switch that carries a stale count across two meanings),
  and how `reserve_windows`'s Phase 3 generalization paid for itself a second time. This ADR is also the
  **record of the amendment itself** — Contract C is frozen, this is the second key added to it with
  sign-off, and ADR-022 is the shape to follow: state what was added, that nothing had written it, and
  therefore that there was nothing to migrate. Cross-reference ADR-027, which made the same
  sub-counter argument for the perception lane.
- **ADR-037 — A failing private key is not laundered through the shared pool** (D40). The strongest
  "product judgment" entry in this phase.
- **ADR-038 — Private-key-only slots** (D41). Why config declares it rather than `KeyValidation.models`
  deciding it, and why `auto` never selects one.
- **ADR-039 — Rate-limiting the validation endpoint** (D43). Including the note that a third `GatewayWindow`
  value is not a Contract C format change, and why that was worth checking.

D42 gets no ADR; say so in ADR-034's consequences.

**Docs:**

- `docs/limitations.md` — a new BYOK section: the exact cache is keyed on slot + history + params and
  **not** on user, so under `auto` a private key can produce an answer another user is later served from
  cache (ADR-023 weighed this class of thing already; this is a new instance of it, and it is documented
  rather than discovered); one active key per provider per user; no rotation; the shared pool's keys stay
  in the environment; a key's validation status is a snapshot, not a live fact; and what the leak test of
  Step 8 does and does not cover.
- `docs/architecture.md` — a "Phase 6: two pools, one request" section after Phase 5's. The picture worth
  drawing is a **single failover chain crossing a scope boundary**: candidate 1 (Gemini, private key,
  `q:{user}`), candidate 2 (Groq, shared key, `q:system`) — because that is the thing that is genuinely
  hard to see from the code and the thing §9.5 is actually about.
- `docs/deploy.md` — `ENCRYPTION_KEY` generation and, more importantly, **what happens if it is lost or
  rotated**: every stored user key becomes unreadable and the resolver falls back to the shared pool with
  an error per row. That is the operational fact this phase adds.
- `README.md` — BYOK in the feature list, framed around the demo: add your own key mid-conversation, the
  next message uses it, remove it and the one after does not.
- `.env.example` — no new variables, but the `ENCRYPTION_KEY` comment should stop saying "for BYOK
  (Phase 6)" and start saying what it now actually guards.
- `CLAUDE.md` — via the `update-claude-md` skill, not by hand.

**Done when:** all five `make` targets are green and the definition-of-done's eight steps are demoable in a
browser.

**Landed.** The frontend touched every file this step names plus one it does not,
`frontend/components/ErrorState.tsx` — `formatWaitSeconds` is now exported so the 429 on this surface
rounds a wait the same way the chat surface does, rather than growing a second copy. `types.ts` gained
`KeyPool` (a named type, since four files restate it), `key_pool` on `MessageMeta` /
`ChatCompletionResponse` / `DoneEvent`, and the three BYOK shapes; `provenance.ts` carries `keyPool`
through all four constructors, `fromMetaEvent` reporting `null` because a restart can change the pool
mid-stream and `fromMessageMeta` *reading* the stored value — unlike `warning`, which pool paid for a turn
is a fact about the turn rather than about the request, so a reopened thread still discloses it. Rule 7 in
`ModelIndicator` renders only `"private"`. `ProviderKeysSection` is the new component, rendered in
`AccountDialog`, each row a labelled `role="group"` so three identical "Remove" buttons stay
distinguishable to a screen reader and to a test. The hook test moved to its own file
(`frontend/tests/useProviderKeys.test.tsx`, the `useSendMessage.test.tsx` precedent) rather than fighting
`ProviderKeysSection.test.tsx`'s own module mock. One backend file changed:
`app/cache/keys.py`'s module docstring now records **both** Contract C amendments together, since the exit
checklist asks for the second one to be as visible as ADR-022's and only the `user_allocation` builder's
own docstring carried it. Six ADRs, the limitations section, the architecture diagram, the deploy note,
the README section and the `.env.example` comment all landed as specified.

---

## 5. Traps

1. **`scope=` appears more times in `router.py` than the reserve call.** `commit`, `release` and
   `_reconcile_hint` all take one, and `_reconcile_hint` is called four times across the two loops. A hint
   applied under the wrong scope corrects a counter the request never touched, in the optimistic
   direction, silently. Grep the file after Step 5.
2. **`get_credentials` cannot depend on the principal inside `deps.py`.** `app/auth/dependency` imports
   `deps`, so a `Depends(get_principal)` there is an import cycle — the same one `get_rate_limiter`'s
   docstring already documents. Compose at the endpoint, as `RateLimitDep` does.
3. **A cache hit resolves nothing.** No key is spent, no scope moves, `key_pool` is `None`,
   `quota_scope` is `"system"`, and `last_used_at` is not touched. This is the same split
   `extraction_tier` and `messages_dropped` already make on that path — follow it rather than inventing a
   fourth answer.
4. **Removing a key must not delete `q:{user_id}:…` counters.** They are the record of what that key
   really spent, they carry their own TTL, and clearing them would let a user reset their own daily usage
   by removing and re-adding a key. Deactivate the row; touch nothing in Redis.
5. **The `provider_keys` unique index must be partial.** `UNIQUE (owner_id, provider)` without
   `WHERE is_active` makes remove-then-re-add fail forever, because the removed row is a soft delete.
6. **`validate_key` raising is not `valid=False`.** The adapters already get this right and the endpoint
   must not flatten it — a 503 saying "we could not check this key" and a 422 saying "this key is bad" are
   different sentences for the user, and §9.2 exists because of the confusion the second one causes when
   the first is true.
7. **`SecretStr` on the request model is not decoration.** It is what keeps a pydantic validation error,
   a `model_dump()` in a log call, and a debugger's `repr` from carrying a live credential.
8. **`DoneEvent` has two construction sites.** The success path and the failure path. Phase 4 missed the
   second with `extraction_tier` and Phase 5 wrote it up as trap 8; `key_pool` is the third field to make
   this trip and the trap has not moved.
9. **The extraction cache stays unscoped.** `file_extractions` is keyed on `file_hash` alone by D24, and
   scoping it per user to "keep private-key work private" would re-spend Gemini's budget to recompute an
   identical string. If that trade ever needs revisiting, it is a decision with an ADR, not a Step-6 edit.
10. **`auto` must never route to a private-key-only slot.** It makes one user's `auto` unreproducible and
    their cache entries unshareable, and it is the kind of thing that looks like a feature until someone
    asks why two accounts get different answers to the same question.
11. **Do not build an admin UI for `user_quota_allocations`.** The table is read-only this phase. `api/admin.py`
    is Phase 7's slot and the temptation to fill it here is exactly how a phase stops being demoable.
12. **`quota_scope` on a *failed* turn is still real.** `record_failure` must carry the scope the attempts
    actually used, not `"system"` by default — a private key that is rate-limited produces rows that are
    only interpretable if the scope is right.
13. **The personal cap is not the gateway rate limit.** Different key, different question, opposite
    failure rule (D39 fails closed with quota; D20 fails open). Two modules, and the docstrings must say
    which is which or someone will delete one as a duplicate.
14. **`last_4` comes from the plaintext, once, at add time.** There is no path that recovers it later
    without decrypting, and no reason to add one.

---

## 6. Test matrix

| Layer | What | Where |
|---|---|---|
| Unit | Fernet round trip, randomized ciphertext, tampered token, wrong-key token, boot validation | `tests/unit/test_crypto.py` (new) |
| Unit | `keys.user_allocation` format and segment validation | `tests/unit/test_cache_keys.py` |
| Unit | `shared_pool_grants`: built, not built, YAML default, row override | `tests/unit/test_quota_allocations.py` (new) |
| Unit | `route`/`route_stream` under a scripted resolver: two providers, two scopes, two keys | `tests/unit/test_router.py` |
| Unit | `RateLimiter.enforce_one` on an `rph` window | `tests/unit/test_rate_limiter.py` |
| Integration | `provider_keys` partial unique index, ownership scoping, upsert, CHECK | `tests/integration/test_repo_provider_keys.py` (new) |
| Integration | Resolver: no rows, one row, **mixed providers**, revoked, undecryptable, one query | `tests/integration/test_key_resolver.py` (new) |
| Integration | Add valid / add invalid (nothing stored) / add while provider is down (503) / add twice / delete / 404 / rate limit / API-key principal 403 | `tests/integration/test_provider_keys_endpoints.py` (new) |
| Integration | Shared turn moves `q:system` **and** `alloc:rpd`; private turn moves only `q:{user}` | `tests/integration/test_chat_endpoint.py` |
| Integration | Personal cap exhausted → `skipped_quota` for that user, not for another | same |
| Integration | Same conversation, key added between turns, `requests.quota_scope` changes | same |
| Integration | Private key `AuthFailed` → chain proceeds, row marked `invalid`, shared key never sent | same |
| Integration | Extraction under a private key; a shared-pool extraction still served to a private-key user | `tests/integration/test_perception_lane.py` |
| Integration | Two accounts, one `/v1/models` call each, different slot lists and different statuses | `tests/integration/test_models_endpoint.py` |
| Integration | `"model": "pro"` answers for the key holder, 400s for everyone else; `auto` never picks it | `tests/integration/test_chat_endpoint.py` |
| Integration | **Sentinel key appears in no log record, response body, or `attempts` JSON** | `tests/integration/test_credential_leakage.py` (new) |
| Frontend | Six settings states; picker refreshes after add/remove | `frontend/tests/ProviderKeysSection.test.tsx` (new) |
| Frontend | Indicator discloses `private`, stays silent on `shared` | `frontend/tests/ModelIndicator.test.tsx` |
| Frontend | `keyPool` in the two-transport agreement test | `frontend/tests/provenance.test.ts` |

Coverage concentration for this phase: `keys_resolution/`, `quota/allocations.py`, and `api/keys.py`.

---

## 7. Documentation

| Document | Change |
|---|---|
| `docs/decisions/ADR-034-per-candidate-credential-resolution.md` | new (D36, D38) |
| `docs/decisions/ADR-035-shared-pool-stays-in-the-environment.md` | new (D37) |
| `docs/decisions/ADR-036-personal-caps-under-frozen-contract-c.md` | new (D39) |
| `docs/decisions/ADR-037-private-key-failure-is-not-laundered.md` | new (D40) |
| `docs/decisions/ADR-038-private-key-only-slots.md` | new (D41) |
| `docs/decisions/ADR-039-validation-endpoint-rate-limiting.md` | new (D43) |
| `docs/limitations.md` | new BYOK section — cache/user asymmetry, one key per provider, no rotation, snapshot validation status, leak-test coverage |
| `docs/architecture.md` | "Phase 6: two pools, one request" — one chain crossing a scope boundary |
| `docs/deploy.md` | `ENCRYPTION_KEY` generation, and what a lost or rotated key does to stored rows |
| `README.md` | BYOK in the feature list, framed as the demo |
| `.env.example` | `ENCRYPTION_KEY` comment updated from seam to fact |
| `CLAUDE.md` | phase status, via the `update-claude-md` skill |
| `doc/reference/phase6.md` | this file — kept accurate as steps land, the way phases 3–5 were |

---

## 8. Exit checklist

- [ ] Migration `0005` up and down clean; `provider_keys` and `user_quota_allocations` exist with their
      CHECKs and the **partial** unique index.
- [ ] `ENCRYPTION_KEY` is validated at boot; a malformed one kills the process naming the variable.
- [ ] A garbage key returns a clear provider-worded error and **nothing is persisted**.
- [ ] A provider that is *down* during an add returns a distinct 503, not "your key is bad".
- [ ] The validation endpoint refuses the sixth attempt in an hour with a `Retry-After`.
- [ ] Adding a key mid-conversation changes the very next message's pool — no reload, no re-login.
- [ ] Removing it reverts the message after that.
- [ ] One request, two providers, two scopes: `q:{user_id}:gemini:*` and `q:system:groq:*` both move.
- [ ] `requests.quota_scope` carries the real value on success **and** on failure.
- [ ] A shared-pool turn also counts against `q:{user}:…:alloc:rpd`; a private one does not.
- [ ] The one Contract C addition is recorded in ADR-036 **and** in `cache/keys.py`'s own docstring, the
      way ADR-022's amendment is — and it is the *only* key builder this phase adds.
- [ ] A user at their personal cap is skipped on the shared path while another user is not.
- [ ] A private key's `AuthFailed` fails over to the next *provider*, marks the row invalid, and never
      sends the shared key to that provider.
- [ ] `/v1/models` differs between two accounts, correctly, in both status and slot list.
- [ ] `auto` never selects a private-key-only candidate.
- [ ] `key_pool` is on the response, the `done` event, the stored `meta`, and the UI — and is `null` on a
      cache hit.
- [ ] The credential-leakage test is green and covers both lanes plus the unhandled-exception path.
- [ ] `grep -rn "SYSTEM_SCOPE" app/` returns only `cache/keys.py` and `keys_resolution/resolver.py`.
- [ ] `RESERVED_BLOCK_TYPES`, `memory/summarize.py`, `FitStrategy` and `list_for_conversation` untouched.
- [ ] Six ADRs, the limitations section, the architecture diagram, the deploy note, the README.
- [ ] `make test`, `make lint`, `make typecheck`, `make frontend-test`, `make frontend-lint` all green.

---

## 9. What Phase 6 hands to Phase 7

**`quota_scope` stops being a constant, which is what makes the usage dashboard interesting.** Phase 7's
`api/admin.py` can now break request volume down by pool, and "how much of the shared free tier is one
user consuming" becomes a query rather than an assumption. `requests.attempts` carries `key_pool` per
attempt, so a failover from a private key to the shared pool is visible in the row that recorded it.

**`user_quota_allocations` is read but never written by a UI.** Phase 7's admin surface is where that
lands, and the repo function it needs (`get_cap`) already exists with the shape a `set_cap` would mirror.

**`config/pricing.yaml` (§4.8's simulated cost) now has a real question to answer.** A private-key request
is billed to the user's own provider account; a shared one is not. Whether the dashboard shows one number
or two is Phase 7's call, and the `key_pool` field on every row is what makes either answer possible.

**Left deliberately unbuilt, seams visible:** `keys.idempotency` (D6, Phase 7); `MultiFernet` key rotation
(ADR-035); `owner_type='system'` rows in `provider_keys` (D37); `KeyValidation.models` persistence
(D41); an admin surface for allocations; `memory/summarize.py`; `pin_target`'s tool branch;
`fitting.FitStrategy`'s second member; message pagination (Phase 7 task 6, and `list_for_conversation`
stays unpaginated when it lands).
