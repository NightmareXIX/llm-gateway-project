"""Contract C's key schema, asserted as strings.

These tests look tautological — a builder returns an f-string and the test spells
the same f-string out — and that is exactly what they are for. Contract C is
frozen, the keys are a wire format shared with every future instance of this
process, and a "harmless" rename of a segment is invisible at runtime: the writer
writes ``cb:groq:x``, the reader reads ``breaker:groq:x``, both succeed, and the
breaker simply never opens. Nothing raises. Spelling the format out here is what
turns that class of change into a red test instead of a silent behaviour loss.

The non-obvious cases, each of which is a real hazard:

- **A model id may contain a colon.** ``openai/gpt-oss-20b:free`` is a live entry
  in ``config/providers.yaml`` and its ``:free`` suffix is load-bearing (dropping
  it routes to the paid variant). A builder that rejected or escaped colons would
  break OpenRouter on the day its adapter lands.
- **An empty segment collapses two keys into one.** Every user sharing one
  rate-limit counter is a security-shaped bug, not a typo.
- **Scope is what keeps BYOK and the shared pool apart.** If two scopes could
  produce one key, a user with their own Gemini key would spend the shared pool's
  budget.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.cache import keys

USER_ID = UUID("11111111-2222-3333-4444-555555555555")


# --------------------------------------------------------------------------- #
# Quota
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("window", ["rpm", "rpd", "tpm", "tpd"])
def test_every_quota_window_has_its_own_counter(window: keys.QuotaWindow) -> None:
    """Four windows, four keys. A provider 429s on whichever it hits first, so
    they cannot share a counter."""
    built = keys.quota(keys.SYSTEM_SCOPE, "groq", "llama-3.3-70b-versatile", window)

    assert built == f"q:system:groq:llama-3.3-70b-versatile:{window}"


def test_scope_separates_the_shared_pool_from_a_users_own_key() -> None:
    """§9.4: shared-pool and BYOK usage must never count against each other."""
    shared = keys.quota(keys.SYSTEM_SCOPE, "gemini", "gemini-3.6-flash", "rpd")
    private = keys.quota(str(USER_ID), "gemini", "gemini-3.6-flash", "rpd")

    assert shared != private
    assert shared == "q:system:gemini:gemini-3.6-flash:rpd"
    assert private == f"q:{USER_ID}:gemini:gemini-3.6-flash:rpd"


def test_the_reservation_key_hangs_off_the_same_prefix() -> None:
    """Same prefix as the counters it reserves against — that adjacency is what
    lets one Lua script take all of them as KEYS without rebuilding the scope."""
    built = keys.quota_reservation("system", "groq", "llama-3.1-8b-instant", "req-abc")

    assert built == "q:system:groq:llama-3.1-8b-instant:res:req-abc"


def test_the_perception_lane_is_a_sub_counter_not_a_separate_scope() -> None:
    """D8's 50/50 split is a slice of Gemini's *own* budget, not a second budget."""
    built = keys.quota_perception_lane("system", "gemini", "gemini-3.6-flash")

    assert built == "q:system:gemini:gemini-3.6-flash:lane:perception"


def test_a_uuid_request_id_is_accepted_as_itself() -> None:
    """Request ids arrive as UUIDs in some call paths and strings in others.
    Requiring the caller to stringify is how one of them forgets."""
    request_id = UUID("99999999-8888-7777-6666-555555555555")

    built = keys.quota_reservation("system", "groq", "m", request_id)

    assert built.endswith(f":res:{request_id}")


# --------------------------------------------------------------------------- #
# Breaker, cache, perception, API surface, auth, streaming
# --------------------------------------------------------------------------- #
def test_the_breaker_key_is_not_scoped_by_user() -> None:
    """A provider being down is a fact about the provider. Scoping it per user
    would make every user rediscover the same outage independently."""
    assert keys.breaker("groq", "llama-3.3-70b-versatile") == "cb:groq:llama-3.3-70b-versatile"


def test_the_remaining_builders_match_the_contract() -> None:
    """One assertion per Contract C row that has no separate hazard of its own."""
    assert keys.exact_cache("a" * 64) == f"cache:exact:{'a' * 64}"
    assert keys.extraction("deadbeef") == "extract:deadbeef"
    assert keys.extraction_lock("deadbeef") == "lock:extract:deadbeef"
    assert keys.idempotency(USER_ID, "client-key-1") == f"idem:{USER_ID}:client-key-1"
    assert keys.rate_limit(USER_ID, "rpm", 1_760_000_000) == f"rl:{USER_ID}:rpm:1760000000"
    assert keys.jwks_supabase() == "jwks:supabase"
    assert keys.stream_attempts(USER_ID) == f"stream:{USER_ID}:attempts"


def test_the_extraction_lock_is_a_different_key_from_the_extraction() -> None:
    """They share the hash and must not share the key: the lock expires in 60s and
    the value in 24h, so collapsing them would drop the cache every minute."""
    assert keys.extraction("h") != keys.extraction_lock("h")


def test_a_negative_rate_limit_window_is_refused() -> None:
    """A negative epoch second means the caller computed the window wrong, and the
    resulting key sorts and expires like nothing else in the space."""
    with pytest.raises(ValueError, match="non-negative"):
        keys.rate_limit(USER_ID, "rpm", -1)


# --------------------------------------------------------------------------- #
# Segment validation
# --------------------------------------------------------------------------- #
def test_a_colon_inside_a_model_id_survives_intact() -> None:
    """Phase 2 trap 9. ``:free`` is part of OpenRouter's model name — dropping or
    escaping it silently routes to the paid variant. Keys are built and never
    parsed, so the extra colon costs nothing."""
    model = "openai/gpt-oss-20b:free"

    assert keys.breaker("openrouter", model) == f"cb:openrouter:{model}"
    assert keys.quota("system", "openrouter", model, "rpd").endswith(f"{model}:rpd")


def test_an_empty_segment_raises_rather_than_collapsing_two_keys_into_one() -> None:
    """The failure this prevents is silent: an empty user id would give every
    caller the same rate-limit counter, and nothing would error."""
    with pytest.raises(ValueError, match="user_id"):
        keys.rate_limit("", "rpm", 0)


def test_a_whitespace_segment_raises() -> None:
    """A key with a space in it cannot be typed into redis-cli during the incident
    it caused, and reaching this state at all means a value was mis-parsed."""
    with pytest.raises(ValueError, match="whitespace"):
        keys.breaker("groq", "llama 3.3")


def test_the_offending_segment_is_named() -> None:
    """Four-segment keys are common here; "a segment was empty" without saying
    which one is a message that sends you reading the builder."""
    with pytest.raises(ValueError, match="'provider'"):
        keys.quota("system", "", "m", "rpm")


# --------------------------------------------------------------------------- #
# TTLs
# --------------------------------------------------------------------------- #
def test_the_ttls_match_the_contract_table() -> None:
    """§2.3's TTL column, transcribed. These are as much a part of the contract as
    the key names — a per-minute counter with a per-hour TTL is a false 429."""
    assert keys.QUOTA_WINDOW_TTL_S == 60
    assert keys.RESERVATION_TTL_S == 120
    assert keys.BREAKER_TTL_S == 3600
    assert keys.EXACT_CACHE_TTL_S == 3600
    assert keys.EXTRACTION_TTL_S == 86_400
    assert keys.EXTRACTION_LOCK_TTL_S == 60
    assert keys.IDEMPOTENCY_TTL_S == 86_400
    assert keys.JWKS_TTL_S == 43_200
    assert keys.STREAM_ATTEMPTS_TTL_S == 300


def test_the_daily_windows_have_no_constant_ttl() -> None:
    """Deliberate. ``rpd`` resets when the *provider* says — midnight Pacific for
    Gemini (``fixed_daily_pt``), a rolling window elsewhere — so a plausible 86400
    here would be wrong by up to eight hours and wrong invisibly."""
    assert keys.UNTIL_PROVIDER_RESET is None


def test_the_breaker_ttl_outlives_the_longest_cooldown() -> None:
    """The cooldown ladder tops out at 300s. If the state hash could expire first,
    an open breaker would resurrect as closed and undo its own decision."""
    assert keys.BREAKER_TTL_S > 300
