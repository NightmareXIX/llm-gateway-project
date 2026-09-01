# ADR-042 — Idempotency: claim before routing, an envelope rather than a bare id, fail open

**Status:** accepted · Phase 7, Steps 5–6 · 2026-09-01
**Implements:** `phase7.md` §3 D47 — the behaviour behind **D6**, the last locked decision in
`contracts-and-phase1.md` §1 to have had no code
**Does not amend:** Contract C (`app/cache/keys.py`) — the key *format* is unchanged; see below
**Relates to:** [ADR-023](ADR-023-exact-cache-identity-and-scope.md) (the other Redis policy over the
same request, and the one it must not be confused with), [ADR-016](ADR-016-streaming-session-lifetime.md)
(why the collector, not the endpoint, completes a streamed claim),
[ADR-010](ADR-010-redis-fail-open-and-readiness.md) / [ADR-018](ADR-018-quota-fails-closed.md) (the
two opposite Redis rules, and which one this takes)

## Context

D6 says: an optional `Idempotency-Key` header maps to a `request_id` in Redis with a 24-hour TTL.
`keys.idempotency` and `IDEMPOTENCY_TTL_S` have existed since Phase 3 and were, until this phase, the
only builders in Contract C that nothing called.

The header solves a real problem for a gateway specifically. A completion is the most expensive thing
this system does and the thing most likely to time out on a client's side while succeeding on ours —
free-tier latency variance is the whole reason the project exists. A retry after that timeout should
cost one completion, not two, and must not leave two assistant turns in one thread.

Three things in the codebase stood in the way, and each is a design question rather than an
implementation detail: the exact cache (D5/D19) already answers some repeat requests; the streaming
path returns a `StreamingResponse` long before the answer exists; and the endpoint has a dozen paths
that raise.

## Decision

**The value at `idem:{user_id}:{idem_key}` is a small JSON envelope, not a bare `request_id`.**
`IdempotencyEnvelope` carries `state` (`in_flight` | `done`), a `fingerprint` of the whole request,
the claiming `request_id`, `stream`, the stored `ChatCompletionResponse` body, and the original's
`cache_status`.

**This is not a Contract C amendment, and that was checked rather than assumed.** §2.3 froze the key
*format*; `idem:{user_id}:{idem_key}` is unchanged, there is no new builder and nothing to migrate.
The last two phases both genuinely *did* amend Contract C with sign-off (ADR-022's `{window}` segment,
ADR-036's `user_allocation` builder), so a reader has to be able to tell "amended again" from "did not
need to be". `app/cache/idempotency.py`'s module docstring says the same thing at the point of use.

**The claim is one `SET NX EX`, issued before the cache read, before quota, before routing** — and,
per trap 2, after `_validate_slot` and before `_resolve_conversation`. Four outcomes, one per row of
D47's table:

| Claim outcome | Behaviour |
|---|---|
| `NX` succeeded (`Claimed`) | This request owns the key. On success, overwrite with the `done` envelope; on failure, **delete it** |
| Existing, `done`, fingerprint matches (`Replay`) | Return the stored body with `X-Idempotent-Replay: true`. No provider call, no quota, no new message rows, one `requests` row at `status='replayed'` |
| Existing, `in_flight`, fingerprint matches (`InFlight`) | `409 idempotency_in_flight`, `Retry-After: 1` |
| Existing, fingerprint differs (`FingerprintMismatch`) | `409 idempotency_key_reuse` — Stripe's semantics |

**Every failure releases the claim.** `create_chat_completion` is now a thin wrapper holding the gate,
with the previous handler body moved to `_serve_completion`, so a single `except BaseException` covers
every path out of the turn. A streamed turn is completed or released by the **collector**, which is
already the component that assembles the full text after `done` (D5).

**Redis down means fail open.** Any error — an unreachable server, an envelope this version cannot
parse — logs once and returns `Claimed`, and the request is served exactly as it would have been
before this module existed.

## Why

**A `request_id` cannot reconstruct a body, and "the replay returns the stored response" is the
behaviour D6 actually asks for.** Storing the id and re-deriving the answer from the `requests` and
`messages` rows would mean rebuilding a wire response from a persistence schema on the retry path —
the one path that must not be able to fail in a new way — and would still not answer what a *streamed*
retry should receive.

**The order is the entire design.** Claim after the cache read and two concurrent identical retries at
`temperature: 0` are both served, which is fine; claim after routing and they both reach a provider,
which is precisely what D6 exists to prevent. Only `SET NX` issued first makes "one provider call" a
property rather than a hope, and the store's own test — eight simultaneous claims via `asyncio.gather`,
exactly one `Claimed` — tests the design rather than the code.

**Claim after `_validate_slot`, before `_resolve_conversation`** (trap 2). Earlier, and a typo'd slot
burns a key for 24 hours. Later, and a replay has already appended a duplicate user message and moved
`preferred_slot` before discovering it had nothing to do.

**A leaked `in_flight` envelope is the worst outcome this feature can produce** (trap 3). It locks the
key for a day, and the client's retry — the exact thing D6 exists to serve — gets a 409. Inlining the
release beside each `raise` in a two-hundred-line handler means the one that gets forgotten is a
latent day-long lock; the wrapper/inner split makes "exactly one of `complete`/`release`" structural.

**The fingerprint folds in `stream`, `conversation_id` and every message's `file_refs`.** A key is a
client's label, not a promise. A streaming retry replaying a non-streaming body would hand SSE a JSON
object; a fingerprint ignoring `conversation_id` would answer "add this turn to thread A" with thread
B's answer. Silently answering a different question under a reused key is worse than either answering
it or refusing, which is why the mismatch is a hard 409 rather than a fresh completion.

**Fail open, not closed** (trap 4). This is caching's rule and D20's, the opposite of quota's D15
rule, and for the same reason ADR-018 gives: nothing is being *spent* by proceeding. Refusing to
answer because a cache is down is a worse failure than answering twice. Getting it backwards turns a
Redis blip into a total outage of the one endpoint that matters.

**`X-Cache` and `X-Idempotent-Replay` are different facts** (trap 11). A replay recomputes no cache
key, so the only honest thing it can say about provenance is what the original said — which is why the
envelope carries `cache_status` at all. A replay of a turn that was originally a cache hit sets both
headers.

**`status='replayed'` is a new value in a deliberately unconstrained column** (trap 18). The column's
docstring has said since Phase 1 that it is unconstrained so phases can add values. Step 2 named the
constant one step *before* Step 6 wrote it and defined the error predicate as a complement
(`_NON_ERROR_STATUSES = (STATUS_OK, STATUS_REPLAYED)`), so `total` partitions exactly into
`ok + errors + replays`, a successful idempotent retry never inflates the error rate, and any status a
later phase adds is a failure by default until somebody classifies it. `provider`/`model` stay NULL,
which the repo docstring already defines as "never got that far" — literally true — and which keeps a
replay out of provider distribution and out of cost while keeping request volume honest. The client
really did make two requests.

## Consequences

- A stored envelope this version cannot revalidate (`ValidationError` on the body) neither replays nor
  409s: it logs `idempotency.unreplayable`, serves the request normally, and **owns nothing** — no
  `complete`, no `release` — because the key belongs to whoever wrote that envelope. The fail-open rule
  applied to schema drift rather than to an outage.
- `FINGERPRINT_VERSION` exists so that changing what the fingerprint folds in invalidates every stored
  envelope at once: an old fingerprint stops matching, so a retry reads as a mismatch (a 409) rather
  than as a replay of an answer computed under different rules.
- Two clients sharing one key across two users cannot collide: the key is per `user_id` by format, and
  a test asserts it.
- A request with **no** `Idempotency-Key` behaves exactly as it did before this phase — asserted by a
  regression test that spells the response out field by field, and by the whole pre-existing suite
  passing unchanged.
- The 24-hour TTL is set on the claim and again on completion, so a `done` envelope lives a full day
  from the moment the answer existed rather than from the moment the request started.
- `Idempotency-Key` is accepted on `POST /v1/chat/completions` and nowhere else: a `GET` is already
  idempotent, and `DELETE /v1/conversations/{id}` is idempotent by construction.
