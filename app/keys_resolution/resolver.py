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

**D40's other half.** ``for_provider`` answers "which credential"; a private
credential can still turn out to be *wrong*, discovered only when a real
``AuthFailed`` comes back. :meth:`ProviderCredentials.record_auth_failure` is
that answer's sequel — found while writing Phase 6 Step 8's leakage test,
which drives exactly this scenario and needs a real disclosure write to
assert against. Both call sites (``routing/router.py``, both loops, and
``perception/extractors.py``'s tier 2) call it only when
``resolved.pool == "private"``; :class:`SystemCredentials` never sees a
private resolution, so its implementation is inert by construction rather
than by caller discipline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache import keys
from app.config import LimitsConfig
from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.crypto import CredentialUnreadable, decrypt_provider_key
from app.core.logging import get_logger
from app.db.models import ProviderKey
from app.db.repo import allocations as allocations_repo
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
    shared_daily_cap: int | None = None
    """D39's personal ceiling on the *shared* pool, for this (user, provider,
    model) triple — ``None`` on the private path by construction (§9.4: a
    user's own key has no shared budget to cap) and always ``None`` for
    :class:`SystemCredentials`, which has no user in view to cap. Resolved
    here rather than by the router because fetching it needs a session
    (D38); the router only ever reads the number, never opens one."""
    user_id: UUID | None = None
    """Who is asking, independent of which pool serves the attempt — needed
    to build D39's personal-cap grant, whose Redis key must always name the
    real user even on the shared path, where :attr:`scope` itself reads
    ``keys.SYSTEM_SCOPE``. ``None`` only for :class:`SystemCredentials`."""


class ProviderCredentials(Protocol):
    """How the router and both lanes ask "who pays for this provider".

    One call per candidate attempt, not once per request — §9.5 requires a
    single failover chain to answer this question differently for different
    providers. Takes the candidate's ``model`` too, as of Phase 6 Step 7
    (D39): a user's key and quota *scope* are per provider, but their
    personal daily *cap* is per (provider, model) — the one place this
    module's per-candidate contract needs finer grain than §9.5 itself asked
    for, so the whole call, not just the cap lookup inside it, takes the
    extra argument.
    """

    async def for_provider(self, provider: str, model: str) -> ResolvedKey: ...

    async def record_auth_failure(self, resolved: ResolvedKey) -> None:
        """D40: a candidate resolved from this object just failed with
        ``AuthFailed``. A no-op for anything that never hands out a private
        credential; :class:`UserCredentials` is the one implementation that
        does something with it. The router calls this only when
        ``resolved.pool == "private"``, but the method itself takes no
        position on that — it is cheap to call unconditionally and wrong to
        assume every caller remembers the guard.
        """
        ...


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

    async def for_provider(self, provider: str, model: str) -> ResolvedKey:
        del model  # No user in view, so there is nothing to cap (D39).
        return ResolvedKey(
            provider=provider,
            key=self._registry.system_key(provider),
            pool="shared",
            scope=keys.SYSTEM_SCOPE,
            key_id=None,
        )

    async def record_auth_failure(self, resolved: ResolvedKey) -> None:
        """No-op: the shared pool has no per-user row to flag. D40 applies
        only to a credential BYOK actually stored."""
        return None


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
        limits: LimitsConfig | None = None,
        tier: str = "free",
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._user_id = user_id
        self._registry = registry
        self._session_factory = session_factory
        self._limits = limits
        """D39's tier default for :attr:`ResolvedKey.shared_daily_cap`.
        ``None`` — the default, and what every pre-Step-7 caller and test
        still constructs with — means no tier default is configured for this
        resolver, so a shared-pool resolution reports ``shared_daily_cap=None``
        unless a ``user_quota_allocations`` row overrides it directly. The
        same "``None`` keeps every existing caller honest" shape D36 already
        established for ``credentials`` itself."""
        self._tier = tier
        self._clock = clock
        self._lock = asyncio.Lock()
        self._loaded = False
        self._by_provider: dict[str, ProviderKey] = {}
        self._decrypted: dict[str, str] = {}
        self._caps: dict[tuple[str, str], int] = {}

    async def for_provider(self, provider: str, model: str) -> ResolvedKey:
        await self._load_once()

        row = self._by_provider.get(provider)
        if row is None:
            return self._shared(provider, model)

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
                return self._shared(provider, model)
            self._decrypted[provider] = plaintext

        return ResolvedKey(
            provider=provider,
            key=plaintext,
            pool="private",
            scope=str(self._user_id),
            key_id=row.id,
            # D39: the private path has no cap by construction — nothing is
            # being shared.
            shared_daily_cap=None,
            user_id=self._user_id,
        )

    async def record_auth_failure(self, resolved: ResolvedKey) -> None:
        """D40's disclosure write: flip this row's ``validation_status`` to
        ``'invalid'`` after a live ``AuthFailed``, so the settings UI can say
        *This key was rejected — re-add it* instead of silently laundering
        the traffic through the shared pool.

        Fire-and-forget, in its own session — never the one ``_load_once``
        opened, which is already closed by the time a candidate can fail —
        and never blocking the request that discovered it: the failing
        candidate has already moved on to the next one in the chain (D40's
        whole point) by the time this matters, and a write that failed here
        must not turn a successfully-failed-over turn into a 500. Errors are
        logged with the ``key_id`` alone, never the key.
        """
        if resolved.key_id is None:
            return
        try:
            async with self._session_factory() as session:
                await provider_keys_repo.mark_invalid(session, key_id=resolved.key_id)
                await session.commit()
        except Exception:
            logger.error(
                "keys_resolution.mark_invalid_failed",
                key_id=str(resolved.key_id),
                provider=resolved.provider,
            )

    def _shared(self, provider: str, model: str) -> ResolvedKey:
        return ResolvedKey(
            provider=provider,
            key=self._registry.system_key(provider),
            pool="shared",
            scope=keys.SYSTEM_SCOPE,
            key_id=None,
            shared_daily_cap=self._cap_for(provider, model),
            user_id=self._user_id,
        )

    def _cap_for(self, provider: str, model: str) -> int | None:
        """D39: a ``user_quota_allocations`` override, else this resolver's
        tier default, else no cap at all — resolved from what
        :meth:`_load_once` already batch-loaded, so this is a pure lookup."""
        override = self._caps.get((provider, model))
        if override is not None:
            return override
        tier_limits = self._limits.gateway.get(self._tier) if self._limits is not None else None
        return tier_limits.shared_pool_daily_cap if tier_limits is not None else None

    async def _load_once(self) -> None:
        """One session opened for two ``SELECT``s, for the life of this resolver.

        ``last_used_at`` is touched here, for every provider-key row loaded,
        in the same session as the load (D38) — not only for the provider a
        caller eventually asks about. That trades a slightly imprecise "was
        this credential available this request" for the one-round-trip cost
        the whole design exists to buy; threading a per-provider callback
        through both lanes to record it more precisely would be machinery for
        a column nobody sorts on, exactly as the module's own docstring in
        ``db/repo/provider_keys.py`` argues for the throttle itself.

        Phase 6 Step 7 adds the allocations load beside it, in the same
        session rather than a second one opened lazily per candidate: D39's
        personal cap needs one row per (provider, model) the resolver might
        ever be asked about this request, and a user's override table is
        sparse enough that loading all of it up front costs the same one
        round trip the provider-key load already pays for.
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
                allocations = await allocations_repo.list_for_user(session, self._user_id)
                await session.commit()
            self._by_provider = {row.provider: row for row in rows}
            self._caps = {
                (allocation.provider, allocation.model): allocation.daily_cap
                for allocation in allocations
            }
            self._loaded = True


def quota_scope_for(pool: Literal["shared", "private"] | None, user_id: UUID) -> keys.Scope:
    """``requests.quota_scope`` (D42/Phase 6 Step 7), reconstructed from a
    turn's winning ``key_pool`` rather than threaded as a second field.

    A private-pool attempt is always billed to *this* caller's own key — BYOK
    stores exactly one credential per (user, provider), and a candidate only
    ever resolves ``pool="private"`` off the caller's own ``UserCredentials``
    — so ``str(user_id)`` is exactly :attr:`ResolvedKey.scope` would have been
    for that attempt, without carrying a redundant scope string on
    ``RouterOutcome``/``StreamResult`` alongside the ``key_pool`` label D42
    already added there. ``None`` (every candidate skipped, or a cache hit
    that never routed) reads as the shared pool, which is correct either way:
    nothing private was ever touched.
    """
    return str(user_id) if pool == "private" else keys.SYSTEM_SCOPE
