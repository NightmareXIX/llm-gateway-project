"""Shared test fixtures.

Three things are set up here, and Steps 5, 8 and 10 all build on them.

**A real Postgres.** The schema uses JSONB, a partial unique index,
``gen_random_uuid()`` and ``timestamptz``. SQLite would need a second, divergent
schema definition, and the bugs worth catching — an ``ON CONFLICT`` that does not
fire, a CHECK constraint that does — are exactly the ones it could not catch. The
test database is created once per session, migrated with Alembic (so the
migrations themselves are under test), and dropped at the end.

**Transactional isolation.** Each test runs inside a transaction that is rolled
back afterwards. Application code calls ``session.commit()`` freely; the session
is bound to a connection in ``create_savepoint`` mode, so those commits release a
savepoint rather than escaping the outer transaction.

**No network, ever.** Supabase's JWKS is served by an ``httpx.MockTransport``
whose call count is assertable, tokens are signed with a keypair minted in the
test process, and Redis is ``fakeredis`` in-process. Nothing here can reach the
internet, and CI runs no Redis container.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

# Settings are read lazily through get_settings(), so setting these before the
# first call is enough to make every test run against known configuration —
# environment variables take precedence over a developer's local .env.
os.environ.setdefault("ENV", "test")
os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
os.environ.setdefault("SUPABASE_JWT_AUDIENCE", "authenticated")
os.environ.setdefault("REQUIRE_VERIFIED_EMAIL", "true")
os.environ.setdefault("GROQ_API_KEY", "gsk_test_key_not_real")
os.environ.setdefault("GEMINI_API_KEY", "gemini_test_key_not_real")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-or-v1-test-key-not-real")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1mZXJuZXQta2V5LW5vdC1hLXJlYWwtb25lLTAwMD0=")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://gateway:gateway@127.0.0.1:5432/gateway")

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from jose import jwt
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.jwt import JwksCache
from app.cache.client import LuaScriptRegistry
from app.config import REPO_ROOT, Settings, get_settings
from app.core.clock import FixedClock
from app.db.models import ApiKey, Conversation, User
from app.deps import get_session
from app.main import create_app
from app.providers.registry import build_registry
from app.usage.metrics import LatencyTable

TEST_DATABASE_NAME = "gateway_test"
TEST_KID = "test-signing-key-1"


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #
def assert_envelope(payload: object) -> dict[str, object]:
    """Assert the error envelope's shape and return its ``error`` object.

    Exactly these four fields, no more: an extra key is a leak, a missing one
    breaks a client. Lives here rather than in a test module because both the
    synthetic-app unit suite and the real-app integration tests check it.
    """
    assert isinstance(payload, dict)
    error = payload["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "request_id", "details"}
    return error


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def _swap_database_name(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def _asyncpg_dsn(url: str) -> str:
    """Strip SQLAlchemy's ``+asyncpg`` so asyncpg itself can parse the DSN."""
    parts = urlsplit(url)
    scheme = parts.scheme.split("+", 1)[0]
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


async def _recreate_database(admin_dsn: str, name: str) -> None:
    """Drop and recreate the test database from a maintenance connection.

    Dropped *before* creating rather than only after: a run killed with Ctrl-C
    leaves the database behind, and the next run should not inherit its rows.
    """
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            name,
        )
        await connection.execute(f'drop database if exists "{name}"')
        await connection.execute(f'create database "{name}"')
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Create and migrate a dedicated test database; drop it afterwards.

    Synchronous on purpose. Alembic's ``env.py`` calls ``asyncio.run`` itself, so
    it cannot be invoked from inside a running event loop — a sync fixture runs
    between loops and sidesteps that entirely.
    """
    base_url = os.environ["DATABASE_URL"]
    url = _swap_database_name(base_url, TEST_DATABASE_NAME)
    admin_dsn = _asyncpg_dsn(_swap_database_name(base_url, "postgres"))

    asyncio.run(_recreate_database(admin_dsn, TEST_DATABASE_NAME))

    # Point the whole process at the test database, then rebuild the cached
    # Settings so Alembic and the app both see it.
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()

    alembic_config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(alembic_config, "head")

    yield url

    os.environ["DATABASE_URL"] = base_url
    get_settings.cache_clear()
    asyncio.run(_drop_database(admin_dsn, TEST_DATABASE_NAME))


async def _drop_database(admin_dsn: str, name: str) -> None:
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            name,
        )
        await connection.execute(f'drop database if exists "{name}"')
    finally:
        await connection.close()


@pytest.fixture
async def db_session(test_database_url: str) -> AsyncIterator[AsyncSession]:
    """A session whose writes are discarded when the test ends.

    The engine is per-test rather than per-session because each test gets its own
    event loop, and an asyncpg connection pooled on one loop cannot be reused on
    another.

    ``join_transaction_mode="create_savepoint"`` is what lets application code
    call ``commit()`` — the commit lands on a savepoint inside the outer
    transaction, which is then rolled back wholesale.
    """
    engine = create_async_engine(test_database_url, poolclass=None)
    connection = await engine.connect()
    transaction = await connection.begin()

    factory = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


# --------------------------------------------------------------------------- #
# Token signing
# --------------------------------------------------------------------------- #
def _b64url_uint(value: int, byte_length: int) -> str:
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class SigningKey:
    """An EC P-256 keypair plus the JWKS document that publishes its public half."""

    def __init__(self, kid: str = TEST_KID) -> None:
        self.kid = kid
        self._private = ec.generate_private_key(ec.SECP256R1())
        self.private_pem = self._private.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        ).decode("ascii")

    def public_jwk(self) -> dict[str, str]:
        numbers = self._private.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "alg": "ES256",
            "use": "sig",
            "kid": self.kid,
            "x": _b64url_uint(numbers.x, 32),
            "y": _b64url_uint(numbers.y, 32),
        }

    def jwks_document(self) -> dict[str, list[dict[str, str]]]:
        return {"keys": [self.public_jwk()]}

    def sign(self, claims: dict[str, Any], *, kid: str | None = None) -> str:
        token: str = jwt.encode(
            claims,
            self.private_pem,
            algorithm="ES256",
            headers={"kid": kid or self.kid},
        )
        return token


@pytest.fixture(scope="session")
def signing_key() -> SigningKey:
    """One keypair for the whole session — generating EC keys is not free."""
    return SigningKey()


TokenFactory = Callable[..., str]


@pytest.fixture
def make_jwt(signing_key: SigningKey, settings: Settings) -> TokenFactory:
    """Build a token that passes verification, with any claim overridden.

    Defaults mirror a real Supabase session: ``email_verified`` under
    ``user_metadata``, ``aud`` of ``authenticated``, and the project's auth URL
    as the issuer.
    """

    def factory(
        *,
        sub: UUID | str | None = None,
        email: str = "user@example.com",
        email_verified: bool | None = True,
        expires_in_s: int = 3600,
        kid: str | None = None,
        drop: tuple[str, ...] = (),
        **overrides: Any,
    ) -> str:
        now = datetime.now(UTC)
        user_metadata: dict[str, Any] = {}
        if email_verified is not None:
            user_metadata["email_verified"] = email_verified

        claims: dict[str, Any] = {
            "sub": str(sub or uuid4()),
            "email": email,
            "aud": settings.SUPABASE_JWT_AUDIENCE,
            "iss": settings.supabase_auth_url,
            "role": "authenticated",
            "is_anonymous": False,
            "user_metadata": user_metadata,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=expires_in_s)).timestamp()),
        }
        claims.update(overrides)
        for claim in drop:
            claims.pop(claim, None)

        return signing_key.sign(claims, kid=kid)

    return factory


# --------------------------------------------------------------------------- #
# JWKS over a mock transport
# --------------------------------------------------------------------------- #
class JwksServer:
    """Serves the test JWKS and counts how many times it was asked.

    The counter is the point: "an unknown ``kid`` triggers exactly one refresh,
    and a second one inside the throttle window triggers none" is a claim about
    outbound requests, and this is what makes it assertable.
    """

    def __init__(self, signing_key: SigningKey) -> None:
        self.signing_key = signing_key
        self.calls = 0
        self.status_code = 200
        self.document: dict[str, Any] = signing_key.jwks_document()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"error": "unavailable"})
        return httpx.Response(200, json=self.document)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def jwks_server(signing_key: SigningKey) -> JwksServer:
    return JwksServer(signing_key)


@pytest.fixture
def frozen_clock() -> FixedClock:
    """A clock a test can advance, for TTL and throttle behaviour."""
    return FixedClock(datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
async def jwks_cache(
    jwks_server: JwksServer, settings: Settings, frozen_clock: FixedClock
) -> AsyncIterator[JwksCache]:
    client = jwks_server.client()
    try:
        yield JwksCache(
            jwks_url=settings.supabase_jwks_url,
            client=client,
            clock=frozen_clock,
        )
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #
@pytest.fixture
async def redis_client() -> AsyncIterator[FakeRedis]:
    """An in-process Redis, per test.

    ``fakeredis`` rather than a container: CI runs no Redis service, and the
    things worth testing here — a breaker's state machine, a Lua script's
    atomicity — are behaviour of the *commands*, which fakeredis implements
    faithfully. ``decode_responses=True`` mirrors ``create_redis_client``; without
    it a test would compare ``b"open"`` against ``"open"`` and pass for the wrong
    reason once and fail for the right one later.
    """
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# The application
# --------------------------------------------------------------------------- #
@pytest.fixture
async def app(
    db_session: AsyncSession,
    jwks_server: JwksServer,
    frozen_clock: FixedClock,
    redis_client: FakeRedis,
) -> AsyncIterator[FastAPI]:
    """The real app, with the lifespan's resources supplied by hand.

    ``httpx.ASGITransport`` does not run the lifespan, and we would not want it
    to — it would open a real engine and a real HTTP client. Everything the
    lifespan normally builds is injected here instead, which is precisely what
    makes the JWKS mockable.
    """
    application = create_app()

    client = jwks_server.client()
    application.state.http_client = client
    application.state.jwks_cache = JwksCache(
        jwks_url=get_settings().supabase_jwks_url,
        client=client,
        clock=frozen_clock,
    )
    # Built over the same mock-transport client, so an adapter reached through
    # the app cannot escape to the network either.
    application.state.provider_registry = build_registry(client=client)

    # The lifespan's Redis leg, supplied by hand like the rest. Every route that
    # touches Redis reads it from here, so an integration test gets real Redis
    # semantics without a server and without reaching the network.
    application.state.redis = redis_client
    scripts = LuaScriptRegistry(redis_client)
    scripts.load_dir()
    application.state.lua_scripts = scripts

    # A fresh latency table per test. The lifespan's is per *process* on purpose
    # (ADR-014), and sharing one across tests would let a warmed-up ranking in one
    # reorder the candidate chain in another — an ordering bug that appears only
    # when the suite runs in a particular order.
    application.state.latency = LatencyTable()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _override_session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()
        await client.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client speaking to the app in-process.

    ``raise_app_exceptions=False`` so a deliberately-triggered bug returns the
    500 envelope to be asserted on, rather than propagating into the test.
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


# --------------------------------------------------------------------------- #
# Row factories
# --------------------------------------------------------------------------- #
@pytest.fixture
async def user_factory(db_session: AsyncSession) -> Callable[..., Any]:
    """Create a ``users`` row directly, bypassing the auth path."""

    async def factory(
        *,
        email: str | None = None,
        tier: str = "free",
        email_verified: bool = True,
    ) -> User:
        user = User(
            id=uuid4(),
            email=email or f"user-{uuid4().hex[:8]}@example.com",
            tier=tier,
            email_verified=email_verified,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return factory


@pytest.fixture
async def conversation_factory(db_session: AsyncSession) -> Callable[..., Any]:
    """Create a ``conversations`` row directly, bypassing the repo under test."""

    async def factory(
        *,
        user: User,
        title: str | None = None,
        preferred_slot: str = "auto",
    ) -> Conversation:
        conversation = Conversation(
            id=uuid4(),
            user_id=user.id,
            title=title,
            preferred_slot=preferred_slot,
        )
        db_session.add(conversation)
        await db_session.flush()
        return conversation

    return factory


@pytest.fixture
async def api_key_factory(db_session: AsyncSession) -> Callable[..., Any]:
    """Create an ``api_keys`` row and hand back the plaintext alongside it."""
    from app.auth.api_keys import generate_api_key

    async def factory(*, user: User, is_active: bool = True) -> tuple[str, ApiKey]:
        generated = generate_api_key()
        api_key = ApiKey(
            id=uuid4(),
            user_id=user.id,
            key_hash=generated.key_hash,
            key_prefix=generated.key_prefix,
            last_4=generated.last_4,
            is_active=is_active,
        )
        db_session.add(api_key)
        await db_session.flush()
        return generated.plaintext, api_key

    return factory
