# ADR-027 — Perception quota under a frozen Contract C: a daily fence, a shared minute

**Status:** accepted · Phase 4, Step 5 · 2026-08-22
**Implements:** `phase4.md` §3 D26
**Relates to:** Contract C (`app/cache/keys.py::quota_perception_lane`); D8 (the 50/50 split this
decision implements); [ADR-020](ADR-020-quota-reservation-placement.md) (the reserve → commit/release
lifecycle the lane reuses); [ADR-019](ADR-019-quota-window-model.md) (why the provider-side counters
are fixed windows in the first place)

## Context

Contract C gives the perception lane exactly **one** key: `q:{scope}:{provider}:{model}:lane:perception`,
TTL "until reset." The answer lane has four (`rpm`, `rpd`, `tpm`, `tpd`). D8 froze the split at 50/50
and `reserved_fraction: 0.5` has been sitting in `config/providers.yaml` since Phase 3 — but a single
key cannot symmetrically fence four windows, so which windows the fence actually applies to was left
open, and Contract C is frozen: adding three more keys is not a free option.

## Decision

**The fence is daily. Per-minute windows are shared, checked twice against two different ceilings.**

- **Daily** (`rpd`, and `tpd` where a model declares one): the perception lane increments
  `lane:perception` and never `rpd`. Its limit is `floor(published * reserved_fraction * (1 -
  headroom))` — `lanes.perception_budget`, spendable for the first time this phase. The answer lane's
  `rpd` is already `floor(published * answer_share * (1 - headroom))`. The two halves sum to the
  published limit minus headroom, and neither can spend the other's.
- **Per minute** (`rpm`, `tpm`): one shared Redis counter, two ceilings enforced against it. Chat
  checks it against the halved limit it has always used; the lane checks the *same key* against the
  **full** published limit. The lane may spend whatever minute chat has not, chat can never push past
  half, and the total across both can never exceed what the provider actually publishes.

The lane gets the same reserve → commit/release lifecycle the answer lane has had since Phase 3
(`lanes.reserve_perception` / `commit_perception` / `release_perception`), all three delegating to the
existing Lua scripts with the lane's key substituted in. **No new Lua.**

## Why

**Starvation is a daily problem, and the fence should match the shape of the failure it prevents.**
"Gemini's budget is gone and it is 2pm" is exactly what D8 exists to stop, and it is a failure of the
*daily* counter running out early. A per-minute collision between one chat turn and one extraction
resolves itself in under sixty seconds on its own — fencing the minute as well would mean the lane
refuses to read a document while five requests per minute of real Gemini capacity sit unused, in
service of a race that was never the actual risk.

**Four more keys was the wrong fix, for a reason stronger than "it's more code."** Contract C is
frozen because a key format that exists in two places drifts silently — the writer and the reader stop
meeting, and a counter reads zero forever with nothing to say why. Phase 3's one amendment to the
contract (`rl:{user_id}:{window}:{window_start}`) was made because the original key was *provably
incorrect* — one key addressing two windows collided at every midnight UTC. Adding perception-specific
`rpm`/`tpm`/`tpd` keys here would be an amendment made for tidiness against a problem — per-minute
starvation — that the daily fence and a sixty-second self-correcting window already solve without one.

**`QuotaTracker._effective_limit` bakes `answer_share` into every limit it computes, and that is
correct for chat and wrong for the lane.** Threading a `lane` parameter through five call sites deep
in the tracker to special-case one caller was rejected in favor of a narrower fix: the lane functions
in `quota/lanes.py` compute their own `WindowGrant`s directly from `perception_budget` and hand them
to `QuotaTracker.reserve_windows`, a small internal entry point Step 5 added for exactly this. The
answer lane's path — `reserve()` deriving grants from `_budget(spec)` — is untouched.

**Model config, not code, decides which Gemini models the lane may call.** `config/providers.yaml`'s
`perception` slot lists the same two Gemini models `general` and `fast` already declare, in
capability order, marked `internal: true` so `registry.slots()` and `GET /v1/models` skip it and a
client naming it explicitly gets the same 400 a typo would. A startup check fails boot if a model's
`reserved_fraction` disagrees between an answer slot and the `perception` slot (trap 19) — the one
failure mode that would silently break D8's arithmetic (the two halves stop summing to one) without
ever raising an exception on its own.

## Consequences

- A native passthrough (tier 1) makes **no** perception reservation at all — the bytes ride in the
  answering model's own payload and are already counted by the reservation `router.py` makes for that
  attempt, through the attachment's `token_cost` (D27). Reserving from the perception lane as well
  would double-count one request against one real budget (trap 7); this is the specific claim the
  Step 8 live-traffic verification checked on real counters.
- The lane's stampede guard (`lock:extract:{file_hash}`) protects the daily fence from concurrent
  duplication the way the reserve script alone cannot — a second identical extraction that started
  before the first committed would spend a second daily slot for a reading the first one is about to
  produce.
- `docs/limitations.md`'s standing caveat about Gemini's quota being metered per Google Cloud project,
  not per Redis deployment, applies identically to the perception half — two environments sharing one
  Gemini key share one real daily budget at Google no matter how correctly each keeps its own Redis
  counters.
- A deployment whose `config/providers.yaml` predates D26's `perception` slot degrades to tier 3 for
  every document rather than raising — `extractors.extract_with_llm` logs `perception.slot_missing`
  and returns `None`, which the lane reads exactly like an exhausted chain.
