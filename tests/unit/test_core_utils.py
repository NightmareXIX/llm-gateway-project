"""The three small primitives everything else is built on: hashing, ids, time.

Small enough that splitting them across three modules would cost more in imports
than it buys in navigation. Each one is load-bearing in a way its size hides.

``crypto`` decides whether a leaked ``api_keys`` table hands out live credentials.
``ids`` decides whether a row's identity leaks its creation time. ``clock`` is the
seam every TTL and throttle in the system is tested through — the JWKS cache
today, the quota windows in Phase 3 — so a bug here is invisible in this module
and shows up as a flaky test three phases from now.

The two BYOK functions in ``crypto`` (``encrypt_provider_key`` /
``decrypt_provider_key``) got real bodies in Phase 6 Step 2; their behaviour is
covered in ``tests/unit/test_crypto.py`` rather than here, since they need an
``ENCRYPTION_KEY`` fixture the rest of this module has no reason to carry.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.core.clock import SYSTEM_CLOCK, Clock, FixedClock, SystemClock
from app.core.crypto import (
    BASE62_ALPHABET,
    constant_time_equals,
    generate_secret,
    sha256_hex,
)
from app.core.ids import new_uuid

# --------------------------------------------------------------------------- #
# crypto — hashing
# --------------------------------------------------------------------------- #
HEX_64 = re.compile(r"\A[0-9a-f]{64}\Z")


def test_the_digest_is_lowercase_hex_and_stable_across_calls() -> None:
    """Stability is the whole feature: the digest is stored once and looked up by
    equality on ``uq_api_keys_key_hash`` forever after."""
    first = sha256_hex("gw_live_abc")
    second = sha256_hex("gw_live_abc")

    assert first == second
    assert HEX_64.match(first)


def test_the_digest_is_not_the_thing_it_digests() -> None:
    assert sha256_hex("gw_live_abc") != "gw_live_abc"


@pytest.mark.parametrize(
    "left,right",
    [
        ("gw_live_abc", "gw_live_abd"),  # one character
        ("gw_live_abc", "gw_live_abC"),  # case
        ("gw_live_abc", "gw_live_abc "),  # trailing space
        ("", "x"),
    ],
)
def test_different_input_digests_differently(left: str, right: str) -> None:
    assert sha256_hex(left) != sha256_hex(right)


def test_non_ascii_hashes_rather_than_raising() -> None:
    """UTF-8 is spelled out in the implementation, so a nickname or an email with
    an accent in it cannot become a ``UnicodeEncodeError`` on the auth path."""
    assert HEX_64.match(sha256_hex("clé-privée-日本語"))


def test_equal_secrets_compare_equal() -> None:
    assert constant_time_equals("a" * 64, "a" * 64) is True


@pytest.mark.parametrize(
    "left,right",
    [
        ("a" * 64, "a" * 63 + "b"),  # differs only at the end
        ("a" * 64, "b" + "a" * 63),  # differs only at the start
        ("a" * 64, "a" * 63),  # differs in length
        ("", "a"),
    ],
)
def test_anything_else_compares_unequal(left: str, right: str) -> None:
    """Constant-time is untestable by timing in a unit test; what *is* testable is
    that the comparison is still correct, which is the part a hand-rolled
    "constant time" loop usually gets wrong."""
    assert constant_time_equals(left, right) is False


# --------------------------------------------------------------------------- #
# crypto — secret generation
# --------------------------------------------------------------------------- #
def test_a_secret_is_exactly_as_long_as_it_was_asked_to_be() -> None:
    """``token_urlsafe(32)`` returns 43 characters. The documented ``gw_live_<32>``
    format is only true because the alphabet is spelled out."""
    for length in (1, 8, 32, 64):
        assert len(generate_secret(length)) == length


def test_a_secret_contains_nothing_that_needs_escaping() -> None:
    """No ``-`` or ``_``: those show up in URLs, shell history and log greps under
    three different escaping rules."""
    secret = generate_secret(512)

    assert set(secret) <= set(BASE62_ALPHABET)
    assert "-" not in secret
    assert "_" not in secret


def test_secrets_do_not_repeat() -> None:
    assert len({generate_secret(32) for _ in range(200)}) == 200


@pytest.mark.parametrize("length", [0, -1, -32])
def test_a_zero_length_secret_is_refused_rather_than_returned_empty(length: int) -> None:
    """An empty string that verifies against an empty string is a key that
    authenticates nobody as somebody."""
    with pytest.raises(ValueError, match="positive"):
        generate_secret(length)


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #
def test_ids_are_random_uuid4s() -> None:
    """v4 rather than v7 on purpose: v7 packs the index better but leaks a row's
    creation time to anyone holding its id."""
    identifier = new_uuid()

    assert isinstance(identifier, UUID)
    assert identifier.version == 4


def test_ids_do_not_repeat() -> None:
    assert len({new_uuid() for _ in range(1000)}) == 1000


# --------------------------------------------------------------------------- #
# clock
# --------------------------------------------------------------------------- #
def test_the_system_clock_is_timezone_aware_utc() -> None:
    """Every ``timestamptz`` column hands back an aware value, and comparing one
    against a naive datetime is a ``TypeError`` at runtime — on whichever code
    path happens to hit it first."""
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_both_implementations_satisfy_the_protocol() -> None:
    """Structurally, not with ``isinstance``. :class:`Clock` is deliberately not
    ``@runtime_checkable`` — it is a static contract, and mypy is what enforces
    it. What is worth asserting at runtime is that the two implementations really
    are interchangeable at the one call site every consumer uses."""
    candidates: list[Clock] = [SYSTEM_CLOCK, SystemClock(), FixedClock(datetime.now(UTC))]

    for clock in candidates:
        now = clock.now()
        assert isinstance(now, datetime)
        assert now.utcoffset() == timedelta(0)


def test_a_fixed_clock_does_not_move_on_its_own() -> None:
    clock = FixedClock(datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC))

    assert clock.now() == clock.now() == datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def test_advancing_moves_the_same_instance_the_component_is_holding() -> None:
    """Mutability is the feature. A component takes the clock at construction, and
    a test advances that object rather than handing it a replacement — which is
    what makes "the cache is now stale" expressible without sleeping."""
    clock = FixedClock(datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC))
    held: Clock = clock

    returned = clock.advance(90)

    assert returned == datetime(2026, 8, 10, 12, 1, 30, tzinfo=UTC)
    assert held.now() == returned


def test_a_fractional_advance_is_honoured() -> None:
    clock = FixedClock(datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC))
    clock.advance(0.5)

    assert clock.now() == datetime(2026, 8, 10, 12, 0, 0, 500_000, tzinfo=UTC)


def test_set_jumps_to_an_arbitrary_instant() -> None:
    clock = FixedClock(datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC))
    clock.set(datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC))

    assert clock.now() == datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_a_non_utc_instant_is_normalized_rather_than_kept() -> None:
    """So a test written in a local timezone compares equal to what the database
    hands back, instead of failing by an offset."""
    clock = FixedClock(datetime(2026, 8, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))))

    assert clock.now() == datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    assert clock.now().utcoffset() == timedelta(0)


def test_a_naive_datetime_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 8, 10, 12, 0, 0))


def test_a_naive_datetime_is_refused_by_set_too() -> None:
    """The easier one to forget, and it would poison every later comparison."""
    clock = FixedClock(datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC))

    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 8, 10, 13, 0, 0))
