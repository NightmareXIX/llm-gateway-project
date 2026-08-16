---
name: update-claude-md
description: Sync CLAUDE.md with the project's actual current state — phase progress, repo structure, conventions — without relitigating locked decisions or frozen contracts. Use when the user asks to "update CLAUDE.md", "sync CLAUDE.md", "mark phase X done", "bump the current phase", or after finishing a phase/step of work described in doc/reference/development-plan.md.
---

# Update CLAUDE.md

CLAUDE.md is the operating guide read at the start of every session in this repo. It drifts from
reality in one direction: work moves forward (phases complete, files land in new slots, decisions get
made) but the doc doesn't update itself. This skill closes that gap deliberately, in one pass, without
touching the parts of the doc that are explicitly off-limits.

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

## What this skill never infers on its own

- Locked decisions (§1) and frozen contracts (§2) — confirm with the user first, always.
- Anything phrased as a rule ("never", "always", "must") — these are policy, not description; adding or
  changing one is a decision the user should explicitly make, even if the code currently behaves that way.

## Procedure

1. **Read current state.** Read `CLAUDE.md` in full. Run `git log --oneline -20` and `git status` to see
   recent work. List `doc/reference/` to see what phase docs exist now.
2. **Establish what actually changed.** If the user named a specific trigger ("mark phase 3 step 2
   done", "we added the perception lane"), scope the update to that. If they just said "update
   CLAUDE.md" with no specifics, infer the likely drift from recent commits (phase markers in commit
   subjects are a strong signal in this repo, e.g. "Phase 3 Steps 1-2: ...") and confirm your read of
   what changed with the user before rewriting sections — this is a checked-in governance doc, not a
   scratch file.
3. **Cross-check against `doc/reference/`.** Pull the actual scope/done-criteria language for the
   target phase from its source doc rather than paraphrasing from memory of the old CLAUDE.md content.
4. **Diff and confirm frozen-adjacent changes.** If the update touches anything near §1/§2 — even just
   nearby wording — call it out explicitly and get a yes before writing.
5. **Edit CLAUDE.md** using targeted edits (not a full rewrite) so the diff is reviewable.
6. **Show the user what changed** (a summary of sections touched) and let them review — don't commit
   automatically. If they separately want it committed, that's a distinct ask (see the
   `commit-and-push` skill).

## Notes

- This skill is about keeping the doc honest, not about doing the underlying phase work. If asked to
  "update CLAUDE.md" but the phase work itself isn't actually done yet, say so instead of marking it
  done anyway.
- If `doc/reference/development-plan.md` itself is out of date relative to the phase docs, flag the
  discrepancy rather than silently picking one source over the other.
