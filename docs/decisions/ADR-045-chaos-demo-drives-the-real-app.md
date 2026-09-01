# ADR-045 — The chaos demo drives the real app in-process and changes nothing in it

**Status:** accepted · Phase 7, Step 10 · 2026-09-01
**Implements:** `phase7.md` §3 D50 (`development-plan.md` §3 Phase 7's "load and chaos demo script")
**Relates to:** [ADR-012](ADR-012-mid-stream-failover.md) (the behaviour the demo exists to show),
[ADR-015](ADR-015-attempt-cap.md) (why a breaker-skipped candidate costs no attempt, which is what
makes the substitution phase fit), [ADR-011](ADR-011-named-slot-spill.md) (the spill the demo makes
visible)

## Context

The headline claim of this project is that providers can die and the client does not see it. That
claim needs a demonstration, and "randomly kill providers" against a deployed service means one of two
things: real credentials revoked mid-run, or a chaos toggle endpoint in production code.

The first is not reproducible and burns real keys. The second is a permanent hole punched in the app —
an endpoint that can make the gateway lie about a provider's health, shipped forever, for the sake of
one recording.

## Decision

**`scripts/chaos_demo.py` builds the real application via `create_app()` and drives it over
`httpx.ASGITransport`.** The upstream side is an `httpx.MockTransport` serving the recorded fixtures
the test suite already commits, whose per-candidate behaviour the script flips as the run progresses.
It reads those fixtures as *files* rather than importing `tests`, under the same rule
`record_fixtures.py` already states.

**Nothing in `app/` changes for this step, and that constraint is the point** (trap 14): if the demo
needs a hook, the demo is wrong. `git diff --stat` on the step's commit shows no `app/` change.

It drives load through the real `gw_live_` API-key path (D7's programmatic half), supplies by hand
everything the lifespan would (the ASGI transport does not run it), inserts its own `users` row, and
deletes it afterwards unless `--keep-data` says to leave the rows for `/usage`.

**Four settings are overridden before `create_app()`, all pre-existing documented switches:**
`RATE_LIMIT_ENABLED=false` (D20 caps one user at 20 rpm and this demo *is* one user by construction),
`QUOTA_ENFORCEMENT=false` (`limits.yaml` describes real free-tier windows a load demo saturates in
seconds; `--quota` turns it back on), `ROUTING_LATENCY_RANKING=false` (D11's reordering makes two runs
of one seed disagree about who served what), and `FILES_STORAGE_BACKEND=memory`. Redis is `fakeredis`
by default, imported lazily so the script still runs where the dev extra is not installed.

**The summary is split into `plan` and `outcome`**, and only the first is claimed byte-reproducible.

## Why

**A chaos toggle in production code is a permanent liability for a temporary artifact.** Every future
reader has to establish that it is safe, every future auth change has to consider it, and the one
deployment where it is reachable is a gateway that can be made to report a healthy provider as dead.
Driving the ASGI app in-process gets the same demonstration with the blast radius of a script.

**The schedule is expressed in rounds and sized from the gateway's own constants, not in wall-clock
weights.** Phase 2 must land `FAILURE_THRESHOLD` failures on `general`'s first candidate to open its
breaker, and phases 2–4 must finish inside one `COOLDOWN_INITIAL_S` or the half-open probe firing
mid-phase-4 spends the attempt the spill needs. When a short `--duration` or a single client cannot
honour both, the script **drops** the substitution phase rather than running it into a failure it would
deserve — a demo that reports a failure it engineered by mis-scheduling itself is worse than a shorter
demo.

**Phase 4 is the only shape in this fleet that can produce `substituted: true` at all.** Every slot is
backed by the same three providers, so a per-provider outage is always absorbed by failover *inside*
the slot. That is why `Phase.kills` is keyed by either a provider or a single `provider/model`, and
why that phase rate-limits exactly `general`'s three candidates while `fast`'s stay healthy. It fits
inside D1's three attempts only because a breaker-skipped candidate is not an attempt (ADR-015) and
because a 429, unlike an `Unavailable`, is not retried on the same provider.

**Reproducibility is claimed precisely, not wholesale.** `plan` — seed, workload, phase schedule — is
byte-identical between two runs of one seed, and is verified. The headline outcome reproduces (360
requests, zero client-visible failures, all 200s). The attempt histogram and the substitution count
move by a request or two, because a breaker's cooldown expires against a wall clock and a request
arriving either side of that boundary routes differently. Saying so is the same disclosure discipline
the gateway applies to its own answers; claiming a deterministic attempt histogram would be the one
dishonest sentence in a document about honesty.

**A streamed `done` event with `status: "failed"` counts as a client-visible failure**, even though it
rode on a 200, and the script exits non-zero on any. That is what makes it usable as a check rather
than only as a demo — and it is the only definition of "client-visible" that matches what a user
actually experiences.

**The demo found a real bug on its first run, which is the argument for it existing.**
`config/providers.yaml` declared OpenRouter's third-choice candidate with `max_output_tokens` equal to
its whole 262144-token context window, leaving `fitting.input_budget` nothing for input; `render`
raises `ContextTooLong`, which Contract A makes **failover-ineligible**, so the whole turn 400s.
Nothing in the suite reached that candidate, because nothing routes that far unless both Groq and
Gemini are already down — exactly the situation the candidate exists for. The third-choice candidate
is the one nobody exercises and the one you need most.

## Consequences

- The config fix that run produced is the one file outside the step's own list that its commit
  touched. It is a config value, not application code, so the "no `app/` change" constraint holds.
- **An in-process mock is not a network.** No TLS, no DNS, no connection pool exhaustion, no real
  timeouts — the latencies in the transcript are not latencies. `docs/chaos-demo.md` carries a "what
  this does not demonstrate" section saying so, along with the fact that one worker proves nothing
  about two and that zero failures is a claim about *this* schedule rather than a proof that no
  schedule produces one.
- `--json` writes the run to a file, so `docs/chaos-demo.md` quotes real captured numbers rather than
  invented ones: seed 1, 360 requests, 81 streamed, five targets killed, zero client-visible failures,
  18 substitutions, seven breaker transitions including a full `open -> half_open -> closed`.
- Because the four overridden switches are all pre-existing and documented, the demo exercises the
  same code paths a production request takes; the table of what is switched off and why is in
  `docs/chaos-demo.md` so a reader can judge what the run does not cover.
- Running against a real Redis and Postgres instead of `fakeredis` is a flag away, which is what makes
  the script usable as a smoke test against a deployed database as well as as a recording.
