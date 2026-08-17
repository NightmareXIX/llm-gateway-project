# ADR-019 — Fixed counters under a frozen key schema

**Status:** accepted · Phase 3, Step 2 · 2026-08-17
**Implements:** `phase3.md` §3 D16
**Relates to:** Contract C (`app/cache/keys.py`, frozen), [ADR-020](ADR-020-quota-reservation-placement.md)
(what consumes these windows), [ADR-022](ADR-022-our-own-rate-limiting.md) (the one counter that
gets the sliding version this ADR explains why the rest cannot have)

## Context

`limits.yaml` calls RPM and TPM `rolling_60s`. Contract C's key for them,
`q:{scope}:{provider}:{model}:{rpm|rpd|tpm|tpd}`, carries no window-start component — unlike
`rl:{user_id}:{window}:{window_start}`, which our own limiter gets. A true rolling window needs
either a sorted set of per-request timestamps or the two-bucket interpolation
(`windows.py::sliding_count`) our own limiter uses; both need a `window_start` segment to bucket
against. Contract C is frozen, and this key does not have one.

RPD is a second, unrelated problem in the same module: Groq's day ends at midnight UTC, Gemini's
ends at midnight in California — a different UTC instant in summer than in winter — and neither is
a rolling 24 hours from the last request.

## Decision

**Fixed windows, aligned to the first increment, for every provider-side counter.** `INCRBY`
against a plain `q:{scope}:{provider}:{model}:{window}` key; the TTL is set **once, when the
counter is created** for `rpm`/`tpm`, and **recomputed on every increment** for `rpd`/`tpd`
(`quota/scripts/reserve.lua`'s `refresh` flag, threaded in from `_CONVERGING_RESETS` in
`tracker.py`).

**`resets_at`/`ttl_s` (`quota/windows.py`) name three reset kinds:** `rolling_60s` → `now + 60s`,
recomputed fresh each call since it is never a real wall-clock boundary. `fixed_daily_utc` → the
next `00:00` UTC strictly after `now`. `fixed_daily_pt` → the next local midnight in
`ZoneInfo("America/Los_Angeles")`, converted back to UTC — never a hardcoded `-8`, because that
offset is wrong for roughly eight months a year once DST is in effect. `tzdata` is a **runtime**
dependency (not just dev) because Windows ships no zoneinfo database at all and slim container
images are not guaranteed to either; a `ZoneInfoNotFoundError` on first use would be a 500 in the
middle of the phase's most-demoed feature.

**`QUOTA_HEADROOM_FRACTION = 0.1`** (`tracker.py::_effective_limit`) reduces every published limit
by ten percent before the script ever sees it, to absorb the fixed window's own overshoot.

## Why

**The honest cost of a fixed window, stated rather than hidden.** A window aligned to its first
increment permits up to 2× the limit across a boundary it happens to straddle — thirty requests at
`:59` and thirty more at `:01` is sixty inside a sixty-two-second span against a limit of thirty.
Three things make this acceptable rather than merely tolerated: the overshoot is bounded and brief
(one window's width, once); the provider answers a real 429 and the breaker opens exactly as it
did before this phase existed, so the worst case is the Phase 2 behavior, not something new; and
the headroom fraction holds the effective ceiling under the real one by more than the jitter costs.
A gateway whose entire premise is running on borrowed free tiers should be under-spending them on
purpose.

**`rpd`/`tpd` refresh their TTL on every increment because their reset target is a real instant,
not a duration.** Recomputing "seconds until the next reset" and re-`EXPIRE`ing with it is
idempotent — the same wall-clock target comes out no matter how many times it runs — and it is what
lets the counter survive a Redis restart mid-day without losing its alignment to midnight.

**`rpm`/`tpm` must not do the same thing, and getting this backwards is trap 1.** A per-minute
counter whose TTL is refreshed on every increment never expires under sustained traffic — the key
is perpetually "just created" from Redis's point of view, `rpm` climbs forever, and the gateway
serves a permanent false 429 that reads exactly like a provider outage, on a provider that is
actually fine. `reserve.lua` sets the TTL only when the `INCRBY` return equals the cost (the
counter was just created by this call) — the one moment it is safe to claim "sixty seconds from
now" means anything.

**`ZoneInfo`, never a fixed offset.** `America/Los_Angeles` is `-08:00` for roughly a third of the
year and `-07:00` for the rest. A hardcoded offset produces an `resets_at` an hour off and an `rpd`
counter that resets an hour early or late for eight months running — wrong quietly, which is worse
than wrong loudly. `_next_midnight` builds the boundary from the *date* and re-localizes, rather
than adding 24 hours to the current instant, specifically because adding a fixed duration across a
DST transition does not land on local midnight — the clocks jump underneath the arithmetic. The
unit suite asserts this on both sides of a transition: consecutive `fixed_daily_pt` resets are 23
or 25 hours apart in transition weeks, never a naive 24.

**Sliding here, fixed there, and the contrast is deliberate rather than an inconsistency to smooth
over later.** Our own rate limiter (D20/ADR-022) gets the sliding two-bucket interpolation because
Contract C's `rl:` key is the one place a `window_start` segment exists to interpolate from. The
provider-side counters do not have that segment, and the frozen key schema is worth more than the
extra precision a sliding window would buy — asking first before amending a frozen contract, per
CLAUDE.md, is worth more than either.

## Consequences

- The 2× boundary overshoot is documented in `docs/limitations.md` rather than quietly absorbed —
  it is a real, bounded cost of the frozen key schema, not a hidden one.
- `declared()` drops any window whose `limits.yaml` value is `null` rather than treating it as
  unlimited or as zero — a provider that does not publish a `tpd` ceiling has no `tpd` counter at
  all, and the tracker never invents one.
- `tzdata>=2025.1` is a `pyproject.toml` **runtime** dependency, not a dev-only one — the container
  image needs it in production, not just in CI.
- A window declared with a limit but no matching `reset` entry in `limits.yaml` is a config bug and
  `declared()` raises rather than guessing a reset kind — the failure surfaces at startup validation
  time, not as a silently wrong `resets_at` three requests into a demo.
