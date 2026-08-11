# ADR-007 — Two authentication surfaces, one `Principal`

**Status:** accepted · Phase 1, Step 3 · 2026-08-10
**Implements:** D7 (§1.2 of `doc/reference/contracts-and-phase1.md`)

## Context

The gateway has two kinds of caller and they have nothing in common operationally.
A human signs in through a browser and expects registration, password reset, email
confirmation and OAuth to exist. A script has no browser, cannot complete an OAuth
redirect, and wants one long-lived credential in a header.

Building the first from scratch means storing password hashes, sending
confirmation mail, and owning a password-reset flow — three chances to get
security wrong, none of which is what this project is about. Building only the
second makes this a chat app with an API-shaped door, not a gateway.

## Decision

**Supabase Auth for humans.** It handles registration, password hashing, email
confirmation, OAuth and reset, on the same free-tier Postgres already in use. The
gateway never sees a password. FastAPI verifies the JWT against the project's
JWKS and mirrors the user locally as `users(id, email, email_verified, tier)`,
upserted on every authenticated request — so foreign keys stay local and
app-specific fields can be added without touching the auth schema.

**Gateway-issued `gw_live_` keys for programs.** 32 base62 characters, SHA-256 in
the database, prefix and last four kept for display, shown in plaintext exactly
once.

**Both collapse to one `Principal`** (`user_id`, `auth_method`, `api_key_id`,
`tier`). Nothing downstream branches on which door was used.

### Supporting choices, and why

**Quota keys on `user_id`, never `api_key_id`.** A user with three integrations is
one user with one budget. Keying on the key would make quota trivially
multipliable by anyone who can click "create key". `api_key_id` stays on the
`requests` row for attribution — *which* integration made the call, as distinct
from *whose* budget paid for it.

**SHA-256, not bcrypt, for API keys.** A work factor buys time against
brute-forcing low-entropy human passwords. A 190-bit random key is not
brute-forceable at any work factor, so slowness buys nothing — while costing a
comparison against every stored row, because a salted hash cannot be indexed.
SHA-256 makes verification one indexed equality lookup. The reasoning explicitly
does not transfer to passwords, which we do not store.

**HS256 is refused.** Supabase signs with ES256/RS256 and publishes the public
half. Accepting a symmetric algorithm alongside a JWKS-sourced key enables
algorithm confusion: the attacker signs their own token using the public key
bytes as an HMAC secret, and a library that trusts the header's `alg` verifies it.
The algorithm is checked before a key is looked up.

**The gateway re-checks `email_verified` that Supabase already enforces.** With
"Confirm email" on — the standing configuration in every environment, dev
included — Supabase should never issue a session to an unconfirmed user, so this
branch should be unreachable. It is defence in depth against that setting being
flipped, and it costs one dictionary lookup. Unverified gets its own 401 code,
`email_not_verified`, because the correct UI response is "check your inbox", not
a login screen. A missing claim reads as unverified (fail closed), gated by
`REQUIRE_VERIFIED_EMAIL` — the claim lives in `user_metadata`, which is Supabase's
shape and not a standard, and if they move it we want an env var between us and
locking out every user, not an emergency redeploy.

**Anonymous sessions are refused outright.** They are off by default in Supabase;
rejecting `is_anonymous` keeps them off by construction rather than by a dashboard
toggle nobody re-checks.

**API keys cannot manage API keys.** `/v1/keys` requires `auth_method ==
"session"`. Otherwise a leaked key mints its own successors and survives the
revocation of the original.

## Consequences

- A hard dependency on Supabase being reachable. Mitigated by a 12-hour JWKS
  cache that keeps serving stale keys through a fetch failure; only a cold cache
  plus an outage is fatal, and that is a 503, not a 401.
- Unknown-`kid` refreshes are floored at 60s, so a caller cannot turn each request
  they send into a request we send to Supabase.
- Two verification paths to keep correct, and one `Principal` type that must not
  grow fields — the moment it carries `email`, half the app reads it from there
  and half re-fetches the row.
- Email confirmation stays on everywhere, which means custom SMTP is required:
  Supabase's built-in sender is rate-limited to a handful of messages an hour and
  is hit during ordinary development, not in production.
- Sessions are not revocable server-side before their `exp`. Acceptable at
  Supabase's short access-token lifetime; API keys, which are long-lived, are
  revocable immediately.
