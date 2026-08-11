"""Gateway API key generation, hashing, and shape validation."""

from __future__ import annotations

from app.auth.api_keys import (
    KEY_PREFIX,
    KEY_SECRET_LENGTH,
    generate_api_key,
    hash_api_key,
    mask,
    parse_api_key,
    verify_api_key,
)


def test_generated_key_has_the_documented_shape() -> None:
    generated = generate_api_key()

    assert generated.plaintext.startswith(KEY_PREFIX)
    assert len(generated.plaintext) == len(KEY_PREFIX) + KEY_SECRET_LENGTH
    assert generated.key_prefix == KEY_PREFIX
    assert generated.last_4 == generated.plaintext[-4:]


def test_keys_are_unique() -> None:
    """A trivial-looking assertion that catches a seeded or reused RNG."""
    keys = {generate_api_key().plaintext for _ in range(100)}
    assert len(keys) == 100


def test_hash_is_stable_and_is_not_the_key() -> None:
    generated = generate_api_key()

    assert generated.key_hash == hash_api_key(generated.plaintext)
    assert generated.plaintext not in generated.key_hash
    assert len(generated.key_hash) == 64  # sha256 hex


def test_verify_accepts_the_original_and_rejects_anything_else() -> None:
    generated = generate_api_key()
    other = generate_api_key()

    assert verify_api_key(generated.plaintext, generated.key_hash) is True
    assert verify_api_key(other.plaintext, generated.key_hash) is False


def test_masked_form_reveals_nothing_usable() -> None:
    generated = generate_api_key()
    masked = generated.masked

    assert masked == mask(KEY_PREFIX, generated.last_4)
    assert generated.plaintext not in masked
    # Everything but the prefix and the last four is gone.
    assert len(masked) < len(generated.plaintext)


def test_parse_accepts_a_real_key_and_strips_whitespace() -> None:
    generated = generate_api_key()

    assert parse_api_key(generated.plaintext) == generated.plaintext
    assert parse_api_key(f"  {generated.plaintext}\n") == generated.plaintext


def test_parse_rejects_anything_not_key_shaped() -> None:
    """Cheap rejection before the database is touched."""
    valid = generate_api_key().plaintext

    assert parse_api_key(None) is None
    assert parse_api_key("") is None
    assert parse_api_key("nonsense") is None
    assert parse_api_key(valid[:-1]) is None  # too short
    assert parse_api_key(valid + "x") is None  # too long
    assert parse_api_key(valid.replace(KEY_PREFIX, "gw_test_")) is None  # wrong prefix
    assert parse_api_key(KEY_PREFIX + "!" * KEY_SECRET_LENGTH) is None  # not base62
