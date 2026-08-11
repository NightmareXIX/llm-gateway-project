"""Gateway-issued ``gw_live_`` API keys — the programmatic half of D7.

Supabase sessions are for humans in a browser. These are for scripts, SDKs and
anything else that cannot do an OAuth dance — and they are what make this a
*gateway* rather than a chat app.

The key is shown exactly once, at creation. Only its SHA-256 digest is stored
(``app/core/crypto.py`` explains why SHA-256 and not bcrypt), alongside a prefix
and the last four characters so the settings UI has something recognizable to
display. There is no path back from the database to a working credential; a lost
key is replaced, never recovered.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.crypto import constant_time_equals, generate_secret, sha256_hex

KEY_PREFIX = "gw_live_"
"""Namespaced and greppable. Anyone scanning a repository for leaked credentials
— including GitHub's own secret scanning — matches on a prefix, and ``gw_test_``
stays available for a future sandbox mode."""

KEY_SECRET_LENGTH = 32
"""32 base62 characters ≈ 190 bits. Not guessable, at any rate limit."""

LAST_4_LENGTH = 4


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """A freshly minted key: the one copy of the plaintext, plus what gets stored."""

    plaintext: str
    """Returned to the caller once and then dropped. Never logged, never stored."""

    key_hash: str
    key_prefix: str
    last_4: str

    @property
    def masked(self) -> str:
        """The display form, e.g. ``gw_live_…a91c``."""
        return mask(self.key_prefix, self.last_4)


def mask(key_prefix: str, last_4: str) -> str:
    """Render a stored key for display. Reveals nothing usable."""
    return f"{key_prefix}…{last_4}"


def generate_api_key() -> GeneratedKey:
    """Mint a new key. The only place a gateway credential comes into existence."""
    secret = generate_secret(KEY_SECRET_LENGTH)
    plaintext = f"{KEY_PREFIX}{secret}"
    return GeneratedKey(
        plaintext=plaintext,
        key_hash=hash_api_key(plaintext),
        key_prefix=KEY_PREFIX,
        last_4=plaintext[-LAST_4_LENGTH:],
    )


def hash_api_key(plaintext: str) -> str:
    """The digest stored in ``api_keys.key_hash`` and looked up on every request."""
    return sha256_hex(plaintext)


def parse_api_key(raw: str | None) -> str | None:
    """Validate the *shape* of a presented key, returning it stripped or ``None``.

    Runs before the database is touched, so a header full of junk costs a string
    comparison rather than a query. This is a well-formedness check and nothing
    more — a syntactically perfect key that was never issued still fails at the
    lookup.
    """
    if raw is None:
        return None

    candidate = raw.strip()
    if len(candidate) != len(KEY_PREFIX) + KEY_SECRET_LENGTH:
        return None
    if not candidate.startswith(KEY_PREFIX):
        return None

    secret = candidate[len(KEY_PREFIX) :]
    if not secret.isalnum() or not secret.isascii():
        return None
    return candidate


def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    """Confirm a presented key matches a stored digest.

    The security here comes from the lookup itself: ``api_keys.key_hash`` is
    unique and indexed, so a row is only found when the digests are already
    equal. This re-check is belt-and-braces against a future caller that resolves
    a row some other way — and it compares in constant time so that caller cannot
    turn it into an oracle.
    """
    return constant_time_equals(hash_api_key(plaintext), stored_hash)
