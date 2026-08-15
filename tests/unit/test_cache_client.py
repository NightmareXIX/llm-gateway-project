"""The Redis pool, the readiness probe, and Lua script loading.

Three claims, each of which fails silently if it is wrong.

**The probe answers, it does not raise.** It is the only place in the codebase
that turns a Redis failure into a boolean, and everything about ``/readyz``
staying 200 through a Redis outage (ADR-010) rests on it. A probe that let an
exception escape would take every instance out of rotation for a dependency they
can serve without.

**The log line carries no password.** ``REDIS_URL`` is a credential in every
deployment that is not a laptop, and a startup log is the least private place in
the system.

**A script survives the server forgetting it.** ``EVALSHA`` answers ``NOSCRIPT``
after a restart, a ``SCRIPT FLUSH``, or a failover to a replica that never saw
the script — none of which are exotic. Phase 3's ``reserve.lua`` runs on every
request that touches quota, so "works until the first Redis restart" would be
found in production and nowhere else.

``fakeredis`` throughout, with the ``[lua]`` extra doing the script execution. CI
runs no Redis container and never should.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.cache.client import (
    CONNECT_TIMEOUT_S,
    HEALTH_CHECK_INTERVAL_S,
    SOCKET_TIMEOUT_S,
    LuaScriptRegistry,
    create_redis_client,
    probe,
    redacted_target,
)
from app.config import Settings

INCREMENT_SCRIPT = """
local current = redis.call('INCRBY', KEYS[1], ARGV[1])
return current
"""


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
async def test_the_client_decodes_responses(settings: Settings) -> None:
    """Every value this system stores is text. Without decoding at the edge, one
    forgotten ``.decode()`` produces ``b"open" != "open"`` — a comparison that is
    false in a way that reads as true, which is how a breaker never opens."""
    client = create_redis_client(settings)
    try:
        assert client.connection_pool.connection_kwargs["decode_responses"] is True
    finally:
        await client.aclose()


async def test_the_client_is_bounded_on_both_connect_and_command(settings: Settings) -> None:
    """A Redis command here is O(1) work on a handful of small keys. Unbounded, a
    server that accepts the connection and then stops answering hangs the request
    that was only asking whether a breaker is open."""
    client = create_redis_client(settings)
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_connect_timeout"] == CONNECT_TIMEOUT_S
        assert kwargs["socket_timeout"] == SOCKET_TIMEOUT_S
        assert kwargs["health_check_interval"] == HEALTH_CHECK_INTERVAL_S
    finally:
        await client.aclose()


async def test_creating_the_client_opens_no_connection(settings: Settings) -> None:
    """Lazy like the SQLAlchemy engine. Pointed at a port nothing is listening on,
    construction still succeeds — which is what stops a slow or unreachable
    Upstash from keeping the process from booting and serving /healthz. /readyz
    is what reports it instead."""
    unreachable = settings.model_copy(update={"REDIS_URL": "redis://127.0.0.1:1/0"})

    client = create_redis_client(unreachable)  # must not raise

    try:
        assert await probe(client, timeout_s=1.0) is False
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# redacted_target
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://localhost:6379/0", "localhost:6379/0"),
        ("redis://redis:6379/0", "redis:6379/0"),
        ("rediss://default:hunter2@eu1.upstash.io:6379", "eu1.upstash.io:6379"),
        ("redis://:pw@127.0.0.1:6379/1", "127.0.0.1:6379/1"),
    ],
)
def test_the_target_names_the_server_without_naming_the_password(url: str, expected: str) -> None:
    assert redacted_target(url) == expected


def test_no_credential_survives_redaction() -> None:
    """The assertion that actually matters — the one above pins the format, this
    one pins the guarantee."""
    redacted = redacted_target("rediss://default:s3cr3t-token@eu1.upstash.io:6379")

    assert "s3cr3t-token" not in redacted
    assert "default" not in redacted


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
async def test_a_reachable_redis_probes_true(redis_client: FakeRedis) -> None:
    assert await probe(redis_client, timeout_s=1.0) is True


async def test_an_unreachable_redis_probes_false_instead_of_raising() -> None:
    """The whole point. If this raised, /readyz would 503 and Fly would pull a
    healthy instance out of rotation over a fail-open dependency."""

    class _RefusingRedis:
        async def ping(self) -> bool:
            raise RedisConnectionError("connection refused")

    assert await probe(_RefusingRedis(), timeout_s=1.0) is False  # type: ignore[arg-type]


async def test_a_hung_redis_probes_false_within_the_bound() -> None:
    """Slow-but-not-down, the failure mode this project cares about everywhere.
    Without the bound the probe waits on a server that will never answer."""

    class _StallingRedis:
        async def ping(self) -> bool:
            await asyncio.sleep(30)
            return True

    async with asyncio.timeout(1.0):
        assert await probe(_StallingRedis(), timeout_s=0.05) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# LuaScriptRegistry
# --------------------------------------------------------------------------- #
@pytest.fixture
def script_dir(tmp_path: Path) -> Path:
    """A stand-in for ``app/quota/scripts/``, which ships empty in Phase 2.

    Writing the file here rather than committing a real one keeps the loader
    under test without shipping a script that exists only to be exercised — the
    same rule the adapters follow for branches no caller can reach yet.
    """
    (tmp_path / "increment.lua").write_text(INCREMENT_SCRIPT, encoding="utf-8")
    return tmp_path


def test_scripts_are_discovered_and_named_by_filename(
    redis_client: FakeRedis, script_dir: Path
) -> None:
    registry = LuaScriptRegistry(redis_client)
    registry.load_dir(script_dir)

    assert len(registry) == 1
    assert "increment" in registry


def test_a_missing_directory_is_not_an_error(redis_client: FakeRedis, tmp_path: Path) -> None:
    """``app/quota/scripts/`` is empty until Phase 3 and may not exist in a
    stripped image. Refusing to boot over an absent optimization is worse than
    the optimization being absent."""
    registry = LuaScriptRegistry(redis_client)
    registry.load_dir(tmp_path / "nowhere")

    assert len(registry) == 0


async def test_warming_an_empty_registry_does_nothing(redis_client: FakeRedis) -> None:
    """Phase 2's actual production case: the registry is empty and startup must
    not care."""
    registry = LuaScriptRegistry(redis_client)

    await registry.warm()  # must not raise

    assert len(registry) == 0


async def test_a_warmed_script_runs(redis_client: FakeRedis, script_dir: Path) -> None:
    registry = LuaScriptRegistry(redis_client)
    registry.load_dir(script_dir)
    await registry.warm()

    result = await registry["increment"](keys=["counter"], args=[5], client=redis_client)

    assert int(result) == 5


async def test_a_script_survives_the_server_forgetting_it(
    redis_client: FakeRedis, script_dir: Path
) -> None:
    """The NOSCRIPT path. A Redis restart, a SCRIPT FLUSH, or a failover to a
    replica that never saw the script all land here, and the recovery — resend the
    source, retry — has to be automatic or Phase 3's quota tracker stops
    reserving the moment Redis bounces."""
    registry = LuaScriptRegistry(redis_client)
    registry.load_dir(script_dir)
    await registry.warm()
    await registry["increment"](keys=["counter"], args=[1], client=redis_client)

    await redis_client.script_flush()

    result = await registry["increment"](keys=["counter"], args=[1], client=redis_client)
    assert int(result) == 2


async def test_warming_a_dead_redis_is_logged_not_fatal(script_dir: Path) -> None:
    """Redis is fail-open (ADR-010) and every script loads on demand through the
    NOSCRIPT path anyway. Warming is an optimization; refusing to boot over one is
    how a cache outage becomes a deploy outage."""

    class _RefusingRedis:
        def register_script(self, script: str) -> Any:
            return object()

        async def script_load(self, script: str) -> str:
            raise RedisConnectionError("connection refused")

    registry = LuaScriptRegistry(_RefusingRedis())  # type: ignore[arg-type]
    registry.load_dir(script_dir)

    await registry.warm()  # must not raise


def test_an_unregistered_script_raises(redis_client: FakeRedis) -> None:
    """A typo in a script name is a bug. Degrading around it would mean a quota
    reservation that silently never happens."""
    registry = LuaScriptRegistry(redis_client)

    with pytest.raises(KeyError):
        registry["reserve"]
