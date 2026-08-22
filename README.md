# LLM Gateway

A FastAPI service that sits between clients and Gemini/Groq/OpenRouter, exposing one
OpenAI-shaped API while it owns conversation state, routes across logical model slots, fails over
when a free tier runs out, tracks heterogeneous quota (RPM/RPD/TPM) in Redis, and understands
uploaded files through a separate "perception lane" even when the answering model can't. Built
entirely on free tiers, as a portfolio/learning project.

Full design docs live in [`doc/reference/`](doc/reference/) — start with
[`project-overview.md`](doc/reference/project-overview.md) for the pitch and
[`contracts-and-phase1.md`](doc/reference/contracts-and-phase1.md) for the frozen contracts
everything else is built against. Decision records are in [`docs/decisions/`](docs/decisions/);
the honest-edges document is [`docs/limitations.md`](docs/limitations.md).

*(This README covers two interview questions in depth for now — the architecture diagrams, full
request-flow walkthrough, and "Design Decisions" index it will eventually carry are Phase 7 work,
tracked in [`doc/reference/development-plan.md`](doc/reference/development-plan.md).)*

---

## Why is "which model answers" a different decision from "which model can see the file"?

Not every free-tier model reads a PDF or an image, and the ones that do meter it separately from
plain chat — so a gateway that only ever attaches a file to whichever model happens to be answering
loses file understanding the moment that model is a text-only one, or the moment its multimodal quota
runs dry. The perception lane exists to decouple the two questions entirely: *which model generates
this response* and *which model, if any, is used to understand an attached file* are handled by two
independent fallback chains, and a text-only model answering a question about a PDF is a normal case,
not a degraded one.

The chain (`app/perception/lane.py`) walks four tiers in order, and the same "always degrade, never
just fail" rule the rest of the gateway follows governs it: a cached reading beats a fresh one for
free; a native passthrough hands the bytes straight to a model that can read them; a dedicated
extraction call (Gemini, paid for out of a daily budget fenced off from plain chat, D8) reads the
document and writes a structured summary for a model that cannot; and if every provider option is
spent, local PyMuPDF/Tesseract still produces a worse-but-real answer rather than an error. Only the
last tier's failure is ever allowed to reach the user — every tier above it logs and falls through,
because a bug in one fallback should cost quality, not the whole request.

The interesting design decision was not "add file upload" — it was working out *when* the reading
happens. Extracting at upload time is the intuitive answer and the wrong one: the gateway does not
know which model will eventually answer, and a document extracted at upload has its extraction
frozen into that moment forever. Extraction instead resolves at render time, from a cache keyed on
the file's content hash — so a better prompt or a bigger extraction model retroactively improves
every stored conversation that ever referenced those bytes, the next time any of them is read. The
reasoning, and what it costs (the first turn about a document pays for its own extraction, in front
of the answer), is in
[ADR-025](docs/decisions/ADR-025-extraction-at-render-not-upload.md) and the rest of the
perception-lane ADRs (026–030) it sits alongside.

## Why a Lua script, and not a pipeline?

Every provider quota the gateway tracks — RPM, RPD, TPM, sometimes TPD — has to be checked and
spent atomically, or the check is worthless. The naive version reads a counter, sees room, and
increments it as two separate Redis round trips. Under any real concurrency that is a race: fifty
simultaneous requests can all `GET` a counter sitting at 9 against a limit of 10, all see room,
and all `INCRBY` — and the overshoot is invisible until the provider free-tier key gets
rate-limited earlier than predicted, or worse, banned for sustained over-limit traffic.

A Redis pipeline does not fix this. A pipeline batches commands into one round trip, but Redis
still executes each command in the batch as its own atomic step — nothing stops another client's
`GET` from landing between this pipeline's own `GET` and `INCRBY`. What actually closes the race is
a Lua script: Redis runs a script to completion, atomically, before serving another client's
command, so "check every window, then spend every window" becomes one indivisible operation no
concurrent caller can interleave with.

That is the whole reason `app/quota/scripts/reserve.lua` exists, and why it does the check in one
pass over every declared window before incrementing any of them in a second pass — a script that
incremented as it went and then bailed partway through would leave the earlier windows permanently
overstated, with no record of what to give back. The reserve → commit/release lifecycle built on
top of it (`app/quota/tracker.py`) is diagrammed in [`docs/architecture.md`](docs/architecture.md),
and the design reasoning is in
[ADR-020](docs/decisions/ADR-020-quota-reservation-placement.md).

A unit test exercises the claim directly rather than taking it on faith: fifty concurrent
`reserve()` calls against a limit of ten grant exactly ten (`tests/unit/test_quota_tracker.py`).
That test is the actual point of the Lua script, and it is the test that would fail first if the
atomicity were ever accidentally lost to a refactor.

## Running it

```
make dev        # docker-compose: app + Postgres + Redis
make test        # pytest, no live provider calls — everything is recorded fixtures
make migrate      # alembic upgrade head
```

See [`.env.example`](.env.example) for required configuration and
[`docs/deploy.md`](docs/deploy.md) for the deployed-instance runbook.
