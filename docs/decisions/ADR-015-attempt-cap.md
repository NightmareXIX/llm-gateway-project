# ADR-015 — One attempt budget of three, and what counts against it

**Status:** accepted · Phase 2, Step 4 · 2026-08-12
**Implements:** `phase2.md` §3 D12, and reconciles it with §4 Step 4's retry policy
**Relates to:** D1 (max 3 attempts per message), Contract C's `stream:{message_id}:attempts`,
[ADR-013](ADR-013-hand-rolled-breaker-and-retries.md) (the hand-rolled retries this bounds),
[ADR-011](ADR-011-named-slot-spill.md) (which makes the chain longer than the cap)

## Context

D1 caps a message at three attempts. Contract C reserves `stream:{message_id}:attempts` with a 300s
TTL. Three questions follow that neither settles: where the count lives, what an attempt *is*, and how
the cap interacts with the same-provider retries ADR-013 describes.

The third is not a detail. Read literally, ADR-013's "two jittered retries for `Unavailable`" and D12's
"an attempt is a request that left the process, capped at three" combine into a router that spends its
entire budget on one candidate — attempt, retry, retry — and never fails over at all. The failure mode
is precisely the one this phase exists to prevent, produced by two rules that are each individually
correct.

## Decision

**One budget of three upstream calls per message, and the retry yields.**

- **The in-process counter is authoritative.** A local variable, incremented immediately before each
  request leaves the process.
- **An attempt is a request that left the process.** A candidate skipped because its breaker was open
  cost no round trip and consumes nothing.
- **Same-provider retries share that budget** — they are requests that left the process — and are
  capped at **one** per candidate, not two.
- **A retry is skipped when it would starve the failover:** if only one attempt remains and an untried
  candidate exists, the budget goes to the candidate.
- **`stream:{message_id}:attempts` is written for observability and deliberately never read** (Step 9).

## Why

**The counter is local because the decision is local.** One request is served by one process. A
distributed counter cannot make a decision a local variable cannot make faster, and it introduces a
round trip on the hot path plus a failure mode — Redis unavailable — on a control-flow decision that
must always resolve. Everything Redis holds in this subsystem is advisory (ADR-010); the cap is not.

**A breaker skip is not an attempt, and that is why the chain is not truncated to the cap.** Truncating
the candidate list to three would mean three open breakers at the head of the chain exhaust the request
without a single call being made, while healthy providers sat at position four — an outage manufactured
entirely by our own bookkeeping. The list runs to its full length; the cap binds on attempts that
actually happened. The consequence to accept is that `requests.attempts` (the event trail, skips
included) can legitimately be longer than `meta.attempts` (the round trips). Two numbers, deliberately
different, and the trail is the one that explains the request.

**One retry rather than two, because the budgets are shared.** Given a cap of three, two retries make
the first candidate's failure fatal to the whole failover chain. The alternatives were to give retries
their own budget — nine upstream calls worst case, and `meta.attempts` stops meaning "round trips" — or
to accept the starvation. Both were rejected: the first quietly triples the worst-case latency of a
request that the cap exists to bound, and the second breaks the phase's headline feature in the exact
scenario it advertises. One budget, one retry, and the number three keeps meaning what a reader assumes
it means.

**The retry yields because a fresh candidate is the better bet.** When the budget is down to its last
attempt, a candidate that has not failed yet strictly dominates a second go at one that just did — the
first candidate has produced evidence, and the evidence is bad. When nothing remains to yield *to*, the
retry goes ahead, because yielding to nobody is just a shorter budget.

**Writing a key we do not read needs its reason attached.** `stream:{message_id}:attempts` is Contract
C's, it is cheap, and it makes an in-flight restart visible in Redis during an incident, which a local
variable is not. It is also the seam a future multi-instance retry-resume would read. Reading it for
control flow would mean a Redis round trip, on the hot path, to learn something the process already
knows — and would make the cap fail-open or fail-closed on a Redis blip, neither of which is
acceptable for a loop bound.

## Consequences

- The worst case for a message is three upstream calls plus one jittered backoff, whatever mix of
  candidates and retries produces it. That is the number to quote when someone asks how slow a fully
  degraded request can get.
- A flaky provider can still consume two of the three attempts (attempt plus retry) when it is first in
  the chain and there is budget to spare, leaving one for a fresh candidate. That is intended: one
  retry is cheap insurance against a blip, and the yield rule is what stops it from being expensive.
- `ContextTooLong`'s re-fit spends an attempt, because it is a request that left the process. It is
  capped at one re-fit per candidate for the same reason a retry is capped at one.
- ADR-013's "two jittered retries" is superseded by this ADR's one. That section of ADR-013 was written
  before the interaction with the cap was worked through, and has been amended to point here.
- The two numbers are now visible in two columns. Step 5 writes the event trail to `requests.attempts`
  and the round-trip count to `messages.meta.attempts`, and a turn with a breaker skip makes them
  disagree on purpose. Anyone reconciling them later should read this ADR rather than "fix" one.
- Step 9 inherits all of this unchanged. The streaming path counts the same way, so a restarted stream
  and a failed-over completion report the same `attempts` for the same amount of work.
