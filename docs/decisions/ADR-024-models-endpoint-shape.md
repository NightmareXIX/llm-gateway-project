# ADR-024 — `GET /v1/models` returns OpenAI's envelope, with our fields inside it

**Status:** accepted · Phase 3, Step 7 · 2026-08-17
**Implements:** `phase3.md` §3 D21
**Relates to:** [ADR-020](ADR-020-quota-reservation-placement.md) (`selection.candidates()`'s
purity is what lets this endpoint reuse it), [ADR-011](ADR-011-named-slot-spill.md) (D2's spill
behavior this endpoint's status has to describe honestly, not contradict)

## Context

`project-overview.md` §8 sketches `GET /v1/models` as a bare array:
`[{"slot": "auto", ...}, {"slot": "llm1", ...}]`. That shape does not survive contact with the fact
that this gateway exposes an OpenAI-compatible `POST /v1/chat/completions` — any SDK pointed at it,
including OpenAI's own, calls `client.models.list()` expecting `{"object": "list", "data": [...]}`
with each entry at least resembling `{"id": ..., "object": "model", "created": ..., "owned_by":
...}`. A bare array under `/v1/models` breaks that client the moment it tries to enumerate models,
which is a strange failure mode for an endpoint whose entire purpose is letting a client discover
what it can ask for.

## Decision

**The OpenAI envelope, carrying our fields alongside theirs.** `data` entries hold OpenAI's `id`
(the slot name — `auto`, or a configured slot like `llm2`), `object: "model"`, a placeholder
`created`, and `owned_by` (the primary candidate's provider; `None` for `auto`, which has no single
owner) — *plus* `status`, `resets_at`, `description`, and a `candidates` array carrying each
candidate's own status, breaker state, and remaining budget per window. An SDK that ignores unknown
fields sees a valid, spec-shaped model list; this project's own frontend reads the rest.
`schemas/chat.py` already made this same trade in Phase 1, for the same reason: a client contract
that is inconsistent about which half of "OpenAI-shaped, plus everything we actually know" it
honors is worse than committing to both halves.

`auto` is always the first entry, with `status` computed as the best status across the whole
routable fleet — `selection.candidates(registry, "auto")`, the exact list the router would walk for
an `auto` request, not a hand-rolled approximation of it.

**Status is derived from local state only — no upstream call, ever, not even a cheap models-list
probe.** Per candidate, in order: the breaker's *stored* state (`CircuitBreaker.peek`, never
`.allows`, which would claim a half-open probe slot as a side effect a read-only status check must
not have) — `open`/`half_open` means `unavailable`. Otherwise any exhausted quota window means
`rate_limited`. Otherwise, if the tracker cannot answer at all — Redis unreachable, or the model
declares no windows — `unknown`. Otherwise `available`. Per slot, the status is the *best* of its
candidates, and `resets_at` is the *earliest* reset across all of them, not just the ones sharing
the winning status.

**Authenticated (`PrincipalDep`), even though the response is identical for every caller today.**

## Why

**Breaking an SDK's assumption about `/v1/models` costs more than it saves.** The whole point of
exposing an OpenAI-shaped chat endpoint is that existing tooling works against this gateway with a
changed base URL and nothing else. A model-list endpoint that returns a bare array technically
answers the question but fails every client that calls `.list()` expecting the standard envelope —
which is a worse failure than adding fields an unaware client will simply never look at.

**`selection.candidates()`'s purity, established for the router (ADR-020), is what makes this
endpoint cheap to build and impossible to drift from routing reality.** Building `auto`'s fleet from
the exact function the router itself calls means this endpoint cannot report a status for a
candidate the router would never actually try, or omit one it would — the two are, by construction,
the same list. A hand-maintained second list here would be a second place D2's spill logic and
D11's latency ranking have to be kept in sync, which is exactly the kind of drift a status endpoint
exists to prevent, not introduce.

**A slot's status is the best of its candidates because that is precisely what the failover chain
already does.** `auto` and a named slot both silently spill across a chain (D2, ADR-011) — a slot
with one unhealthy candidate ahead of a healthy one *is* servable, because that is exactly what a
real request against that slot will experience. Reporting the slot as `rate_limited` because its
first candidate is exhausted, while its second candidate is sitting there healthy, would tell a
client "this doesn't work" about a slot that, in fact, works.

**Zero upstream calls, stated as a hard invariant rather than a preference (trap 11).** Every number
this endpoint reports — the breaker hash, the quota counters, the registry's static slot table — is
already sitting in Redis or in memory by the time a request for `/v1/models` arrives. A status
endpoint that called even one provider to confirm its own answer would turn a page load into a round
trip against the exact budget it exists to report on, which is a worse failure mode than reporting a
slightly stale `unknown` and being honest about it.

**`unknown` exists as its own status because a status page that lies in the confident direction is
worse than one that admits the gap.** A model with no published limits, or a Redis that cannot be
reached to check them, is not the same claim as "available" — the former is a fact about the
model's configuration, the latter is degraded information about a live system, and collapsing
either into `available` would tell a client to route somewhere the gateway genuinely does not know
is safe.

**Authenticated from day one, on a phase boundary rather than a later breaking change.** Phase 6
personalizes this endpoint per user (§9.7 — a private key can unlock an extra slot others do not
see). An endpoint that starts open and later requires a credential is a breaking change to every
existing caller; one that always required a credential, even while every caller sees the same
response, absorbs that change invisibly when Phase 6 actually starts varying the payload.

## Consequences

- `created` is a fixed placeholder, not a real timestamp — there is no meaningful "model creation
  time" for a logical slot, and inventing one would imply a precision this endpoint does not have.
- The integration suite's status matrix forces one breaker open and one counter to its limit in the
  same test run and asserts all three non-`available` statuses plus a healthy fourth candidate
  simultaneously, specifically to catch a regression that only shows up when more than one failure
  mode is live at once — the case a real outage actually looks like.
- Every response asserts **zero** calls against the mock transport, not merely a fast response time
  — a future change that adds an upstream call here would still pass a naive latency-based test.
