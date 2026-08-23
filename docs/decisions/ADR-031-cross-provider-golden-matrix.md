# ADR-031 — The cross-provider golden matrix renders, it does not build a payload

**Status:** accepted · Phase 5, Step 8 · 2026-08-23
**Implements:** `phase5.md` §3 D31
**Relates to:** `tests/unit/test_gemini_payload.py`, `test_groq_payload.py`, `test_openrouter_payload.py`
(the per-adapter goldens this suite reuses rather than replaces); ADR-025 (why a `file_ref`'s shape is
decided at render time, not upload time — the same reasoning that makes `build_payload` alone
insufficient here)

## Context

`contracts-and-phase1.md` §2.2.6 promises one property: a single fixed canonical history — a system
message, a `file_ref`, five turns — produces the correct payload for all three providers. Before this
step, three `*_general` goldens and one `gemini_attachment` golden existed, and every one of them was
asserted against `adapter.build_payload` directly. That is the right test for what those files protect:
`build_payload` is pure (Contract A), so a golden on it is a golden on a function with no dependencies.
It cannot express §2.2.6's actual claim, for one concrete reason — **a `file_ref` has no payload shape
until something decides whether it renders native or injected, and that decision is render step 1, not
`build_payload`.** A suite that only ever calls `build_payload` with a hand-picked `mode` has decided the
tier question itself, off-camera, before the function under test ever runs.

## Decision

**The matrix drives `app.memory.render.render`, with a scripted resolver, and asserts against the same
committed goldens `build_payload` already golds.**

`tests/provider_fixtures.py` gained `ScriptedResolver`, a test double implementing
`render.AttachmentResolver` that answers exactly one question — native with bytes when
`spec.supports_mime(mime)` and the size is within `spec.max_file_bytes`, otherwise injected with a fixed,
committed extraction text — and nothing else. No database, no Redis, no `PerceptionResolver` import.

`tests/contract/test_cross_provider_matrix.py` renders `canonical_history()` and
`canonical_history_with_attachment()` through all three adapters and asserts six goldens:
`{provider}_general` (all three pre-existing, reused byte-for-byte) and `{provider}_attachment`
(`gemini_attachment` pre-existing and unchanged, `groq_attachment`/`openrouter_attachment` new). Three
further assertions, the actual point of the suite: the system message lands in its provider-correct
position, structurally rather than only via the golden diff; the omission marker survives into every
provider's rendered text identically; and the extracted-document envelope is byte-identical across the
two injected providers.

The per-adapter `build_payload` golden suites are untouched and stay untouched — they cover purity,
clamping, role mapping, and refusal shapes that this matrix does not re-litigate.

## Why

**Reusing the three `*_general` goldens byte-for-byte, rather than copying them into a new fixture, is
the test's whole value.** If `render()` ever produced one different byte from a direct `build_payload`
call on a history that needs no fitting, that would be a real bug in the pipeline's transparency between
render and the adapter — and the only way to catch it is to point two different call paths at the same
committed file and let them disagree. A matrix with its own private copies of the general goldens could
never surface that disagreement; it would just have two sets of goldens that happen to agree because
nobody wired them together.

**`tests/contract/`, not `tests/unit/`.** This asserts an agreement between three implementations of one
protocol (`ProviderAdapter`) against one shared input, which is exactly what `test_adapter_conformance.py`
already holds in that directory. A per-adapter payload test is a unit test of one function; this is a
contract test of three adapters agreeing on what one canonical history means.

**The scripted resolver, not `PerceptionResolver`.** `PerceptionResolver`'s own tier logic — cache, native,
llm, local, and the failure/degradation rules around each — is already covered end to end by
`tests/integration/test_perception_lane.py`, which is where that coverage belongs. A golden test that
needed Postgres, Redis, and object storage running to produce a diff is a golden nobody runs on a red
build; it would defeat the purpose of a fast, deterministic contract suite. `ScriptedResolver` answers
only tier 1's question — supported MIME and size, or not — because that is the only fact `build_payload`
itself needs to have already been decided; everything upstream of that decision is Phase 4's concern, not
this suite's.

**The per-adapter suites stay, unfolded into this one.** They are not superseded: `build_payload` purity
means they can assert things this suite cannot cheaply reach — clamping edge cases, malformed-input
rejection, role-mapping details that would need a full canonical history to exercise through `render()`
for no added confidence. Folding them in would trade a fast, targeted suite for a slow, indirect one that
tests the same claims worse.

## Consequences

- A disagreement between `render()` and a committed `build_payload` golden is a stop-and-report event,
  never a re-bless (trap 2). Fixing it means understanding which of the two was wrong, not making the
  test pass again.
- The bless entrypoint (`python -m tests.contract.test_cross_provider_matrix --bless`) rewrites all six
  files from one render pass, using `provider_fixtures.write_golden` — the one place any bless script
  writes a file, forcing `\n` line endings so `.gitattributes`' byte-exactness contract survives a Windows
  checkout (the CRLF finding this step's own predecessor, Step 1, fixed).
- `ScriptedResolver` growing a session parameter, or any dependency beyond a fixed byte string and a
  `ModelSpec`, is the signal it has become the thing it was written to avoid (trap 9) — a reason to stop
  and reconsider, not to extend it further.
