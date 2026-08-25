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
from app.config import LimitsConfig
from app.core.crypto import encrypt_provider_key
from app.db.models import UserQuotaAllocation
from app.db.repo import provider_keys as provider_keys_repo
from app.keys_resolution import resolver as resolver_module
from app.keys_resolution.resolver import (
    ResolvedKey,
    SystemCredentials,
    UserCredentials,
    quota_scope_for,
)
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


async def _grant(
    db_session: AsyncSession, *, user_id: UUID, provider: str, model: str, daily_cap: int
) -> None:
    db_session.add(
        UserQuotaAllocation(user_id=user_id, provider=provider, model=model, daily_cap=daily_cap)
    )
    await db_session.flush()


def _limits(*, free_cap: int | None) -> LimitsConfig:
    return LimitsConfig.model_validate(
        {
            "version": 1,
            "limits": {},
            "gateway": {"free": {"rpm": 20, "rpd": 500, "shared_pool_daily_cap": free_cap}},
        }
    )


# --------------------------------------------------------------------------- #
# SystemCredentials
# --------------------------------------------------------------------------- #
async def test_system_credentials_answers_identically_for_every_provider() -> None:
    registry = _registry(gemini="shared-gemini-key", groq="shared-groq-key")
    credentials = SystemCredentials(registry)

    gemini = await credentials.for_provider("gemini", "gemini-3.6-flash")
    groq = await credentials.for_provider("groq", "openai/gpt-oss-120b")

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

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

    assert resolved == ResolvedKey(
        provider="gemini",
        key="shared-gemini-key",
        pool="shared",
        scope=keys.SYSTEM_SCOPE,
        key_id=None,
        user_id=user.id,
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

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

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

    gemini = await credentials.for_provider("gemini", "gemini-3.6-flash")
    groq = await credentials.for_provider("groq", "openai/gpt-oss-120b")

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

    await credentials.for_provider("gemini", "gemini-3.6-flash")
    await credentials.for_provider("groq", "openai/gpt-oss-120b")

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

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

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

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

    assert resolved.pool == "shared"
    assert resolved.key == "shared-gemini-key"
    assert len(logged) == 1
    event, fields = logged[0]
    assert event == "keys_resolution.credential_unreadable"
    assert fields["provider"] == "gemini"
    assert "not-a-real-fernet-token" not in str(fields)


# --------------------------------------------------------------------------- #
# D39 — the personal cap on the shared pool, Phase 6 Step 7
# --------------------------------------------------------------------------- #
async def test_the_private_path_never_carries_a_cap(
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
    credentials = UserCredentials(
        user.id, registry, factory, limits=_limits(free_cap=50), tier="free"
    )

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

    assert resolved.pool == "private"
    assert resolved.shared_daily_cap is None


async def test_the_shared_path_reports_the_tier_default_with_no_override(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key")
    credentials = UserCredentials(
        user.id, registry, factory, limits=_limits(free_cap=50), tier="free"
    )

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

    assert resolved.pool == "shared"
    assert resolved.shared_daily_cap == 50
    assert resolved.user_id == user.id


async def test_an_override_row_wins_over_the_tier_default(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    user = await user_factory()
    await _grant(
        db_session, user_id=user.id, provider="gemini", model="gemini-3.6-flash", daily_cap=5
    )
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key")
    credentials = UserCredentials(
        user.id, registry, factory, limits=_limits(free_cap=50), tier="free"
    )

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

    assert resolved.shared_daily_cap == 5


async def test_no_row_and_no_tier_default_is_no_cap_at_all(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    """A resolver built with no ``limits`` at all — every pre-Step-7 caller —
    never reports a cap, the same "``None`` keeps every existing caller
    honest" shape D36 already established for ``credentials`` itself."""
    user = await user_factory()
    factory, _ = session_factory
    registry = _registry(gemini="shared-gemini-key")
    credentials = UserCredentials(user.id, registry, factory)

    resolved = await credentials.for_provider("gemini", "gemini-3.6-flash")

    assert resolved.shared_daily_cap is None


async def test_the_allocations_load_shares_the_provider_keys_query(
    db_session: AsyncSession,
    session_factory: tuple[SessionFactory, list[int]],
    user_factory: Callable[..., Any],
) -> None:
    """D38's one-query promise extends to the cap: loading it must not cost a
    second round trip beyond the provider-key load ``_load_once`` already
    pays for, even across two different providers in one chain."""
    user = await user_factory()
    await _grant(
        db_session, user_id=user.id, provider="gemini", model="gemini-3.6-flash", daily_cap=5
    )
    factory, calls = session_factory
    registry = _registry(gemini="shared-gemini-key", groq="shared-groq-key")
    credentials = UserCredentials(
        user.id, registry, factory, limits=_limits(free_cap=50), tier="free"
    )

    await credentials.for_provider("gemini", "gemini-3.6-flash")
    await credentials.for_provider("groq", "openai/gpt-oss-120b")

    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# D42 — reconstructing `requests.quota_scope` from a turn's `key_pool`
# --------------------------------------------------------------------------- #
def test_quota_scope_for_a_private_pool_is_the_users_own_id() -> None:
    user_id = UUID("11111111-1111-1111-1111-111111111111")
    assert quota_scope_for("private", user_id) == str(user_id)


def test_quota_scope_for_the_shared_pool_and_for_no_pool_is_system() -> None:
    user_id = UUID("11111111-1111-1111-1111-111111111111")
    assert quota_scope_for("shared", user_id) == keys.SYSTEM_SCOPE
    assert quota_scope_for(None, user_id) == keys.SYSTEM_SCOPE
