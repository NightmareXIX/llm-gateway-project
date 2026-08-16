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

## Where this leaves Phase 3

The failover loop's candidate chain is exactly where a quota filter attaches next: `selection.py`
documents a single insertion point, run *before* D11's latency sort, that removes an exhausted
candidate from the chain entirely rather than merely losing the race to it. Nothing on either diagram
above needs to change shape for that — quota becomes one more reason a candidate is skipped, alongside
an open breaker, in the same box.
