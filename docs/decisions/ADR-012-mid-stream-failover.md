# ADR-012 — Mid-stream failover: restart, not "never again"

**Status:** accepted · Phase 2, Step 9 · 2026-08-15
**Implements:** `contracts-and-phase1.md` §1 D1, `phase2.md` §3 D13
**Overrides:** `development-plan.md` §0 D1's original recommendation ("only fail over between
messages, never mid-stream")
**Relates to:** [ADR-015](ADR-015-attempt-cap.md) (the attempt budget this spends),
[ADR-016](ADR-016-streaming-session-lifetime.md) (what happens after `done`), Contract A's error
flags, §1.1's SSE wire protocol

## Context

The development plan's own D1 recommendation was the simplest defensible answer: once a stream has
sent a token, treat the model as committed, and if the provider dies mid-generation, surface a clean
error with whatever partial content exists. Restarting mid-stream — discarding tokens already shown to
a user and starting over on a different model — reads as a UI bug (a duplicated, contradictory answer)
if the client does the wrong thing with it, and it means a wasted request against a free tier that will
never be billed for by anyone but the gateway's own budget.

Contract A's frozen error hierarchy overrode that recommendation before this phase started. Every
normalized error carries `failover_eligible` as a class attribute, with no carve-out for "unless a
byte has already gone out" — `Unavailable` (an idle stall, a reset connection) is exactly as
failover-eligible mid-sentence as it is before the first token. Respecting the contract as written
means restarting, not stopping. This ADR is the reasoning the contract's authors did not have to write
down at the time, because Contract A predates the phase that has to live with the consequence.

A second problem arrives with the first: once a stream can fail after committing to a 200, the JSON
error envelope every other endpoint uses is unavailable — the status line already went out. Something
has to decide when the gateway is still allowed to answer "no" cleanly and when it has already promised
"yes" and has to make good on that promise in-band.

## Decision

**Restart on the wire, up to the same budget of three attempts everything else uses (ADR-015).** On a
mid-stream fault:

1. Abort the failed candidate's connection and stop reading from it.
2. Record what it actually produced as `wasted_tokens_out` — real tokens, really billed against a free
   tier, that nobody will ever read (`app/streaming/orchestrator.py::_Turn.abort`).
3. Open the breaker if the fault is breaker-eligible, exactly as the non-streaming path would.
4. If a candidate remains and the budget is not spent, emit `restart` naming the failed model, the next
   one, and `discarded_chars` — then **clear the buffer completely** and try again.
5. If the budget is spent or the chain is exhausted, emit `done` with `status: "failed"` and, when the
   longest discarded attempt cleared `MIN_PARTIAL_CONTENT_CHARS`, `partial_content` — an offer to keep
   what exists, never a silent substitute for a real answer.

**Never splice.** The client-side contract (`frontend/lib/sse.ts`, `PendingTurn`) is exactly as
strict as the server-side one: a `restart` event means *discard the bubble and swap the indicator*,
not "append and relabel." Two partial answers from two different models, welded together, reads as a
broken model rather than as a client bug, and is the harder failure to diagnose of the two — nobody
suspects the transport when the symptom looks like content.

**Four conditions never restart, and each is the identical reasoning behind a different corner of
Contract A:**

- **After `done`.** The terminal event makes the message final; nothing downstream is still listening
  for a correction.
- **`BadRequest` or `ContentFiltered`.** The first is our own bug and fails identically on every
  candidate; the second is a refusal, and restarting would shop the same prompt around the pool until
  something agrees to answer it — laundering, not resilience, the same failure mode
  `EmptyResponse`-vs-`ContentFiltered` misclassification exists to prevent on the non-streaming path.
- **`ContextTooLong`.** Re-fits and retries once, but only *before* the first delta — mid-generation
  the prompt was already accepted, so there is nothing left to re-fit and the error means something
  else entirely.
- **A client disconnect.** Not a fault at all. Detected explicitly (`is_disconnected`), it aborts
  upstream, records the tokens, and stops — persisting nothing further and logging the unremarkable
  event it is, rather than letting a `CancelledError` traceback make a closed laptop look like an
  incident.

**D13: not one byte until an attempt has produced one.** The router loop
(`routing.route_stream`) runs to its first delta before the orchestrator yields anything at all. A
failure in that window — every candidate breaker-open, every candidate `BadRequest`, a fully-down
provider pool — never touches the wire, so `_stream_chat_completion` can still translate it through
`to_app_error` into the same JSON envelope with a `request_id` that every other endpoint returns. Only
a fault *after* the first delta has to go in-band, as `done` with `status: "failed"`.

Mechanically this costs nothing: Starlette sends the ASGI status line the instant its body generator
first yields, not before, so the router simply has to run before the first `yield` — which is what it
already does to know whether it has anything to say. `_stream_chat_completion` primes the generator by
hand (`await frames.__anext__()`) outside the `StreamingResponse` constructor for exactly this reason;
constructing it eagerly would hand Starlette control before the router has proven it has anything to
send.

**A dedicated, tighter first-token budget.** D13's boundary introduces its own risk: with nothing sent
yet, a client waiting on a candidate that has accepted the connection and gone quiet has no headers, no
status, and no way to tell the gateway apart from a dead socket — and some clients and proxies impose
their own header-response timeouts. `DEFAULT_FIRST_TOKEN_TIMEOUT_S = 10.0` answers this, distinct from
`DEFAULT_IDLE_TIMEOUT_S = 30.0` once a candidate has proven it works. A provider that has produced
nothing at all is not warming up; it is not answering, and two others are standing by.

## Why

**The alternative to restarting is not "no failure," it is "the same failure, disclosed worse."** A
provider dying thirty tokens into an answer is not made less likely by refusing to fail over — it is
made *equally* likely and the gateway's only remaining choice is whether the client gets a working
answer from a different model or a half sentence and an apology. The three sources this project exists
to unify are free tiers; a dead connection mid-generation is not an edge case on them, it is Tuesday.

**Restarting is honest in a way silence is not.** `served_by` and `substituted` already exist on the
non-streaming response to disclose "this model, not the one you asked for." A stream that restarts and
says nothing about it would be the one place in the system where a provider swap happens invisibly —
exactly what D1/D2's disclosure requirement forbids everywhere else. The `restart` event is that
disclosure mechanism extended onto the wire, not a new idea.

**The four never-restart conditions are not exceptions bolted onto the state machine — they are Contract
A's flags, read literally, one more time.** `BadRequest` is not `failover_eligible`; `ContentFiltered`
is not `failover_eligible`; `ContextTooLong` is not `failover_eligible` but carries its own one-shot
repair. None of that logic lives in the orchestrator — `app/streaming/orchestrator.py`'s own docstring
says so explicitly: whether to restart is `route_stream`'s decision, made on the flags, and the
orchestrator would be duplicating it if it inspected an error class at all. The orchestrator's job is
narrower and easier to get right: given that the router has already decided "try again," clear the
buffer and say so.

**D13's boundary is worth the one generator-priming trick it costs**, because the failure mode it
avoids — a 200 whose body eventually says `"failed"` — is the one shape of error a monitoring dashboard,
an SDK's status-code branch, and a human skimming logs all handle worst. A `RoutingFailed` that never
got to try anything is exactly as debuggable as any other 502, with a `request_id` a user can quote.

## Alternatives considered

**Stop entirely, per the original D1 recommendation.** Rejected because Contract A had already made the
call: an error hierarchy whose flags say "try elsewhere" cannot then be told "except once a byte has
gone out" without either changing the frozen contract or quietly ignoring half of what it says. The
originally-cited cost — "incoherent output and duplicated tokens" — is a client-side risk, not a
server-side one, and is exactly what the clear-the-buffer rule and the `restart` event exist to
prevent instead of accepting.

**Splice the partial answers together across a restart.** Rejected outright, and named as Trap 7 in
`phase2.md` for a reason: a reader cannot distinguish "the model produced this" from "two different
models each produced half of this," and the failure reads as a quality problem with the model rather
than a transport problem with the gateway — the worse of the two failures to have someone debug.

**Commit to the 200 immediately, and let a pre-first-byte failure go in-band too.** Rejected because it
throws away debuggability for no benefit: nothing has been shown to the client yet, so there is nothing
D13's boundary costs by staying out-of-band, and a 200 that occasionally means "actually, no" is a worse
API than one that means what its status code says.

## Consequences

- Three candidates that each stall on the first-token budget cost at most ~30s before the client sees
  anything (three × 10s), not the ~90s three read-timeout waits would cost — the worst case D13's own
  budget is sized to bound.
- `meta` is emitted immediately before the first delta rather than at request start. This costs nothing
  real: the client has nothing to render until there is a token to show it, so moving `meta` later loses
  no information the client could have acted on sooner.
- A restart's cost is honestly counted, not hidden: `wasted_tokens_out` accumulates across every
  discarded attempt, and Phase 3's quota tracker inherits a correct number instead of a systematically
  low one in the exact scenario — restarts — where undercounting would first show up as a key rate-limited
  earlier than predicted.
- The state machine's own invariants (never restart after `done`, never on `BadRequest` /
  `ContentFiltered`, client disconnect aborts without persisting) are each a named test in
  `tests/unit/test_orchestrator.py` rather than a property inferred from reading the code, because a
  restart bug is exactly the kind of thing that is invisible until a chaos demo finds it live.
- `_stream_chat_completion`'s generator-priming pattern is now the one place in the codebase that
  constructs a `StreamingResponse` after already consuming its first value — worth remembering before
  "simplifying" it back to the natural-looking `StreamingResponse(frames, ...)`, which would silently
  reopen the exact gap D13 exists to close.
