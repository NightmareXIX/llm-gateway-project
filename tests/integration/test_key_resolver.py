"""``app/keys_resolution/resolver.py`` — Phase 6 Step 4 (D36, D38).

Needs a real session factory: ``UserCredentials`` opens and closes its own
session inside ``_load_once``, which is exactly the D14 shape
``PerceptionResolver`` already uses and exactly why this test needs the real
database fixture rather than a unit-test double, per the step's own file
list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache import keys
from app.core.crypto import encrypt_provider_key
from app.db.repo import provider_keys as provider_keys_repo
from app.keys_resolution import resolver as resolver_module
from app.keys_resolution.resolver import ResolvedKey, SystemCredentials, UserCredentials
from app.providers.registry import ProviderRegistry

pytestmark = pytest.mark.integration

SessionFactory = async_sessionmaker[AsyncSession]


def _registry(**system_keys: str) -> ProviderRegistry:
    return ProviderRegistry(
        specs={},
        adapters={},
        keys={name: SecretStr(value) for name, value in system_keys.items()},
    )


@pytest.fixture
def session_factory(db_session: AsyncSession) -> tuple[SessionFactory, list[int]]:
    """Hands back the same transactional ``db_session`` every time it is
    called, and counts how many times that was — the way ``_load_once`` opens
    a session for its one query, mirroring ``conftest.py``'s own
    ``_override_session_factory`` for the FastAPI dependency.

    Structurally satisfies ``UserCredentials``'s ``async_sessionmaker[
    AsyncSession]`` parameter (a zero-arg callable returning something usable
    as ``async with ... as session:``) without being one — ``cast`` says so
    honestly, the same duck typing ``conftest.py``'s own override already
    relies on for the FastAPI-injected version of this."""
    calls: list[int] = []

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        calls.append(1)
        yield db_session

    return cast(SessionFactory, factory), calls


async def _add_key(
    db_session: AsyncSession, *, user_id: UUID, provider: str, plaintext: str, **overrides: Any
) -> None:
    fields: dict[str, Any] = {
        "user_id": user_id,
        "provider": provider,
        "encrypted_key": encrypt_provider_key(plaintext),
        "last_4": plaintext[-4:],
        "nickname": None,
        "validation_status": "valid",
        "last_validated_at": None,
    }
    fields.update(overrides)
    await provider_keys_repo.upsert(db_session, **fields)


# --------------------------------------------------------------------------- #
# SystemCredentials
# --------------------------------------------------------------------------- #
async def test_system_credentials_answers_identically_for_every_provider() -> None:
    registry = _registry(gemini="shared-gemini-key", groq="shared-groq-key")
    credentials = SystemCredentials(registry)

    gemini = await credentials.for_provider("gemini")
    groq = await credentials.for_provider("groq")

    assert gemini == ResolvedKey(
        provider="gemini",
        key="shared-gemini-key",
        pool="shared",
        scope=keys.SYSTEM_SCOPE,
        key_id=None,
    )
    assert groq == ResolvedKey(
        provider="groq", key="shared-groq-key", pool="shared", scope=keys.SYSTEM_SCOPE, key_id=None
    )


# --------------------------------------------------------------------------- #
# UserCredentials
# --------------------------------------------------------------------------- #
async def test_no_rows_falls_back_to_the_shared_pool(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key")
    credentials = UserCredentials(user.id, registry, factory)

    resolved = await credentials.for_provider("gemini")

    assert resolved == ResolvedKey(
        provider="gemini",
        key="shared-gemini-key",
        pool="shared",
        scope=keys.SYSTEM_SCOPE,
        key_id=None,
    )


async def test_a_stored_row_resolves_private_under_the_users_own_scope(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    await _add_key(
        db_session, user_id=user.id, provider="gemini", plaintext="user-owned-gemini-key"
    )
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key")
    credentials = UserCredentials(user.id, registry, factory)

    resolved = await credentials.for_provider("gemini")

    assert resolved.pool == "private"
    assert resolved.scope == str(user.id)
    assert resolved.key == "user-owned-gemini-key"
    assert resolved.key_id is not None


async def test_the_mixed_case_one_provider_private_one_shared(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    """The test that matters most in this step (phase6.md's own words): §9.5
    is a single failover chain crossing a provider the user has a key for and
    one where they stay on the shared pool, and one resolver must answer both
    correctly."""
    user = await user_factory()
    await _add_key(
        db_session, user_id=user.id, provider="gemini", plaintext="user-owned-gemini-key"
    )
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key", groq="shared-groq-key")
    credentials = UserCredentials(user.id, registry, factory)

    gemini = await credentials.for_provider("gemini")
    groq = await credentials.for_provider("groq")

    assert gemini.pool == "private"
    assert gemini.scope == str(user.id)
    assert gemini.key == "user-owned-gemini-key"

    assert groq.pool == "shared"
    assert groq.scope == keys.SYSTEM_SCOPE
    assert groq.key == "shared-groq-key"
    assert groq.key_id is None


async def test_two_calls_issue_one_query(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    await _add_key(
        db_session, user_id=user.id, provider="gemini", plaintext="user-owned-gemini-key"
    )
    factory, calls = session_factory
    registry = _registry(gemini="shared-gemini-key", groq="shared-groq-key")
    credentials = UserCredentials(user.id, registry, factory)

    await credentials.for_provider("gemini")
    await credentials.for_provider("groq")

    assert len(calls) == 1


async def test_a_revoked_row_is_invisible(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    await _add_key(
        db_session, user_id=user.id, provider="gemini", plaintext="user-owned-gemini-key"
    )
    await provider_keys_repo.deactivate(db_session, user_id=user.id, provider="gemini")
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key")
    credentials = UserCredentials(user.id, registry, factory)

    resolved = await credentials.for_provider("gemini")

    assert resolved.pool == "shared"
    assert resolved.key == "shared-gemini-key"


async def test_a_row_that_will_not_decrypt_falls_back_and_logs(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CredentialUnreadable`` (an ``ENCRYPTION_KEY`` rotated out from under
    a stored row) must not take the user's gateway down — the private
    candidate falls back to the shared pool, and the failure is logged rather
    than swallowed."""
    user = await user_factory()
    await _add_key(
        db_session,
        user_id=user.id,
        provider="gemini",
        plaintext="irrelevant",
        encrypted_key="not-a-real-fernet-token",
    )
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key")
    credentials = UserCredentials(user.id, registry, factory)

    logged: list[tuple[str, dict[str, Any]]] = []

    class _LogSpy:
        def error(self, event: str, **kwargs: Any) -> None:
            logged.append((event, kwargs))

    monkeypatch.setattr(resolver_module, "logger", _LogSpy())

    resolved = await credentials.for_provider("gemini")

    assert resolved.pool == "shared"
    assert resolved.key == "shared-gemini-key"
    assert len(logged) == 1
    event, fields = logged[0]
    assert event == "keys_resolution.credential_unreadable"
    assert fields["provider"] == "gemini"
    assert "not-a-real-fernet-token" not in str(fields)
