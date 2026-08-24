"""§9.3's credential-and-scope resolver — Phase 6's centre of gravity (D36).

Two facts the gateway asked separately for five phases — "which credential
answers this candidate" and "whose quota budget pays for it" — are one fact,
answered here, per candidate. §9.5 makes this unavoidable: BYOK is per
*provider*, not per user, and a single failover chain can cross a provider the
caller has their own key for and one where they still ride the shared pool.
:class:`ResolvedKey` is that one answer; :class:`SystemCredentials` and
:class:`UserCredentials` are the two ways of producing it.

**Why per request, not per session (D38, §9.6).** Removing a key must take
effect on the caller's very next message, with no reload. A per-session cache
would serve a revoked key until the session expired; a snapshot taken fresh
each request satisfies "the next message" exactly, because the next message
*is* the next request. The cost that buys is one query — :meth:`UserCredentials
._load_once` loads every active row this user holds in a single
``list_active_for_user`` call, memoized for the life of the resolver, so a
three-provider failover chain still costs one round trip rather than three.

**Why the router gets a session-free object.** D14: a streamed turn's
generator outlives the FastAPI request-scoped session dependency, so
:class:`UserCredentials` takes a ``session_factory`` and opens its own session
inside :meth:`_load_once`, closing it before the first candidate is even
reached — the same shape ``PerceptionResolver`` already uses for the same
reason.

**A row that will not decrypt does not take the user's gateway down.**
``ENCRYPTION_KEY`` rotating between when a row was written and when it is
read is an operational event, not a caller's problem; :meth:`UserCredentials
.for_provider` falls back to the shared pool on ``CredentialUnreadable`` and
logs the ``key_id`` — never the ciphertext — so an operator has a signal
without a user ever seeing a 500.

Threaded through the answer lane's two loops (``routing.route``/``route_stream``)
and ``streaming.stream_completion`` as of Step 5; Step 6 threads it through the
perception lane the same way.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache import keys
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.crypto import CredentialUnreadable, decrypt_provider_key
from app.core.logging import get_logger
from app.db.models import ProviderKey
from app.db.repo import provider_keys as provider_keys_repo
from app.providers.registry import ProviderRegistry

logger = get_logger("app.keys_resolution.resolver")


@dataclass(frozen=True, slots=True)
class ResolvedKey:
    """One candidate's credential and the quota scope it must be spent under.

    Deliberately one object rather than two parameters that have to agree
    (D36) — a request that resolved these separately is a request one bug
    away from spending a user's private key against the system counters, or
    the reverse.
    """

    provider: str
    key: str
    """Plaintext, in memory only, never logged. A plain ``str`` — not
    ``SecretStr`` — because Contract A's ``complete(payload, key: str)`` is
    frozen; wrapping it here buys a false sense of safety and one
    ``.get_secret_value()`` at the single call site that matters. The
    credential-leakage test (Phase 6 Step 8) is what actually keeps this
    field out of a log line."""
    pool: Literal["shared", "private"]
    scope: keys.Scope
    """``keys.SYSTEM_SCOPE`` for the shared pool, or ``str(user_id)`` for a
    private key — quota keys on this, never on ``key_id`` (D7)."""
    key_id: UUID | None
    """The winning ``provider_keys`` row, private pool only. ``None`` on the
    shared path — there is no row to name."""


class ProviderCredentials(Protocol):
    """How the router and both lanes ask "who pays for this provider".

    One call per candidate attempt, not once per request — §9.5 requires a
    single failover chain to answer this question differently for different
    providers.
    """

    async def for_provider(self, provider: str) -> ResolvedKey: ...


class SystemCredentials:
    """The shared-pool-only implementation. Every provider resolves to the
    environment's key at :data:`keys.SYSTEM_SCOPE`.

    The default everywhere a caller passes no resolver of its own — which is
    what lets every pre-Phase-6 test keep passing unchanged. It answers
    identically for every provider, so it carries no per-request state at
    all.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        self._registry = registry

    async def for_provider(self, provider: str) -> ResolvedKey:
        return ResolvedKey(
            provider=provider,
            key=self._registry.system_key(provider),
            pool="shared",
            scope=keys.SYSTEM_SCOPE,
            key_id=None,
        )


class UserCredentials:
    """§9.3, the real resolver: a user's own keys first, the shared pool
    second, memoized per request (D38).

    ``for_provider`` is safe to call concurrently — the router can reach two
    candidates from two coroutines under ``asyncio.gather`` in principle, and
    nothing here assumes otherwise. The load happens at most once, guarded by
    a lock rather than the bare ``_loaded`` flag alone, so two concurrent
    first calls cannot both open a session.
    """

    def __init__(
        self,
        user_id: UUID,
        registry: ProviderRegistry,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._user_id = user_id
        self._registry = registry
        self._session_factory = session_factory
        self._clock = clock
        self._lock = asyncio.Lock()
        self._loaded = False
        self._by_provider: dict[str, ProviderKey] = {}
        self._decrypted: dict[str, str] = {}

    async def for_provider(self, provider: str) -> ResolvedKey:
        await self._load_once()

        row = self._by_provider.get(provider)
        if row is None:
            return self._shared(provider)

        plaintext = self._decrypted.get(provider)
        if plaintext is None:
            try:
                plaintext = decrypt_provider_key(row.encrypted_key)
            except CredentialUnreadable:
                logger.error(
                    "keys_resolution.credential_unreadable",
                    key_id=str(row.id),
                    provider=provider,
                )
                return self._shared(provider)
            self._decrypted[provider] = plaintext

        return ResolvedKey(
            provider=provider,
            key=plaintext,
            pool="private",
            scope=str(self._user_id),
            key_id=row.id,
        )

    def _shared(self, provider: str) -> ResolvedKey:
        return ResolvedKey(
            provider=provider,
            key=self._registry.system_key(provider),
            pool="shared",
            scope=keys.SYSTEM_SCOPE,
            key_id=None,
        )

    async def _load_once(self) -> None:
        """One ``list_active_for_user`` for the life of this resolver.

        ``last_used_at`` is touched here, for every row loaded, in the same
        session as the load (D38) — not only for the provider a caller
        eventually asks about. That trades a slightly imprecise "was this
        credential available this request" for the one-round-trip cost the
        whole design exists to buy; threading a per-provider callback through
        both lanes to record it more precisely would be machinery for a
        column nobody sorts on, exactly as the module's own docstring in
        ``db/repo/provider_keys.py`` argues for the throttle itself.
        """
        async with self._lock:
            if self._loaded:
                return
            async with self._session_factory() as session:
                rows = await provider_keys_repo.list_active_for_user(session, self._user_id)
                for row in rows:
                    await provider_keys_repo.touch_last_used(
                        session, key_id=row.id, clock=self._clock
                    )
                await session.commit()
            self._by_provider = {row.provider: row for row in rows}
            self._loaded = True
