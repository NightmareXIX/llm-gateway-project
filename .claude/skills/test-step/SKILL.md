---
name: test-step
description: Verify a just-implemented phase step against its own written spec — the "Touches" file list, "Tests," "Done when," and phase-level "Traps" that doc/reference/phaseN.md names for it — then run this repo's real gates (pytest, ruff, mypy, and the frontend suite when touched) and check CLAUDE.md's cross-cutting hard rules and frozen contracts against the diff. Use when the user asks to "test this step", "verify step X", "check if this step is done", "run the gates", or right after implementing a step from a phaseN.md plan.
---

# Test a step

This repo's phase docs (`doc/reference/phase2.md` … `phase7.md`) don't just describe scope — each
step names its own file list, its own tests, a literal "Done when" line (sometimes a `grep`/`git diff`
command to run verbatim), and the phase's numbered "Traps" section calls out the specific ways that
step tends to go subtly wrong. `make test` passing is necessary but not sufficient: the suite can be
green while a step touched a file it wasn't supposed to, skipped a test the doc explicitly asked for,
or reintroduced a trap the doc already named. This skill checks the step against *its own* spec, not
just against the generic test suite.

## 1. Locate the step's own spec

1. Identify which phase and step is under test — from the user's message, or from CLAUDE.md's
   "Current phase" section plus recent commits (`git log --oneline -5`).
2. Open the matching `doc/reference/phaseN.md` and find that step's subsection: its **Touches** file
   list, its numbered implementation notes, its **Tests** paragraph, and its **Done when** line.
3. Skim that phase's §5 **Traps** list (or equivalent) for entries relevant to this step — traps are
   phase-scoped, not step-scoped, so read all of them and keep the ones that apply.
4. If no phase step matches (a bug fix, a pre-Phase-1 change, exploratory work) skip to §7, the
   generic gate, and don't force-fit a phase-doc structure that isn't there.

## 2. Scope discipline

- `git diff --stat` (against the base the step started from) vs. the step's own **Touches** list.
  Anything touched that the list doesn't name is a finding — surface it, don't wave it through.
- Check any file the step or CLAUDE.md explicitly says must stay untouched — these are stated
  literally in the docs (e.g. "`git diff` shows no change inside `list_for_conversation`'s body",
  "`app/` is not touched — verify with `git diff --stat`"). Run the actual command named; don't
  approximate by eyeballing the diff.
- If "Done when" includes a literal shell check (a `grep -n "..." path` expected to return nothing,
  or only specific lines, or a round-trip like `make migrate` → `downgrade -1` → `upgrade head`), run
  that exact command and compare the real output to what the doc claims — don't take the doc's word
  for what it would show.

## 3. Confirm the named tests actually exist

The phase doc's **Tests** line for a step names exact test files and, often, exact scenarios (e.g.
"a fresh key claims; a second claim with the same fingerprint while in flight is `InFlight`..."). Read
the target test file(s) and check each named scenario has a corresponding test — not just that *a*
test exists in that file.

- A named scenario with no matching test is a real gap: the step isn't actually verified yet, even if
  everything currently in the suite passes. Report this as the blocker, not as a passing step.
- Don't silently write the missing tests — that's a scope decision the user should make explicitly,
  the same way this repo treats any non-trivial addition. Ask, or report the gap and stop there.

## 4. Run the gates

- **Backend, fast pass first**: run just the new/changed test file(s) directly
  (`pytest tests/unit/test_foo.py tests/integration/test_bar.py -v`) before the full suite, so a
  failure is cheap to iterate on.
- **Backend, full suite**: `make test`. This needs Postgres up (`docker compose up -d postgres`) —
  check first; a connection-refused error on every integration test is an environment problem, not a
  code regression, and should be reported as such rather than as failing tests.
- `make lint` (`ruff check` + `ruff format --check`) and `make typecheck` (`mypy`, strict).
- **Frontend**, only if `frontend/` was touched: `make frontend-test`, `make frontend-lint`, and
  `next build` — all three are named as gates in the phase docs' own "Done when" lines whenever a step
  touches the frontend (e.g. Phase 6 Step 10, Phase 7 Step 9).
- Report each gate's pass/fail individually. A green `make test` next to a red `mypy` is a different
  finding than both red, and collapsing them into "tests pass" hides which one to go fix.

## 5. Check CLAUDE.md's hard rules against the actual diff

These are stated as absolutes in CLAUDE.md and are cheap to check mechanically — do it against the
real diff, not from memory of whether the step "should" comply:

- **Never call a live provider API from tests.** Grep new/changed test files for real network usage;
  confirm any new provider interaction goes through `httpx.MockTransport` or reads a fixture under
  `tests/fixtures/provider_responses/`. If a genuinely new fixture is needed, that's a `make
  record-fixtures` job for the user to run with real keys — never fetch it yourself.
- **Never write an f-string Redis key outside `app/cache/keys.py`.** Grep the diff for
  `f"..."` patterns containing `:` outside that one file.
- **Never store a provider's request body.** Any new persistence path should only ever handle
  `CanonicalMessage`/`ContentBlock` shapes, never a raw provider payload.
- **Every conversation read is ownership-scoped in the SQL query itself.** New repo functions should
  filter `WHERE ... user_id = :uid` (or the matching join) in the query — not fetch-then-check in
  Python. A miss must be a 404 at the query level, not a 403 after the fact.
- **Phase 2+ seams stay typed `NotImplementedError` stubs until actually filled in.** If this step
  fills one in, confirm the signature it implements still matches what was frozen — a step is not the
  place to quietly widen a seam's contract.

## 6. Check the frozen contracts

If the diff touches `app/providers/` (Contract A), `app/memory/canonical.py` or `render.py`
(Contract B), or `app/cache/keys.py` (Contract C): confirm no signature, error class, invariant, or key
format changed without the explicit sign-off CLAUDE.md requires. If something did change, stop and
flag it as a question rather than deciding it's fine — this skill verifies, it doesn't authorize
contract changes. (A key format *amendment* with prior sign-off, like ADR-022's `{window}` segment or
Phase 6's `user_allocation` builder, is fine — check the module's own docstring for whether the change
in front of you already has that cover.)

## 7. Generic gate (no matching phase step)

For work that isn't tied to a phaseN.md step: run `make test`, `make lint`, `make typecheck` (plus the
three frontend gates if `frontend/` changed), then still walk §5 (hard rules) and §6 (frozen contracts)
against the diff — those apply to every change in this repo, phase step or not.

## 8. Report

Terse and structured, not narrated:

- Gates: pass/fail, one line each.
- Done-when criteria: met / not met, literally — quote the doc's own line next to the check.
- Traps checked: which ones apply here and whether the diff avoids each.
- Hard-rule / contract check: clean, or the specific violation with file:line.
- Missing tests named by the doc but not yet written: the actual blocker, called out as such — don't
  report "done" over a real gap just because the existing suite is green.

## Notes

- This is a verification pass, not an implementation pass. If something's red, report why; fix it only
  if the user asks, or if it's the kind of trivial lint/format nit already covered by standing
  instructions.
- This skill never marks a step done in `CLAUDE.md` — that's `update-claude-md`'s job, and it should
  only be invoked once this skill reports a genuinely clean result.
- If Postgres or Redis aren't running locally, say so plainly rather than letting connection failures
  read as code bugs in the report.
