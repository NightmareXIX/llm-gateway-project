---
name: update-claude-md
description: Sync CLAUDE.md with the project's actual current state — phase progress, repo structure, conventions — without relitigating locked decisions or frozen contracts. Use when the user asks to "update CLAUDE.md", "sync CLAUDE.md", "mark phase X done", "bump the current phase", or after finishing a phase/step of work described in doc/reference/development-plan.md.
---

# Update CLAUDE.md

CLAUDE.md is the operating guide read at the start of every session in this repo. It drifts from
reality in one direction: work moves forward (phases complete, files land in new slots, decisions get
made) but the doc doesn't update itself. This skill closes that gap deliberately, in one pass, without
touching the parts of the doc that are explicitly off-limits.

## Check first: does this actually need an update?

Don't edit on reflex just because the skill was invoked. Before touching anything:

1. Read CLAUDE.md's current "Current phase" section and whatever other section the request points at.
2. Check the real state — `git log --oneline -20`, the repo tree, the relevant `doc/reference/phaseN.md`
   step's own "Done when" line.
3. If CLAUDE.md's existing text already matches that reality (the phase/step it names is still the
   right one, the file list is still current, nothing genuinely moved), **say so and stop** — don't
   make a cosmetic pass, don't reword something that wasn't wrong, don't re-run a full sync "just in
   case." A no-op report ("CLAUDE.md already reflects this — no change needed") is a valid, complete
   outcome of invoking this skill.
4. Only proceed past this point when you can name the specific drift: a phase/step actually completed
   that the doc doesn't reflect, a file that actually landed in a slot the tree doesn't list, a decision
   the user actually confirmed that isn't recorded yet.

## Ground rules before touching anything

- **§1 "Locked decisions" and §2 "The three frozen contracts" are frozen.** CLAUDE.md says so itself:
  "Any change to a signature, error class, invariant, or key format requires asking the user first."
  Never edit D1–D8 or contracts A/B/C as a side effect of a routine sync. If evidence in the repo
  contradicts one of them (e.g. a signature actually changed), stop and flag it to the user as a
  question, not a silent edit.
- **Don't relitigate.** If the code disagrees with a locked decision, the fix is either (a) the code is
  wrong, or (b) the user wants to unlock and change the decision — both are calls for the user, not for
  this skill to resolve unilaterally.
- **Where the contracts doc and the overview disagree, the contracts doc wins** — same rule CLAUDE.md
  states for itself; apply it when reconciling content pulled from `doc/reference/*.md`.
- **Stay terse.** Match the existing voice: dense, declarative, no filler. Don't expand sections into
  prose explanations. A one-line addition beats a new paragraph.

## What this skill updates

1. **§4 "Current phase."** The most common drift: commits move past a phase but the section header and
   scope description still describe an old one. Check `git log --oneline -20` and the repo tree against
   `doc/reference/development-plan.md` (and the per-phase docs — `contracts-and-phase1.md`, `phase2.md`,
   `phase3.md`, etc., check `doc/reference/` for the current set since new phase docs get added as the
   project progresses) to determine what phase the work is actually in. Rewrite §4 to name the current
   phase, its scope, its "Explicitly NOT in scope" list, and its "Done means" criterion, sourced from the
   matching phase doc — don't invent scope that isn't written there.
2. **§3 "Repo structure."** Diff the tree literally (`git ls-files` / directory listing) against the
   fenced tree block. Add genuinely new top-level modules/files to their existing category; do not
   reorganize categories or invent new ones. If a new file doesn't fit any existing slot, ask the user
   where it belongs rather than guessing — CLAUDE.md is explicit that new files have a designated slot.
3. **"Full specs" links at the top.** If `doc/reference/` has gained files not referenced in the intro
   paragraph (e.g. a new `phaseN.md`), add them to the link list.
4. **Conventions / Hard rules.** Only touch these if the user explicitly states a new convention or rule
   was adopted (e.g. a new testing convention, a new "never do X" learned from an incident). Don't infer
   these from code alone — they represent decisions, not just observed patterns.

## Exclude redundant information

CLAUDE.md is an operating guide read at the start of every session — not a second copy of the phase
docs, and not a changelog. Before writing new content into it:

- **Don't restate what a linked doc already says in full.** `doc/reference/phaseN.md` carries the
  complete step-by-step account, file lists, and reasoning; CLAUDE.md's phase blurb only needs enough
  for a session to orient itself (what's done, current status, the one or two facts that would surprise
  someone reading the frozen decisions or repo tree next to it) plus the link. If a fact is fully and
  durably recorded in the phase doc, point at it rather than re-deriving it into prose here.
- **Don't duplicate a fact CLAUDE.md already states elsewhere in itself.** Before adding something to a
  phase blurb, check whether it's already covered under "Locked decisions," "Frozen contracts," or an
  earlier phase's section — a decision only needs restating where it's genuinely load-bearing for the
  reader at that point in the doc, not every time it's touched again.
- **Don't carry forward routine gate results as prose** ("`make test` (N passed), `ruff check`, `ruff
  format --check`, and `mypy` are all green") unless a passing gate is itself the fact establishing the
  step is done and there's no shorter way to say so. A one-line "gates green" is enough when nothing
  about *which* gates or *what* they caught is actually informative to a future reader.
- **Prefer trimming an existing paragraph to appending a new one.** If updating a phase blurb, look for
  sentences that a newer fact has made redundant (superseded status, a since-fixed caveat) and cut them
  rather than layering the new fact on top and leaving the stale one in place.
- When in doubt about whether a specific detail is redundant enough to cut, keep it — this rule is
  about not padding the doc with restatement, not about stripping it down to the point of losing the
  "why," which is the one thing `git log` and the phase docs don't reliably carry on their own.

## What this skill never infers on its own

- Locked decisions (§1) and frozen contracts (§2) — confirm with the user first, always.
- Anything phrased as a rule ("never", "always", "must") — these are policy, not description; adding or
  changing one is a decision the user should explicitly make, even if the code currently behaves that way.

## Procedure

1. **Read current state.** Read `CLAUDE.md` in full. Run `git log --oneline -20` and `git status` to see
   recent work. List `doc/reference/` to see what phase docs exist now.
2. **Run the necessity check above first.** Name the specific drift before doing anything else. If
   there isn't one, report that and stop — the remaining steps assume real drift was found.
3. **Establish what actually changed.** If the user named a specific trigger ("mark phase 3 step 2
   done", "we added the perception lane"), scope the update to that. If they just said "update
   CLAUDE.md" with no specifics, infer the likely drift from recent commits (phase markers in commit
   subjects are a strong signal in this repo, e.g. "Phase 3 Steps 1-2: ...") and confirm your read of
   what changed with the user before rewriting sections — this is a checked-in governance doc, not a
   scratch file.
4. **Cross-check against `doc/reference/`.** Pull the actual scope/done-criteria language for the
   target phase from its source doc rather than paraphrasing from memory of the old CLAUDE.md content.
5. **Diff and confirm frozen-adjacent changes.** If the update touches anything near §1/§2 — even just
   nearby wording — call it out explicitly and get a yes before writing.
6. **Edit CLAUDE.md** using targeted edits (not a full rewrite) so the diff is reviewable. Apply the
   "Exclude redundant information" rules above while drafting the new text, not as an afterthought —
   and check the paragraph you're editing for anything the new fact has made stale enough to cut.
7. **Show the user what changed** (a summary of sections touched) and let them review — don't commit
   automatically. If they separately want it committed, that's a distinct ask (see the
   `commit-and-push` skill).

## Notes

- This skill is about keeping the doc honest, not about doing the underlying phase work. If asked to
  "update CLAUDE.md" but the phase work itself isn't actually done yet, say so instead of marking it
  done anyway.
- If `doc/reference/development-plan.md` itself is out of date relative to the phase docs, flag the
  discrepancy rather than silently picking one source over the other.
