# ADR-032 — Pinning without tool calls: a complete mechanism, one deferred trigger

**Status:** accepted · Phase 5, Step 8 · 2026-08-23
**Implements:** `phase5.md` §3 D32
**Relates to:** D3 (`contracts-and-phase1.md` §1 — the locked decision this ADR builds the write side
for); `app/memory/canonical.py::pin_target`, `app/db/repo/conversations.py::set_pinned`,
`app/routing/selection.py::pin_warning`; ADR-023 (the other place a frozen contract's shape had to be
worked around rather than reopened)

## Context

`development-plan.md`'s Phase 5 task list says, in effect: "if a message has tool-call content, set
`conversations.pinned_model`." But `RESERVED_BLOCK_TYPES` in `app/memory/canonical.py` rejects
`tool_call` and `tool_result` at `parse_block` — the database boundary — and `ChatCompletionRequest` is
`extra="forbid"` with no `tools` field. No history v1 can store, and no request v1 can accept, will ever
carry tool content. A trigger written against a condition that cannot occur, with the phase then marked
done, is the kind of thing that reads fine in a commit message and is discovered to be theatre the first
time someone tries to demo it.

Three ways to close this:

- **(a) Leave the whole mechanism as an unimplemented seam, documented.** Honest, but it leaves
  `project-overview.md` §8's "per-conversation pinning" unbuilt, and the `warning` field the overview
  specifies has nothing to emit it.
- **(b) Unfreeze `RESERVED_BLOCK_TYPES` enough to accept and store tool content.** This is Phase 5 quietly
  becoming the tool-call phase — exactly what D3 locks against, and what `phase5.md` §1's "Explicitly NOT
  in Phase 5" list rules out by name.
- **(c) Build the pin as a complete, reachable mechanism whose *trigger* is the one deferred part.**

## Decision

**(c).** Three pieces, in descending order of how load-bearing each is:

**`conversations_repo.set_pinned(session, *, conversation_id, user_id, model) -> bool`** — real,
ownership-scoped in the SQL exactly like `touch` and `rename`, returning `bool` the same way. Idempotent
for a re-pin to the same model. A re-pin to a *different* model is refused by the WHERE clause itself
(`pinned_model IS NULL OR pinned_model = :model`) rather than by raising — an UPDATE that relocated an
existing pin would defeat the reason the pin exists, so "zero rows matched" is the correct outcome on an
existing conversation, not a bug to route around with a second query.

**`canonical.pin_target(history: list[CanonicalMessage]) -> str | None`** — a pure predicate beside the
schema's other invariant helpers. Returns `"{provider}/{model}"` off the first message whose content
carries a `tool_call` or `tool_result` block (a `_TOOL_BLOCK_TYPES` subset of `RESERVED_BLOCK_TYPES` that
deliberately excludes `summary` — that type is reserved for the unrelated §2.2.7 seam, and folding it in
would fire the pin on the wrong feature landing first), else `None`. Runs against the *stored* history,
never the request body — D3 pins a conversation permanently from the first tool call anywhere in it, not
from what one particular turn asked (trap 4). This is real code over a real input domain: it is v1's
*storage* rule (`parse_block` rejecting the two block types) that keeps the non-`None` branch cold, not a
gap in the function. It is unit-tested directly against a hand-constructed `CanonicalMessage` — `content`
is a plain Python list, not the JSONB boundary `parse_block` guards, so a test can build the input no
database write could ever produce.

**The `warning` field is live today, not deferred.** `ChatCompletionResponse.warning: str | None` and
`DoneEvent.warning: str | None`, built by one shared helper (`selection.pin_warning`) read by both the
non-streaming and streaming paths. It fires whenever `conversation.pinned_model` is set and the requested
slot is not the one the pin resolves to — `auto` included, since a pin overriding `auto` removes a choice
just as much as a pin overriding a named slot does. `chat.py` calls `pin_target` after a successful turn
and, on a non-`None` result with `conversation.pinned_model is None`, calls `set_pinned` in the same
transaction as the assistant row — wired and reachable in code, even though nothing in v1 can currently
supply a non-`None` result.

## Why

**A complete mechanism with one deferred trigger is a documented limitation. A silently-passing stub is
not.** The distinction matters because of what each one costs a future reader. Option (a) leaves five
questions unanswered the moment tool calls do land in a later phase: what gets pinned, on what condition,
scoped to whom, refusing what kind of re-pin, disclosed how. Option (c) answers all five now, against a
predicate that happens to always return `None` today for a reason that is itself locked and documented
(D3). The day tool calls are built, the write path does not change at all — only `parse_block`'s rejection
of `tool_call`/`tool_result` has to move, and `pin_target`'s existing branch starts firing on real input it
was already tested against.

**Why not (b).** Unfreezing `RESERVED_BLOCK_TYPES` to make the trigger reachable would mean designing tool
schema, wire format, and storage shape as a side effect of finishing a *different* phase's write path —
exactly the scope creep `phase5.md` names as the failure mode to avoid ("No tool calls. Not the schema,
not the wire format, not an execution loop"). The pin is the mechanism that makes deferring tool calls
*safe*: it is the promise that if a provider's response ever did carry tool-call content, the conversation
would not silently keep round-robining across providers with incompatible tool schemas. Building that
promise does not require building the thing it promises to handle.

**Why the `warning` field could not wait for the trigger.** `warning` disclosure is exercisable today
through the read side alone — a conversation pinned by a direct row write (exactly how
`test_a_pinned_conversation_ignores_the_requested_slot` already establishes a pinned fixture) still needs
to disclose that pin on every subsequent turn. Tying `warning`'s existence to `pin_target` ever returning
non-`None` would have left the overview's disclosure promise unmet for the entire lifetime of a
manually-pinned conversation, which is the only kind that exists in v1.

**`set_pinned` refusing a re-pin outright, rather than moving it, follows from what a pin is *for*.** A
pin exists because a conversation's history cannot safely translate across providers once it contains
something provider-specific. An UPDATE that relocated the pin on request would reopen exactly the
translation problem the pin exists to close — so refusing is the correct behavior, not a missing feature.

## Consequences

- The only unreachable line in this feature is one branch of `pin_target` — reachable in a unit test,
  unreachable through the live API, and unreachable for a reason that is itself a locked, cross-referenced
  decision (D3) rather than an oversight.
- `RESERVED_BLOCK_TYPES`, `memory/summarize.py`, and `FitStrategy` are untouched by this ADR's decision,
  as `phase5.md` §1 requires — pinning's write path needed none of them to be complete.
- `preferred_slot` (D33) gets no ADR of its own; it is a bug fix with a code comment, not a decision with
  live alternatives, and this ADR's consequences section is where a reader looking for it is told so
  (`phase5.md` §4 Step 8 says the same).
- The day a later phase does unfreeze `RESERVED_BLOCK_TYPES` for real tool-call support, `pin_target` and
  `set_pinned` need no redesign — only `parse_block`'s rejection has to move, which is precisely the
  seam this decision was built to leave clean.
