---
name: commit-and-push
description: Stage the relevant changes, create a git commit with a well-drafted message, and push to the current branch's remote. Use when the user asks to "commit and push", "push this up", "commit this and push it", or similar — a single request covering both the commit and the push, not just a commit.
---

# Commit and push

Create a commit for the current changes and push it to the remote, in one flow. This skill composes
the ordinary commit workflow with a push step — use it when the user's request covers both, not just
"commit this."

## Steps

1. **Survey state.** Run these in parallel:
   - `git status` (never `-uall`)
   - `git diff` (unstaged) and `git diff --staged` (already-staged changes that will be included)
   - `git log --oneline -10` to match this repo's commit message style
   - Check whether the current branch tracks a remote and whether it's ahead/behind
     (`git status -sb` or `git rev-list --left-right --count HEAD...@{u}`)

2. **Draft the commit message.** 1-2 sentences, focused on *why* not *what*. Match the tone/format of
   recent commits from step 1 (this repo uses short imperative subject lines, e.g. "Phase 2 Step 8:
   server-side SSE framing"). Do not mention Claude or Claude Code in the message.

3. **Stage deliberately.** Add specific files by name — never `git add -A` or `git add .`. Before
   staging, check for anything that shouldn't be committed:
   - Files that look like secrets/credentials (`.env`, `*.pem`, `credentials.json`, etc.) — warn the
     user and exclude unless they explicitly confirm.
   - Files unrelated to the requested change that happen to be dirty in the working tree — leave those
     unstaged unless the user asked for a full commit of everything.

4. **Commit.** Use a heredoc so message formatting survives:
   ```
   git commit -m "$(cat <<'EOF'
   <subject line>

   <optional body>
   EOF
   )"
   ```
   - Never `--amend` unless the user explicitly asked for it.
   - Never `--no-verify` or other hook-skipping flags.
   - If a pre-commit hook fails: fix the underlying issue, re-stage, and make a **new** commit — do not
     bypass the hook.

5. **Confirm before pushing.** Pushing is visible to others and can affect shared state, so before
   running `git push`:
   - If the branch has no upstream yet, the push will need `-u origin <branch>` — call this out.
   - If the branch is `main`/`master` (or otherwise looks like a shared/protected branch), or if the
     remote has commits this branch doesn't (diverged), stop and confirm with the user rather than
     pushing — do not force-push under any circumstances without explicit, specific confirmation.
   - Otherwise, state what you're about to push (branch → remote) and proceed. A user who invoked a
     skill named "commit and push" has already signaled intent to push a normal, non-diverged branch —
     don't re-ask for the routine case, only escalate for the risky ones above.

6. **Verify.** Run `git status` after the push to confirm it succeeded and the branch is now up to
   date with its upstream. Report the commit hash and branch/remote pushed to.

## Notes

- If there is nothing staged and nothing to stage (no changes at all), say so and stop — don't create
  an empty commit.
- If the repo has no remote configured, say so and stop before attempting a push.
- Follow the git safety rules from the system prompt at all times: no force-push to main/master without
  explicit request, no `git reset --hard`/`clean -f` as a shortcut, no skipping hooks.
