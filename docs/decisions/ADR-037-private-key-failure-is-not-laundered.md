# ADR-037 — A private key that fails is not laundered through the shared pool

**Status:** accepted · Phase 6, Steps 5–8 · 2026-08-26
**Implements:** `phase6.md` §3 D40 (`project-overview.md` §9.2, §9.5)
**Relates to:** Contract A's failover flags (`app/providers/errors.py`),
[ADR-011](ADR-011-named-slot-spill.md) (a named slot spills into the fleet — the same "the chain
proceeds" instinct), [ADR-034](ADR-034-per-candidate-credential-resolution.md) (why the next
candidate resolves independently)

## Context

A user's own Gemini key is revoked, or hits its own rate limit. The candidate raises `AuthFailed` or
`RateLimited`. Nothing in the gateway prevents the router from retrying **the same candidate** on the
shared key: the model is reachable, the shared pool has budget, and the request would succeed.

It is tempting. The user never sees a failure, the answer arrives, and no support ticket is filed.

## Decision

**Refuse it. A private key's failure fails that candidate, and the chain proceeds normally to the
next one.** The one addition is disclosure: on `AuthFailed` from a **private-pool** attempt, flip that
row's `validation_status` to `'invalid'` — a fire-and-forget write in its own session, swallowing and
logging any failure so a broken disclosure write can never turn an already-recovered request into a
500. Not on `RateLimited`: a spent key is a working key.

`ProviderCredentials` gained `record_auth_failure(resolved)` for this. `SystemCredentials`'s is a
no-op — the shared pool has no per-user row to flag. Both candidate loops in `routing/router.py` and
tier 2 in `perception/extractors.py` call it under one guard:

```python
if isinstance(exc, AuthFailed) and resolved.pool == "private":
    await credentials.record_auth_failure(resolved)
```

## Why

**It hides a broken key indefinitely.** The Settings page says *Using your key*, every answer comes
from the shared pool, and nothing ever tells the user. That is precisely the failure §9.2 forbids at
the *add* step — "silently saving a bad key just means it fails later, mid-conversation, in a much
more confusing way" — arriving one layer down, where it is harder to notice and impossible to debug
from the outside.

**It misbills.** They opted into their own provider account specifically so the traffic would not
come out of the shared budget. Quietly moving it back is the opposite of what they asked for, and the
gateway's whole disclosure discipline (`served_by`, `substituted`, `extraction_tier`) exists to stop
exactly this class of silent helpfulness.

**It needs no new mechanism to do the right thing.** Contract A already marks `AuthFailed` and
`RateLimited` `failover_eligible`, so the chain moves to the *next candidate* — a different provider,
where the resolver answers independently and may well be shared. The user's Gemini being spent means
Groq answers. That is the gateway's entire premise, and it did not need a special case; refusing to
launder is a decision to **not** write code.

**Why the row is flagged on `AuthFailed` and not on `RateLimited`.** The two errors say different
things about the credential. A 401 means the key is wrong, revoked, or scoped away from that model —
a state that will not fix itself, and one the user must act on. A 429 means the key is *working* and
busy, which is the normal condition of a free tier and would flip a healthy row to `invalid` on any
busy afternoon. `validation_status` would stop meaning anything within a day.

**Why a separate write from the user's own re-check.** `record_validation_result` moves
`validation_status` *and* `last_validated_at` together, because a user pressing "Check again" really
did just ask the provider. `mark_invalid` moves only the status: nobody validated anything, a live
request simply failed with it, and stamping `last_validated_at` would claim a verification that never
happened. Two events, two functions, and the tests assert the timestamp difference directly.

## Consequences

- **The write had never been wired.** `mark_invalid` existed from Step 1 and nothing between Steps
  4 and 7 called it, so a private key's live `AuthFailed` failed over silently with no annotation —
  the exact failure this ADR exists to prevent, latent since the resolver was built. Writing the
  credential-leakage suite's "force an `AuthFailed` on the private key" scenario in Step 8 is what
  surfaced it. That is worth recording: a decision that is only a docstring is not implemented, and
  the test that proves it is what makes it real.
- The guard reads `resolved.pool`, not just the error class, and a unit test asserts that
  specifically: a shared-pool `RateLimited` from Groq must not touch anything, and only the
  private-pool `AuthFailed` from Gemini does.
- The settings row is where the user meets this (`ProviderKeysSection`): an `invalid` row renders a
  warning saying the provider rejected the key and that requests are going to the shared pool, and
  offers a **re-check** as well as a removal — a key rejected during a provider outage is worth
  asking about twice.
- A user whose only stored key is broken sees answers that keep arriving, from a different provider,
  disclosed by `served_by` as always. The gateway degrades; it does not lie about which key paid.
