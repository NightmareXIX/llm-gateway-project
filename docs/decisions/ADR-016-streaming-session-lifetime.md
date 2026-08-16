# ADR-016 — A streamed turn holds no database session while it generates

**Status:** accepted · Phase 2, Step 9 (orchestrator) and Step 10 (collector) · 2026-08-15
**Implements:** `phase2.md` §3 D14
**Relates to:** [ADR-012](ADR-012-mid-stream-failover.md) (the state machine this session sits
underneath), `app/deps.py::get_session`, `app/streaming/{orchestrator,collector}.py`

## Context

`deps.get_session` yields a request-scoped `AsyncSession` that FastAPI tears down around the response
lifecycle — opened before the handler runs, closed (or rolled back) after it returns. A
`StreamingResponse` body generator does not fit that lifecycle: Starlette starts sending bytes the
moment the generator first yields and keeps it alive, potentially for tens of seconds across up to
three provider attempts, well past the point a normal request-response handler would have returned and
released its dependency.

Two ways of getting this wrong are both live options, not hypotheticals:

- **Hold the request-scoped session for the whole generation.** A connection pinned open per
  concurrent streaming chat is a capacity problem before it is a correctness one — a free-tier Postgres
  pool (`pool_size 5 + max_overflow 5` per worker, per `fly.toml`'s own comment) runs out of connections
  under a handful of concurrent streams, and the failure looks like unrelated requests timing out rather
  than like the streaming endpoint that is actually responsible.
- **Let FastAPI tear the session down mid-generation anyway**, because the `yield`-dependency's teardown
  runs on the handler returning, and an async generator handed to `StreamingResponse` is *not* the
  handler returning — it is a value the handler returned. Using that session after teardown is a closed
  session, silently or loudly depending on when the reuse happens.

`api/v1/chat.py`'s own docstring already states the project's standing rule — never hold a transaction
across a provider call that can legitimately take sixty seconds — and D14 is that rule applied to the
one place in the codebase where holding one for the *whole request* looks, at first glance, like the
natural way to persist a streamed answer.

## Decision

**No session is held for the duration of a stream's generation. Three sessions cover the lifetime of
one streamed turn, and none of them span the provider call:**

1. **Before streaming begins**, the request-scoped session (Phase 1's `get_session`) commits the user's
   inbound message, exactly as the non-streaming path does. This session is *also* what
   `_stream_chat_completion` uses for a pre-first-byte failure (D13/ADR-012) — the same
   `usage_logger.record_failure` call the non-streaming path uses, because at that point nothing
   streaming-shaped has happened yet.
2. **During generation**, `app/streaming/orchestrator.py::stream_completion` holds nothing at all. It
   takes a `StreamPersistence` protocol, not a session — `Collector` satisfies it, but the orchestrator
   itself never imports `AsyncSession`, `messages_repo`, or anything database-shaped. This is what
   keeps the orchestrator testable by capturing a `StreamResult` and asserting on it, without standing
   up a database.
3. **After `done`**, `app/streaming/collector.py::Collector.persist` opens its own short-lived session
   from `app.state.db_session_factory` — a plain callable, not a session — writes the assistant message
   (success) or the `requests` row (failure), commits once, and closes. This is the *only* code in the
   codebase that touches the database after the stream has started.

**The factory, not a session, is what gets threaded through.** `Collector.__init__` takes
`SessionFactory` (`Callable[[], AbstractAsyncContextManager[AsyncSession]]`) and the caller's
`Principal`, and is constructed once per request in `_stream_chat_completion`, closed over both. This
is also what makes `Collector` unit-testable against a throwaway engine without a second one just to
satisfy the type checker.

**A persistence failure after `done` must not raise into the response.** By the time `Collector.persist`
runs, the client already has their answer — there is no response left standing to turn a raised
exception into a 500. `_persist` (`orchestrator.py`) wraps the call, logs at `error` level with
`message_id`, `conversation_id`, and `attempts`, and swallows. The honest cost: the message will not
survive a page refresh. That is worse than persisting and strictly better than a traceback attached to
a request that visibly succeeded — and it is visible in the logs rather than silent, which a swallowed
exception without a log line would not be.

## Why

**A session pinned across a provider call is a capacity bug that only appears under load**, which is
the worst kind to ship: it passes every functional test, including a load test run at low concurrency,
and shows up as a production incident the first time a demo or a real user runs four streaming chats at
once. Sizing the pool up is not a fix — it treats the symptom of a design that assumes streaming
requests are as short as ordinary ones, and free-tier Postgres does not offer the headroom to paper over
that assumption.

**Splitting the session by phase is what makes each phase's failure mode independently reasonable.** A
crash mid-generation, with no session open, leaves nothing to roll back — the inbound message is
already committed, and the collector never got a chance to run, so the conversation is in exactly the
state a client that got no `done` event would expect: the user's turn exists, the assistant's does not
yet. Contrast that with a single session held end-to-end, where a mid-stream crash leaves an ambiguous,
implicit rollback decision entangled with a live SSE connection.

**The collector taking a factory rather than a session is a testability decision as much as a
correctness one.** A protocol-typed `StreamPersistence` lets `test_orchestrator.py` assert on a captured
`StreamResult` with no database at all, and a factory-typed `Collector` lets `test_collector.py` assert
on what actually got written with a throwaway engine and no request. Neither test file needs the other
module's machinery, which is the payoff of the seam being a protocol and a callable rather than a
concrete session threaded through two layers.

**Swallowing the post-`done` failure is the same reasoning ADR-012 already applies to a client
disconnect: the honest answer to "what do we do about an event we cannot act on" is to log it plainly
and not manufacture an error the caller cannot receive.** The alternative — raising and letting it
surface as a 500 for a request whose body already finished streaming a success — is not more correct,
it is a traceback for an audience of nobody, since the response is over.

## Alternatives considered

**A provisional `message_id`, corrected once persistence actually happens.** Rejected — `meta` promises
`message_id` to the client with the first token (ADR-012), and a client that started rendering against
one id only to have it silently change later is a client-side bug generator, not a simplification. Step
9 mints the id up front instead, and `messages_repo.append` takes an optional `message_id` kwarg so the
row lands with the id already promised — a repo signature change, not a contract change.

**Keep the request-scoped session alive by extending its dependency lifetime past the handler's
return.** Rejected as fighting the framework rather than working with it: FastAPI's `yield`-dependency
teardown is tied to the handler returning, and a `StreamingResponse` is a value the handler *returns* —
reaching past that boundary means either a private FastAPI implementation detail or a session lifetime
nothing else in the codebase would recognize.

**Let a post-`done` persistence failure raise, on the theory that silent data loss is worse than a
traceback.** Rejected on where the exception would land: by construction it happens after the SSE body
generator has already yielded its terminal `done` frame, so an exception here has no response to become
— it becomes an unhandled error in a body generator that has already told the client it succeeded. Log
level exists precisely for the case where a failure is real but there is nothing left to do about it in
the request.

## Consequences

- A free-tier connection pool now has to size for concurrent *non-streaming* work plus the brief moment
  each streamed turn spends persisting after `done` — not for the full duration of every concurrent
  stream. This is the entire capacity argument the ADR exists to make, made numerically true.
- A page refreshed mid-stream, before `done`, shows the conversation without the in-progress assistant
  turn — there was never a row to show. This is consistent with the client's own state (no `done` event
  received) and needs no special-casing on the frontend.
- A persistence failure after `done` is a silent-to-the-user, loud-to-the-logs event:
  `stream.persist_failed` at error level, carrying enough to find the request (`message_id`,
  `conversation_id`, `attempts`) but not the message content — the content is gone with the process
  memory that held it, which is the honest cost this ADR accepts rather than hides.
- `Collector` and `stream_completion` can each be tested without the other's harness —
  `tests/unit/test_orchestrator.py` never imports SQLAlchemy, and `tests/integration/test_collector.py`
  never imports the router or the SSE framing. A future contributor extending either one only needs the
  seam's protocol, not both implementations.
- The pattern generalizes: any future long-running response body (a batch export, a multi-step tool
  call) should reach for the same shape — request-scoped session before the long operation, no session
  during it, a fresh short-lived one after — rather than rediscovering the capacity bug this ADR
  already paid to find.
