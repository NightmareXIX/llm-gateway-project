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
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

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
# BYOK encryption — Phase 6 Step 2
# --------------------------------------------------------------------------- #
class CredentialUnreadable(RuntimeError):
    """A stored provider credential could not be decrypted.

    Raised rather than returning ``None`` or a partial string, so a caller can
    tell "no key stored" (a missing row) apart from "this row was written
    under a different ``ENCRYPTION_KEY``" (a rotated or corrupted one) — Step
    4's ``UserCredentials`` falls back to the shared pool on this specific
    error and logs it, which it can only do if the two cases raise
    differently.
    """


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """The process's one ``Fernet`` instance, built from ``Settings.ENCRYPTION_KEY``.

    Cached rather than rebuilt per call — key derivation is not free, and
    ``get_settings()`` is already cached, so this just extends that caching one
    step further. Key rotation is out of scope for v1; the seam for it is
    ``cryptography.fernet.MultiFernet``, not built.
    """
    key = get_settings().ENCRYPTION_KEY.get_secret_value()
    return Fernet(key.encode("ascii"))


def encrypt_provider_key(plaintext: str) -> str:
    """Encrypt a user's upstream provider credential for storage at rest.

    Fernet, keyed from ``Settings.ENCRYPTION_KEY``. Randomized per call (Fernet
    embeds a fresh IV every time), so encrypting the same plaintext twice never
    produces the same ciphertext — a deterministic scheme here would let a
    database read alone reveal which two users share a provider key.

    The plaintext must never be logged, returned over the wire, or put in an
    error message. ``provider_keys.last_4`` exists precisely so the settings UI
    never needs it back.
    """
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_provider_key(ciphertext: str) -> str:
    """Inverse of :func:`encrypt_provider_key`.

    Raises :class:`CredentialUnreadable` on a bad or foreign-keyed token —
    never ``None``, never a partial string. See that class's docstring for why
    the distinction matters to the caller.

    The result is a live credential: never log it, never put it in an error
    message, never return it over the wire.
    """
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialUnreadable(
            "stored provider credential could not be decrypted; ENCRYPTION_KEY "
            "may have been rotated since this row was written"
        ) from exc
