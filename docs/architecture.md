# Architecture

Two diagrams, both added at the close of Phase 2 because both only became true once Milestone B
landed: the failover loop that serves both the non-streaming and the streaming path
(`app/routing/router.py`), and the restart state machine layered on top of it for streaming turns
(`app/streaming/orchestrator.py`). Read alongside `doc/reference/contracts-and-phase1.md` §1 (D1/D2)
and `doc/reference/phase2.md` §3, which this page draws diagrams for rather than repeats.

---

## The failover loop

One entry point serves both `route` (non-streaming) and `route_stream` (streaming) — the alternative is
two copies of the attempt bookkeeping that drift, which `phase2.md` §4 Step 4 calls out explicitly. The
loop walks a candidate chain built by `selection.candidates()` (D10's named-slot spill, D11's
latency-ranked `auto` expansion) and makes exactly one branching decision per failure: *what kind* of
error was this, read off Contract A's class flags — never `isinstance`, never `adapter.name`.

```mermaid
flowchart TD
    Start(["Request arrives\nrequested slot / auto"]) --> Chain["selection.candidates()\nD10 spill + D11 latency rank"]
    Chain --> Next{"Candidates\nremaining?"}
    Next -- no --> Exhausted["Raise RoutingFailed\n(last error, full trail)"]
    Next -- yes --> Cap{"Attempts < 3?\n(ADR-015)"}
    Cap -- no --> Exhausted
    Cap -- yes --> Breaker{"Breaker open\nfor this candidate?"}
    Breaker -- "yes: skip, free" --> Trail1["trail += skipped_breaker\n(no attempt spent)"]
    Trail1 --> Next
    Breaker -- no --> Render["render() -> build_payload()\n(only path to a payload)"]
    Render --> Attempt["Attempt leaves the process\n(+1 to the budget)"]
    Attempt -- success --> Success(["Return / yield the answer\nrecord breaker success\nrecord latency (successes only)"])
    Attempt -- failure --> Flags{"Error class flags\n(Contract A)"}

    Flags -- "BadRequest\nContentFiltered" --> Abort["Abort the whole request\n(not this candidate's fault --\nnothing behind it would differ)"]
    Flags -- "ContextTooLong,\nno delta shown yet" --> Refit["Re-fit to limit_tokens\nretry once, same candidate"]
    Refit --> Attempt
    Flags -- "retryable_same_provider\n(Unavailable / EmptyResponse)" --> RetryGate{"Budget allows a retry\nwithout starving\nan untried candidate?"}
    RetryGate -- yes --> Jitter["Jittered backoff\n1 retry per candidate"]
    Jitter --> Attempt
    RetryGate -- no --> FailoverEligible
    Flags -- "failover_eligible\n(RateLimited / Unavailable\nafter retries)" --> FailoverEligible["Record breaker failure\nopen breaker if breaker_eligible"]
    FailoverEligible --> Next

    Abort --> AbortRaise(["Raise RoutingFailed\n(failover_eligible=False)"])

    classDef terminal fill:#2f6f4f,color:#fff,stroke:none;
    classDef fail fill:#8a3a3a,color:#fff,stroke:none;
    class Success terminal;
    class Exhausted,AbortRaise fail;
```

**What is not on this diagram, deliberately:** the streaming path's first-token budget and mid-stream
restart. Those live one layer up — this loop only knows "the candidate I just tried failed before or
after producing a delta," and the *streaming-specific* consequence of a post-delta failure (never
retry, always restart) is the orchestrator's business, described next. `route_stream` reuses every box
above unchanged; it adds one extra rule not shown here — once a delta has been delivered, the same
candidate is never retried, because a restart onto a *different* provider is D1's mechanism for that
case, not this loop's own retry.

---

## The restart state machine (D1, streaming only)

`app/streaming/orchestrator.py::stream_completion` drives this on top of `route_stream`'s events
(`AttemptStarted`, `AttemptDelta`, `AttemptAborted`, `StreamCompleted`). The loop above decides *whether*
to try the next candidate; this state machine decides what the *client* is told and when the response
is allowed to start at all (D13).

```mermaid
stateDiagram-v2
    [*] --> WaitingForFirstAttempt: request received,\nnothing sent yet

    WaitingForFirstAttempt --> WaitingForFirstAttempt: candidate skipped\n(breaker open) or\nretried same-provider
    WaitingForFirstAttempt --> PreCommitFailure: every candidate failed\nfailover-ineligibly, or\nchain exhausted\n(no byte sent -- D13)
    PreCommitFailure --> [*]: raise RoutingFailed\nJSON error envelope\n+ request_id (not a 200)

    WaitingForFirstAttempt --> Streaming: first delta arrives\n(within DEFAULT_FIRST_TOKEN_TIMEOUT_S)\nemit meta, then delta\n*response now committed to 200*

    Streaming --> Streaming: more deltas\n(within DEFAULT_IDLE_TIMEOUT_S)\nheartbeat comment on a gap\nwhile still waiting

    Streaming --> Done: StreamCompleted\nemit done{status:"ok"}\nhand StreamResult to Collector

    Streaming --> Aborted: mid-stream fault\n(idle stall, reset, refusal,\nBadRequest/ContentFiltered\nnever reach here as a restart)
    Aborted --> RestartCheck: record wasted_tokens_out\nrecord breaker failure\n(open breaker if eligible)\nclear the buffer completely

    RestartCheck --> Streaming: attempt < 3\nand a candidate remains\nemit restart{discarded_chars}\n*never spliced with old buffer*
    RestartCheck --> Failed: attempts exhausted or\nerror not failover-eligible

    Streaming --> Disconnected: client left\n(not a fault)
    Disconnected --> [*]: abort upstream, record tokens\npersist nothing further\nno restart, no done

    Failed --> [*]: emit done{status:"failed",\npartial_content if >= 40 chars}\nhand StreamResult to Collector

    Done --> [*]
```

**The rule the diagram cannot make self-evident, so it is stated here too:** `RestartCheck` never
re-enters `Streaming` on the *same* candidate. A candidate that delivered at least one delta and then
died has demonstrated it accepts a connection and then fails — a second attempt against it would spend
a budget slot exactly like an untried candidate, for worse odds. A restart always advances to a new
candidate; the same-provider retry in the failover loop above is reserved for failures that happened
*before* any content was shown.

---

## After `done` (D14)

Neither diagram shows persistence, on purpose — by the time either reaches a terminal state, the
response body is finished, and what happens next is a separate concern with its own lifetime rule
(ADR-016): the orchestrator holds no database session at any point above, and
`app/streaming/collector.py::Collector` opens one short-lived session *after* `Done` or `Failed`, writes
one row (or none, for a pre-commit failure — that one is the request-scoped session's job, one layer up
in `api/v1/chat.py`), commits, and closes. A persistence failure here is logged and swallowed, never
raised — there is no response left to turn it into.

---

## Phase 3: quota joins the loop

The insertion point the section above predicted is exactly where it landed: `render` → `quota.reserve`
→ `attempts += 1` → attempt, inside the same box the breaker check already occupied. Quota becomes a
second reason a candidate is skipped for free — `skipped_quota` alongside `skipped_breaker` — and
neither costs the three-attempt budget (ADR-015, [ADR-020](decisions/ADR-020-quota-reservation-placement.md)).

```mermaid
flowchart TD
    Render["render() -> build_payload()\n(estimated_tokens from RenderReport)"] --> Reserve{"quota.reserve(spec, scope,\nestimated_tokens)"}
    Reserve -- "Redis unreachable\n(D15, fail closed)" --> SkipQuota["trail += skipped_quota\ndegraded=true\n(no attempt spent)"]
    Reserve -- "window exhausted" --> SkipQuota2["trail += skipped_quota\nblocked_window, retry_after_s\n(no attempt spent)"]
    Reserve -- allowed --> Spend["attempts += 1\n(the only place this counter moves)"]
    SkipQuota --> NextCandidate(["next candidate"])
    SkipQuota2 --> NextCandidate

    Spend --> Call["adapter.complete() / adapter.stream()"]
    Call -- "never reached the provider\n(render/reserve-adjacent failure)" --> Release["quota.release(reservation)\ngives back every window,\nrequest windows included"]
    Call -- "reached the provider,\nsucceeded or failed" --> Commit["quota.commit(reservation,\ntokens_in=actual, tokens_out=actual)\nrequest windows NEVER refunded (trap 6)"]

    Call --> Hint{"take_hint() -- QuotaHint\npublished by _request (D18)"}
    Hint -- present --> ApplyHint["quota.apply_hint()\nmoves a counter UP only,\nnever grants (never authorizes)"]
    Hint -- none --> Done(["attempt outcome returned\nto the failover loop above"])
    ApplyHint --> Done
    Release --> Done
    Commit --> Done

    classDef skip fill:#8a3a3a,color:#fff,stroke:none;
    classDef terminal fill:#2f6f4f,color:#fff,stroke:none;
    class SkipQuota,SkipQuota2 skip;
    class Done terminal;
```

**Reserve is a filter that spends its own answer**, not a read followed by a write — the whole reason
Contract C mandates one Lua script rather than a pipelined check-then-increment (trap 2,
[ADR-020](decisions/ADR-020-quota-reservation-placement.md)). `reserve.lua` checks every declared
window before incrementing any of them, so a chain that blocks on window three never leaves windows
one and two overstated (trap 3).

**A reservation that expires mid-flight commits nothing rather than guessing.** `RESERVATION_TTL_S` is
120s; a stream that outlives it finds `commit.lua`'s reservation hash already gone and no-ops, logging
`quota.reservation_expired` — over-counting the original estimate rather than risking a blind delta
that double-counts a stream a `release` already refunded on another path.

**`apply_hint` runs after every attempt, success or failure**, draining whatever
`HttpProviderAdapter._request`/`_stream_events` published to the `QuotaHint` contextvar
([ADR-021](decisions/ADR-021-quotahint-transport.md)) — Groq and OpenRouter's own rate-limit headers
correcting the gateway's estimate toward ground truth, one direction only.

## Phase 4: two lanes, one system

Nothing on either diagram above needed to change shape for the perception lane —
`quota/lanes.py::reserve_perception` was the typed seam Phase 4 filled in, spending the half of
Gemini's budget D8's `reserved_fraction` had already fenced off from the answer lane the whole time.
What Phase 4 adds is a second lane feeding render step 1, and its own decision inside the box the
diagram above already labelled `render() -> build_payload()`.

```mermaid
flowchart LR
    Upload(["POST /v1/files\nbytes + metadata only\n(ADR-025 — no extraction here)"]) --> Store["ObjectStore.put()\nprivate bucket, content-addressed\n{hash[:2]}/{hash} (ADR-026)"]
    Store --> FilesRow[("files row\nuser_id, file_hash\nownership lives here")]

    Turn(["Chat turn with file_refs"]) --> Gate{"Ownership check\nWHERE file_hash = ANY(:hashes)\nAND user_id = :uid"}
    Gate -- "unowned hash" --> NotFound(["404 file_not_found\n(never 403 — D24)"])
    Gate -- owned --> Stored[("message row\ntext block, then file_ref block")]

    Stored -.->|"every render, every candidate\n(D22 — resolved here, not at upload)"| Lane

    subgraph Lane["PerceptionResolver.resolve() — ADR-028's chain"]
        direction TB
        T0{"Tier 0: cache\nextract:{hash} or\nfile_extractions row?"}
        T0 -- "llm row found" --> Injected0(["injected, tier=cache\nno provider call"])
        T0 -- "no row / local row only" --> T1{"Tier 1: native\nsupports_mime and\nsize <= max_file_bytes?"}
        T1 -- yes --> Native(["native, tier=native\nno perception reservation (trap 7)\ncost via token_cost (ADR-029)"])
        T1 -- no --> T2{"Tier 2: llm\nperception slot has\nbudget + closed breaker?"}
        T2 -- yes --> Injected2(["injected, tier=llm\nreserve_perception (ADR-027)\npersist: Postgres, then Redis"])
        T2 -- "no / chain exhausted" --> T0f{"stored local row\nheld back above?"}
        T0f -- yes --> Injected0b(["injected, tier=cache\nreplays the local fallback"])
        T0f -- no --> T3{"Tier 3: local\nPyMuPDF text layer,\nor rasterize + Tesseract"}
        T3 -- "reading produced" --> Injected3(["injected, tier=local\ndegraded=true, persisted\n(unless PERCEPTION_LOCAL_ONLY)"])
        T3 -- "nothing recovered" --> Unreadable(["raise FileUnreadable\n422, no message written\n(only failure that surfaces)"])
    end

    Lane --> Payload["adapter.build_payload()\ninline_data (Gemini) or\ndocument_envelope text (others)"]
    Payload --> Answer(["answer lane\n(the failover loop, above)"])

    classDef terminal fill:#2f6f4f,color:#fff,stroke:none;
    classDef fail fill:#8a3a3a,color:#fff,stroke:none;
    class Injected0,Injected0b,Native,Injected2,Injected3 terminal;
    class NotFound,Unreadable fail;
```

**Every tier but the last is wrapped in `_guarded`**, not shown as a separate box because it applies
uniformly: an exception from any of tiers 0–2 is logged with the file hash and the tier, and the
chain falls through exactly as if that tier had declined on its own terms. Only `FileUnreadable` from
tier 3 is allowed to end the turn (ADR-028).

**The memo, also not drawn, is what makes this diagram honest under failover.** `PerceptionResolver`
is one instance per request, keyed on `(file_hash, native_wanted)` — so a turn that spills from Groq
to Gemini under D1 runs this chain once per distinct file, not once per attempt (ADR-025, trap 6). A
diagram of the chain alone would suggest it runs fresh every time render does; in practice the second
and third renders of one turn mostly hit the in-memory memo before this flowchart's first box.

**Bytes are fetched at most once per chain, and only when a tier needs them.** Tier 0 needs none —
the common case, a second turn about an already-read document, never calls `ObjectStore.get` at all.
A chain that falls from tier 1 through to tier 3 still only downloads once, held by `_Lazy` across the
tiers that share the need.
