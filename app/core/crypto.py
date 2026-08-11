"""Hashing, secret generation, and the encryption seam for BYOK.

**Why SHA-256 and not bcrypt/argon2 for gateway API keys.** Password hashes are
deliberately slow because passwords are low-entropy and human-chosen — the work
factor is what buys time after a database leak. A ``gw_live_`` key is 32 base62
characters minted by ``secrets``: roughly 190 bits of entropy, which is not
brute-forceable at any work factor, so slowness buys nothing. It costs plenty,
though: a per-request bcrypt comparison against every stored hash, because you
cannot index a salted hash. A plain SHA-256 makes verification a single indexed
equality lookup, which is exactly what the ``uq_api_keys_key_hash`` unique index
on ``api_keys.key_hash`` is for.

That reasoning does not transfer to passwords. The gateway never sees one —
Supabase owns them (D7).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

BASE62_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
"""Explicit alphabet so a generated secret's length is exactly what was asked for.

``secrets.token_urlsafe(n)`` returns a base64 encoding whose length is a function
of *n bytes*, not of any character count, and it can emit ``-`` and ``_`` — which
then show up in URLs, shell history and log greps with different escaping rules.
"""


def sha256_hex(value: str) -> str:
    """Lowercase hex SHA-256 of ``value`` (UTF-8). Stable across processes."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two secrets without leaking their common prefix through timing."""
    return hmac.compare_digest(left, right)


def generate_secret(length: int) -> str:
    """A cryptographically random base62 string of exactly ``length`` characters."""
    if length <= 0:
        raise ValueError("length must be positive")
    return "".join(secrets.choice(BASE62_ALPHABET) for _ in range(length))


# --------------------------------------------------------------------------- #
# BYOK encryption — Phase 6 seam
# --------------------------------------------------------------------------- #
def encrypt_provider_key(plaintext: str) -> str:
    """Encrypt a user's upstream provider credential for storage at rest.

    Phase 6. Fernet, keyed from ``Settings.ENCRYPTION_KEY``. The signature and
    return type are the contract; only the body is deferred.
    """
    raise NotImplementedError("BYOK credential encryption lands in Phase 6")


def decrypt_provider_key(ciphertext: str) -> str:
    """Inverse of :func:`encrypt_provider_key`. Phase 6.

    The result is a live credential: never log it, never put it in an error
    message, never return it over the wire. ``provider_keys.last_4`` exists so
    the settings UI never needs the plaintext back.
    """
    raise NotImplementedError("BYOK credential decryption lands in Phase 6")
