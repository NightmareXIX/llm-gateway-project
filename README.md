# LLM Gateway

A FastAPI service that sits between clients and Gemini/Groq/OpenRouter, exposing one
OpenAI-shaped API while it owns conversation state, routes across logical model slots, fails over
when a free tier runs out, tracks heterogeneous quota (RPM/RPD/TPM) in Redis, understands
uploaded files through a separate "perception lane" even when the answering model can't, and lets a
user bring their own provider key — resolved per candidate, so one failover chain can spend a private
key and a shared one and count each correctly. Built entirely on free tiers, as a portfolio/learning
project.

Full design docs live in [`doc/reference/`](doc/reference/) — start with
[`project-overview.md`](doc/reference/project-overview.md) for the pitch and
[`contracts-and-phase1.md`](doc/reference/contracts-and-phase1.md) for the frozen contracts
everything else is built against. Decision records are in [`docs/decisions/`](docs/decisions/);
the honest-edges document is [`docs/limitations.md`](docs/limitations.md).

*(This README covers four interview questions in depth for now — the architecture diagrams, full
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

## Why does the same history come out looking different for every provider?

The gateway stores one shape of conversation — a canonical schema (Contract B) that is deliberately
provider-agnostic — and never a provider's own request body. Every payload any provider ever receives is
built fresh, per attempt, from that one stored history, through a single six-step pipeline
(`app/memory/render.py`). That is what makes switching providers mid-conversation, whether by a user
picking a different slot or by D1/D2's own failover firing mid-request, a non-event for the data: the
history a Gemini attempt sees and the history a Groq attempt sees three messages later are the same rows,
rendered twice.

The interesting part is what "rendered" has to mean once the shapes genuinely disagree. Gemini lifts the
system message out of the message list into a top-level `system_instruction` field; Groq and OpenRouter,
both OpenAI-shaped, leave it as `messages[0]`. A `file_ref` has no shape at all until render decides
whether the candidate about to be tried can read the bytes natively or needs them injected as text — the
same stored block becomes Gemini's `inline_data` for one attempt and a `<document>`-wrapped extraction in
the next provider's prompt text a moment later, with no second upload and no second extraction call.
Context-window overflow gets the same treatment: the oldest messages are dropped and a plain-text
omission marker takes their place, because none of the three wire formats has a field for "some of this
was cut" — the marker is prose because prose is the one representation every provider actually reads.

A single test asserts the claim directly rather than trusting each adapter's own unit tests to add up to
it: `tests/contract/test_cross_provider_matrix.py` renders one fixed history through `render()` against
all three adapters, with and without an attachment, and pins six golden payloads plus three structural
properties — where the system message lands, that the omission marker survives into all three payload
texts identically, and that the extracted-document envelope is byte-for-byte identical between the two
providers it gets injected for. The reasoning for testing at the render boundary rather than at each
adapter's `build_payload` is in
[ADR-031](docs/decisions/ADR-031-cross-provider-golden-matrix.md); the "one history, three shapes"
diagram is in [`docs/architecture.md`](docs/architecture.md).

The demo this proves: start a conversation on `fast` (Groq), ask something, get an answer. Switch to
`general` and ask what you said first — the answer quotes it, and `served_by` names a different provider
than the one that answered turn one. Attach a PDF on the first turn and ask about it on the second — the
same uploaded bytes render natively for one model and as extracted text for the other, with one upload
and one extraction between them.

## What changes when you paste in your own API key?

The next message. Not the next session, not the next login — the next message, in the same
conversation, with no reload.

Open Settings → API Keys and every provider reads *Using shared pool*. Paste a Gemini key and the
gateway validates it against Gemini **before** storing anything: a bad key comes back with Google's
own wording and nothing is written, and a Gemini that happens to be *down* comes back as a distinct
"we couldn't check this" rather than as "your key is bad" — two sentences that lead to opposite next
actions, which is the entire reason there are two error codes. A good key is encrypted with Fernet,
stored, and the row flips to *Using your key · ••••a91c*. Send the next message and its provenance
says `key_pool: private`, the `requests` row carries `quota_scope = <your user id>`, and in Redis
`q:{you}:gemini:gemini-3.6-flash:rpd` has moved while `q:system:…` has not. Remove the key and the
message after that is back on the shared pool.

The part that is actually hard is that **BYOK is per provider, not per user**. One failover chain
crosses both pools: your Gemini key pays for candidate 1, and when it is spent, the gateway's shared
Groq key pays for candidate 2 — one request, two credentials, two sets of counters, neither leaking
into the other. So "which key" and "which budget" are one question answered *per candidate*, by one
injected object, and the old per-request `scope` parameter was deleted rather than kept alongside it
([ADR-034](docs/decisions/ADR-034-per-candidate-credential-resolution.md)). A private key that gets
rejected is never quietly retried on the shared key for the same provider: the chain moves to the
next *provider*, and the broken key's row is flagged so Settings can say so, because silently
laundering a dead key through the shared pool means the user is told everything is fine forever
([ADR-037](docs/decisions/ADR-037-private-key-failure-is-not-laundered.md)).

Two consequences worth naming rather than hiding. Your own key can unlock a slot nobody else sees —
`pro`, on a Gemini Pro model the shared free-tier key genuinely cannot reach — and `auto` still never
routes to it, because an `auto` that resolves differently per account is unreproducible and its cache
entries unshareable ([ADR-038](docs/decisions/ADR-038-private-key-only-slots.md)). And the exact
cache is keyed on the request, not on the user, so an answer your key paid for can be replayed to
someone else asking the identical question; that trade is written down in
[`docs/limitations.md`](docs/limitations.md) rather than discovered.

## Running it

```
make dev        # docker-compose: app + Postgres + Redis
make test        # pytest, no live provider calls — everything is recorded fixtures
make migrate      # alembic upgrade head
make chaos-demo    # kill providers under load and watch nobody notice
```

See [`.env.example`](.env.example) for required configuration and
[`docs/deploy.md`](docs/deploy.md) for the deployed-instance runbook.
[`docs/chaos-demo.md`](docs/chaos-demo.md) explains the chaos run — 360 requests, five
candidates killed and revived on a schedule, zero client-visible failures — and, just as
importantly, what an in-process mock does not prove.
