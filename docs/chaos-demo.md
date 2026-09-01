# The chaos demo

Phase 7, Step 10. One command that kills providers under load and shows that nobody on the
client side notices:

```
docker compose up -d postgres
make chaos-demo
```

Ninety seconds, four concurrent clients, 360 requests, five candidates killed and revived on a
schedule, and one number that is the whole point: **client-visible failures: 0**.

---

## What it actually runs

**The real application.** `scripts/chaos_demo.py` builds the app with `create_app()` — the same
factory `uvicorn` imports — and drives it over `httpx.ASGITransport`. The router, the circuit
breaker, the exact cache, the canonical-history render pipeline, the persistence, the disclosure
fields: all of it is the shipped code, not a rehearsal of it.

**A scripted far side of the wire.** The only thing faked is the providers. An
`httpx.MockTransport` answers every outbound call from
`tests/fixtures/provider_responses/` — the same recorded responses the test suite replays — and
the script flips each candidate's behaviour as the run progresses. A healthy candidate gets
`success.json`; a rate-limited one gets `rate_limited.json` (a real 429 with a real
`retry-after`); a dead one gets `server_error_html.json`, which is a 502 whose body is a load
balancer's HTML error page rather than tidy JSON, because that is what a provider outage looks
like from the outside.

**Nothing in `app/` changes for this** (D50). That constraint is the design, not a chore: a chaos
toggle that a deployed service can reach is a permanent hole punched in the gateway for the sake of
one recording. If the demo ever needs a hook, the demo is wrong.

**Its own account.** The script inserts a `users` row and a `gw_live_` API key and drives the load
through the real API-key auth path — D7's programmatic half. Every row it writes is deleted
afterwards (`ON DELETE CASCADE` takes the conversations, messages and `requests` rows with the
user), unless you pass `--keep-data`, which is what you want if the next thing you open is
`/usage`.

**Redis is `fakeredis` by default**, so a run starts from a clean fleet: breaker state that
survived the previous run would make the first minute of this one a replay of the last one's
outages. `--redis-url` points it at a real server.

---

## The schedule

Five phases, and the sizes of the middle three are not aesthetic — they are derived from two
constants in the gateway's own code: D1's cap of three attempts per request
(`routing/router.py::MAX_ATTEMPTS`) and the breaker's thirty-second first cooldown
(`routing/circuit_breaker.py::COOLDOWN_INITIAL_S`).

| # | Phase | What is broken | What the gateway does |
|---|---|---|---|
| 1 | steady state | nothing | candidate 0 of each slot answers |
| 2 | `groq/gpt-oss-120b` rate-limited | `general`'s first candidate, 429 | a 429 is not retryable on the same provider, so every `general`/`auto` turn fails over to Gemini on attempt two. Five failures open that candidate's breaker |
| 3 | groq + openrouter down | two providers, every model, 502 | `general` skips the open breaker and lands on Gemini in one attempt. `fast` burns its one same-provider retry (an `Unavailable` *is* retryable, unlike a 429) and still succeeds on attempt three |
| 4 | `general`'s whole chain rate-limited | all three of `general`'s candidates, 429; `fast`'s three healthy | the **substitution** phase: candidate 0 is skipped for free (phase 2 opened its breaker), the other two spend one attempt each, and the third attempt spills into `fast`'s chain. A different slot answers a named request, and `substituted: true` says so (D2) |
| 5 | recovery | nothing | the cooldowns expire, the half-open probes succeed, the breakers close |

Phase 4 is the only shape in this fleet that can produce a substitution at all, and it is worth
saying why: every slot is backed by the same three providers, so killing a *provider* is always
absorbed by failover **inside** the slot. Emptying one slot's candidate list while another's
survives needs per-candidate kills — which is why `Phase.kills` is keyed by either a provider or a
single `provider/model`.

It also only fits inside three attempts because phase 2 opened a breaker first. **A skipped
candidate is not an attempt**; a failed one is. That is the entire arithmetic of the phase, and
when the run is too short to honour it — a small `--duration`, or a single client, so phase 2 never
lands five failures, or phases 2–4 no longer fit inside one cooldown — the script **drops phase 4**
rather than running it into a failure it would deserve. A short run demonstrates failover; the
cascade needs the defaults.

---

## A captured run

```
$ python -m scripts.chaos_demo --seed 1 --no-live

round   90/90   phase: recovery - breakers re-probe and close
requests   360   in flight  0   client-visible failures 0   substitutions 18   p50 130ms

  candidate                                         upstream  breaker    served
  groq/openai/gpt-oss-120b                          ok        closed     157
  groq/openai/gpt-oss-20b                           ok        closed     72
  gemini/gemini-3.6-flash                           ok        open       46
  gemini/gemini-3.5-flash-lite                      ok        closed     85
  openrouter/nvidia/nemotron-3-super-120b-a12b:free ok        open       0
  openrouter/openai/gpt-oss-20b:free                ok        closed     0

==============================================================================
  chaos demo - summary
==============================================================================
  seed 1   90 rounds x 4 clients @ 1.0s   (90.0s wall clock)

  requests sent ................. 360 (81 streamed)
  targets killed ................ 5 (gemini/gemini-3.6-flash, groq, groq/openai/gpt-oss-120b, openrouter, openrouter/nvidia/nemotron-3-super-120b-a12b:free)
  CLIENT-VISIBLE FAILURES ...... 0
  substitutions disclosed ...... 18
  multi-attempt turns .......... 11
  stream restarts .............. 0
  cache hits ................... 0
  statuses ..................... {'200': 360}

  served by candidate:
    gemini/gemini-3.5-flash-lite                  85
    gemini/gemini-3.6-flash                       46
    groq/openai/gpt-oss-120b                      157
    groq/openai/gpt-oss-20b                       72

  breaker transitions:
    t+  9.28s  groq/openai/gpt-oss-120b  closed -> open
    t+ 22.43s  groq/openai/gpt-oss-20b  closed -> open
    t+ 25.31s  gemini/gemini-3.6-flash  closed -> open
    t+ 25.31s  openrouter/nvidia/nemotron-3-super-120b-a12b:free  closed -> open
    t+ 40.08s  groq/openai/gpt-oss-120b  open -> half_open
    t+ 40.33s  groq/openai/gpt-oss-120b  half_open -> closed
    t+ 55.31s  groq/openai/gpt-oss-20b  open -> closed

  latency (in-process, not a network): p50 130.2ms  p95 174.3ms  max 405.0ms
==============================================================================
```

Five things in that output are worth reading slowly.

**`statuses: {'200': 360}` with five candidates killed.** Every request was answered. Not "mostly
answered" and not "answered after the client retried" — the failover happened inside the gateway,
inside the request, and the client saw one 200 with a full body.

**The full state machine is visible in the transition log.** `gpt-oss-120b` goes
`closed -> open` at t+9.3 (five 429s), sits out its thirty-second cooldown, and comes back
`open -> half_open -> closed` at t+40.1 — one probe request claimed the half-open slot, succeeded,
and closed the breaker for everyone.

**Two candidates end the run with an open breaker and that is correct.** `gemini-3.6-flash` and
`nemotron` opened at t+25.3 during phase 4 and never re-closed, because a breaker only closes when
a half-open probe *succeeds*, and a probe only happens when a real request reaches that candidate.
Once `gpt-oss-120b` closed at t+40.3 it answered every `general` turn again, so nothing routed as
far as the other two for the rest of the run. An open breaker with nothing to probe it is the
system correctly declining to spend a request on satisfying a dashboard.

**`groq/gpt-oss-120b` served 157 of 360 requests despite being the first candidate killed.** That
is what "failover" means as opposed to "failing away from": it was skipped while it was known-bad
and picked straight back up on the far side of its own cooldown.

**`stream restarts: 0`, and that is honest rather than convenient.** D1's mid-stream restart only
fires when a stream dies *after* the first byte. This script's scripted upstream fails a stream at
the response status, before the first frame — which the router handles as an ordinary pre-first-byte
failover. A genuine mid-stream fault needs a body that starts and then breaks, which
`tests/unit/test_orchestrator.py` scripts directly; that path is tested, not demonstrated here.

---

## Reproducibility, exactly

`--seed` fixes the workload: which slot each request names, which ones stream, in what order.
The phase schedule is expressed in **rounds**, not wall-clock seconds, so the same seed sends the
same requests into the same fleet states on a busy machine as on an idle one.

The summary is split into two blocks that say which is which:

- **`plan`** — seed, rounds, concurrency, interval, request count, slot mix, the phase schedule,
  the list of killed targets. Two runs of one seed produce a **byte-identical** `plan`:

  ```
  python -m scripts.chaos_demo --seed 1 --json a.json
  python -m scripts.chaos_demo --seed 1 --json b.json
  python -c "import json;a,b=[json.load(open(f))['plan'] for f in ('a.json','b.json')];print(a==b)"
  # True
  ```

- **`outcome`** — everything measured. The headline reproduces: 360 requests, zero client-visible
  failures, all 200s. The per-candidate counts, the attempt histogram and the substitution count
  move by a request or two between runs, because a breaker's cooldown expires against a **wall
  clock** and a request arriving either side of that boundary is routed differently.

Claiming the whole summary was reproducible would be false, and this project's whole argument is
that a disclosed approximation beats a flattering one.

The script exits non-zero when `client_visible_failures` is not zero, so it works as a check and
not only as a demo.

---

## What is turned off, and why

Four settings are overridden before the app is built. Each already exists, each is already
documented, and none of them is new for this script:

| Setting | Value | Why |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `false` | D20's limiter caps one user at 20 requests a minute. This demo *is* one user by construction, so leaving it on would measure the limiter rather than failover |
| `QUOTA_ENFORCEMENT` | `false` | `config/limits.yaml` describes real free-tier budgets measured in tens of requests per minute; a load demo saturates them in seconds and the outage story drowns in quota skips. `--quota` turns it back on — try it, and watch the failure count climb for a reason that has nothing to do with the providers dying |
| `ROUTING_LATENCY_RANKING` | `false` | D11 reorders candidates once it has samples, and a reordering fleet makes two runs of one seed disagree about who served what |
| `FILES_STORAGE_BACKEND` | `memory` | no request here carries a file, and the default backend wants a Supabase service key to construct |

Every request also sends `temperature: 0.7` and a unique prompt. That is deliberate: D19 would
answer a repeated deterministic prompt out of the exact cache, and a demo whose provider calls
quietly stopped happening would report a very impressive zero failures.

---

## What this does not demonstrate

**An in-process mock is not a network.** There is no TCP, no TLS, no DNS, no packet loss, no
half-open socket, no proxy in between. The failure modes this exercises are the ones a provider
*reports* — a status code and a body — not the ones a network *inflicts*. Connection resets and
idle stalls are covered in the unit suite with a scripted byte stream; they are not covered here.

**The latencies are not latencies.** `p50 134ms` is the cost of this process talking to itself,
including a real Postgres round trip and a fake Redis, and it is dominated by whatever else the
machine is doing. It is in the summary so a regression is visible between two runs on one box,
and for nothing else. A real free-tier completion takes hundreds of times longer, and the ordering
that emerges under real latency is exactly what D11's ranking exists for — which this run turns
off.

**One process, one worker.** Render runs two. The breaker and the quota counters are shared
through Redis and therefore correct across workers, but nothing here proves that, because there is
only one. The `/metrics` counters, which are deliberately per-process (D49), are not exercised at
all.

**Zero failures is a claim about this schedule.** It is not a proof that no schedule produces one.
The schedule is built to keep every request inside D1's three-attempt budget — that is the honest
framing: *this* pattern of outage, the one free-tier providers actually inflict, is survivable, and
the demo shows the machinery surviving it. Kill all three providers at once and the gateway returns
a 503 with an error envelope, which is the correct answer and not a demo.

**It found a real bug the first time it ran.** `config/providers.yaml` declared OpenRouter's
`nvidia/nemotron-3-super-120b-a12b:free` with a `max_output_tokens` equal to its whole context
window, which leaves `fitting.input_budget` nothing for input; `render` raises `ContextTooLong`,
which is deliberately *not* failover-eligible, so the whole turn 400s. Nothing in the suite reached
that candidate, because nothing routes that far unless both Groq and Gemini are already down —
exactly the situation the candidate exists for. That is the argument for this script existing,
better than any paragraph about chaos engineering: the third-choice candidate is the one nobody
exercises, and the one you need most.
