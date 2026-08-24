"""BYOK credential encryption at rest — Phase 6 Step 2.

The two seams in ``core/crypto.py`` (``encrypt_provider_key`` /
``decrypt_provider_key``) plus the boot-time check in ``config.py`` that keeps
a malformed ``ENCRYPTION_KEY`` from surfacing on a user's first paste instead
of at startup. What matters here is not "Fernet works" — that is the
library's own test suite — but the two properties this codebase depends on:
randomization (so a stored ciphertext alone cannot reveal which two users
share a provider key) and a typed failure that lets a rotated key be told
apart from a missing row (D38's fallback path in Step 4 depends on it).

Every test gets its own valid Fernet key via ``isolated_encryption_key``,
clearing both ``get_settings``'s cache and ``crypto._fernet``'s — two
independent ``lru_cache``s that would otherwise leave a stale ``Fernet``
instance keyed on a previous test's value.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

from app.config import ConfigError, get_settings, validate_startup_config
from app.core import crypto


def _generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _clear_caches() -> None:
    get_settings.cache_clear()
    crypto._fernet.cache_clear()


@pytest.fixture
def isolated_encryption_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh, valid Fernet key for the duration of one test."""
    key = _generate_key()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    _clear_caches()
    yield key
    _clear_caches()


def test_a_plaintext_round_trips(isolated_encryption_key: str) -> None:
    ciphertext = crypto.encrypt_provider_key("sk-a-real-provider-key")

    assert crypto.decrypt_provider_key(ciphertext) == "sk-a-real-provider-key"


def test_encryption_is_randomized(isolated_encryption_key: str) -> None:
    """Fernet embeds a fresh IV per call. Asserted directly because a
    deterministic scheme here would let a database read alone reveal which two
    users happen to share a provider key."""
    first = crypto.encrypt_provider_key("sk-same-plaintext")
    second = crypto.encrypt_provider_key("sk-same-plaintext")

    assert first != second
    assert crypto.decrypt_provider_key(first) == "sk-same-plaintext"
    assert crypto.decrypt_provider_key(second) == "sk-same-plaintext"


def test_a_tampered_token_is_unreadable(isolated_encryption_key: str) -> None:
    ciphertext = crypto.encrypt_provider_key("sk-a-real-provider-key")
    mid = len(ciphertext) // 2
    replacement = "A" if ciphertext[mid] != "A" else "B"
    tampered = ciphertext[:mid] + replacement + ciphertext[mid + 1 :]

    with pytest.raises(crypto.CredentialUnreadable):
        crypto.decrypt_provider_key(tampered)


def test_a_token_from_a_different_key_is_unreadable(
    isolated_encryption_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row written under a rotated ``ENCRYPTION_KEY`` must raise the same
    typed error a tampered token does — Step 4's ``UserCredentials`` treats
    both as "cannot use this credential", not as "no key stored"."""
    ciphertext = crypto.encrypt_provider_key("sk-a-real-provider-key")

    monkeypatch.setenv("ENCRYPTION_KEY", _generate_key())
    _clear_caches()

    with pytest.raises(crypto.CredentialUnreadable):
        crypto.decrypt_provider_key(ciphertext)


def test_validate_startup_config_accepts_a_real_key(isolated_encryption_key: str) -> None:
    """The happy path for the boot check this step adds — every other test in
    this module implicitly exercises the failure side."""
    validate_startup_config()


def test_validate_startup_config_rejects_a_malformed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module's "fail loudly at boot" promise, applied to this variable: a
    bad key kills the process at startup, naming itself, rather than failing
    on a user's first paste mid-conversation."""
    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")
    _clear_caches()

    with pytest.raises(ConfigError, match="ENCRYPTION_KEY"):
        validate_startup_config()

    _clear_caches()
