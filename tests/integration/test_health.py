"""``/healthz`` and ``/readyz`` — what an orchestrator reads to decide our fate.

[test_openapi_errors.py](tests/unit/test_openapi_errors.py) already asserts these
two routes publish the right schema and do not claim to need credentials. What it
cannot assert is the behaviour, and the behaviour is the whole design:

**Liveness answers whenever the process is up.** ``/healthz`` must not touch the
database. If it did, a Postgres blip would look like a dead process and the
orchestrator would restart a perfectly healthy app — turning a recoverable
dependency failure into a crash loop.

**Readiness reports the dependency and stays bounded.** An unreachable database
refuses fast; a *hung* one would hold the probe open until the orchestrator's own
timeout, which is the slow-but-not-down case ``READYZ_TIMEOUT_S`` exists for.

**Readiness reports Redis, and is decided by it only when quota enforcement
means the gateway actually depends on it (D15, ADR-018).** With
``QUOTA_ENFORCEMENT=True`` (the default), a candidate that cannot reserve
because Redis is unreachable fails closed, so an instance in that state
refuses every chat request while the breaker's fail-open reasoning (ADR-010)
would otherwise report it healthy — leaving it in rotation would convert a
Redis outage into a fleet of instances that all answer 502 instead of one that
is taken out. With ``QUOTA_ENFORCEMENT=False`` the tracker is never
constructed and ADR-010's original verdict applies unchanged: Redis is
reported, not decided by.

The conftest ``app`` fixture deliberately does not set ``db_engine`` — the
lifespan never runs, so nothing opens a real pool. This module supplies stub
engines instead, which is also the only way to reach the 503 and timeout branches
at all: a working Postgres cannot be asked to fail on demand. Redis gets the same
treatment: the fixture supplies a working ``fakeredis``, and the tests that need a
broken one swap in a stub.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.config import get_settings
from app.core.logging import REQUEST_ID_HEADER
from tests.conftest import assert_envelope

pytestmark = pytest.mark.integration


def _with_quota_enforcement(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Patch ``app.main``'s ``get_settings`` to flip ``QUOTA_ENFORCEMENT``.

    ``Settings`` is frozen (fails loudly at startup is the point), so the
    cached singleton cannot be mutated in place — a copy with the one field
    overridden, handed back through the same accessor ``/readyz`` calls, is
    what lets these tests isolate the timeout mechanics (quota enforcement
    off) from the D15 fail-closed decision (quota enforcement on) without
    reaching into Redis or Postgres for either.
    """
    patched = get_settings().model_copy(update={"QUOTA_ENFORCEMENT": enabled})
    monkeypatch.setattr("app.main.get_settings", lambda: patched)


class _StubEngine:
    """Stands in for an ``AsyncEngine`` at the one method ``/readyz`` calls."""

    def __init__(self, *, failure: Exception | None = None, stall_s: float | None = None) -> None:
        self._failure = failure
        self._stall_s = stall_s
        self.connections = 0

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        self.connections += 1
        if self._failure is not None:
            raise self._failure
        if self._stall_s is not None:
            await asyncio.sleep(self._stall_s)
        yield _StubConnection()


class _StubConnection:
    async def execute(self, statement: object) -> None:
        return None


class _StubRedis:
    """Stands in for the client at the one method ``/readyz`` calls.

    The conftest fixture already supplies a working ``fakeredis``; this exists
    only to produce the two states a working server cannot be asked for.
    """

    def __init__(self, *, failure: Exception | None = None, stall_s: float | None = None) -> None:
        self._failure = failure
        self._stall_s = stall_s
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        if self._failure is not None:
            raise self._failure
        if self._stall_s is not None:
            await asyncio.sleep(self._stall_s)
        return True


@pytest.fixture
def engine(app: FastAPI) -> Iterator[_StubEngine]:
    """A reachable database by default; individual tests swap in a broken one."""
    stub = _StubEngine()
    app.state.db_engine = stub
    yield stub


# --------------------------------------------------------------------------- #
# /healthz
# --------------------------------------------------------------------------- #
async def test_liveness_answers_without_credentials(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_liveness_does_not_depend_on_the_database(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """A dead database must not read as a dead process — that is a restart loop."""
    app.state.db_engine = _StubEngine(failure=OSError("connection refused"))

    response = await client.get("/healthz")

    assert response.status_code == 200


async def test_liveness_echoes_the_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={REQUEST_ID_HEADER: "probe-1"})

    assert response.headers[REQUEST_ID_HEADER] == "probe-1"


# --------------------------------------------------------------------------- #
# /readyz
# --------------------------------------------------------------------------- #
async def test_readiness_reports_every_dependency_it_checked(
    client: httpx.AsyncClient, engine: _StubEngine
) -> None:
    """Both legs named in the body. An operator reading this should not have to
    know which dependencies the probe happens to cover this phase."""
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}
    assert engine.connections == 1


async def test_an_unreachable_database_is_a_503_in_the_envelope(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    app.state.db_engine = _StubEngine(failure=OSError("connection refused"))

    response = await client.get("/readyz")

    assert response.status_code == 503
    error = assert_envelope(response.json())
    assert error["code"] == "database_unavailable"
    assert error["request_id"]


async def test_the_failure_says_nothing_about_the_database(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """The exception is logged, never returned. A readiness probe is reachable by
    anyone, and a DSN or a hostname in its body is a free map of the deployment."""
    app.state.db_engine = _StubEngine(
        failure=OSError(
            "could not connect to server at 10.0.3.7 port 5432: password authentication failed"
        )
    )

    response = await client.get("/readyz")
    body = response.text

    assert response.status_code == 503
    assert "10.0.3.7" not in body
    assert "password" not in body
    assert "5432" not in body


async def test_a_hung_database_is_refused_rather_than_waited_on(
    client: httpx.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slow-but-not-down case. Without the bound, the probe stalls until the
    orchestrator's own timeout and the instance sits in limbo the whole time."""
    monkeypatch.setattr("app.main.READYZ_TIMEOUT_S", 0.05)
    app.state.db_engine = _StubEngine(stall_s=5.0)

    async with asyncio.timeout(3.0):
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert assert_envelope(response.json())["code"] == "database_unavailable"


async def test_readiness_needs_no_credentials_either(
    client: httpx.AsyncClient, engine: _StubEngine
) -> None:
    """It is a probe, not an endpoint. Requiring auth here would mean the
    orchestrator has to hold a credential to ask whether the app is up."""
    response = await client.get("/readyz")

    assert response.status_code == 200


async def test_readiness_echoes_the_request_id(
    client: httpx.AsyncClient, engine: _StubEngine
) -> None:
    response = await client.get("/readyz", headers={REQUEST_ID_HEADER: "probe-2"})

    assert response.headers[REQUEST_ID_HEADER] == "probe-2"


# --------------------------------------------------------------------------- #
# /readyz — the Redis leg, quota enforcement off (ADR-010's original verdict)
# --------------------------------------------------------------------------- #
async def test_an_unreachable_redis_is_reported_and_still_ready_when_enforcement_is_off(
    client: httpx.AsyncClient,
    app: FastAPI,
    engine: _StubEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``QUOTA_ENFORCEMENT=False`` the tracker is never constructed, so a
    dead Redis costs the breaker its memory and nothing else — the instance can
    serve, and /readyz gates both traffic and rollouts, so failing here would
    turn an Upstash blip into an outage and a blocked deploy."""
    _with_quota_enforcement(monkeypatch, enabled=False)
    app.state.redis = _StubRedis(failure=OSError("connection refused"))

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "unavailable"}


async def test_a_hung_redis_neither_stalls_nor_fails_the_probe_when_enforcement_is_off(
    client: httpx.AsyncClient, app: FastAPI, engine: _StubEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fail-open dependency must not be able to fail the probe by starvation
    either — which is why its bound is separate from, and shorter than, the
    database's rather than sharing that block."""
    _with_quota_enforcement(monkeypatch, enabled=False)
    monkeypatch.setattr("app.main.READYZ_REDIS_TIMEOUT_S", 0.05)
    app.state.redis = _StubRedis(stall_s=5.0)

    async with asyncio.timeout(3.0):
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["redis"] == "unavailable"


async def test_a_stalled_redis_does_not_spend_the_databases_budget(
    client: httpx.AsyncClient, app: FastAPI, engine: _StubEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The starvation case stated directly: with both checks under one timeout, a
    hung Redis would consume the database's budget and the probe would report the
    *database* as unreachable — pointing the investigation at the wrong service.
    Quota enforcement's own verdict on a hung Redis is covered separately below;
    this test isolates the timeout mechanics from it."""
    _with_quota_enforcement(monkeypatch, enabled=False)
    monkeypatch.setattr("app.main.READYZ_TIMEOUT_S", 0.5)
    monkeypatch.setattr("app.main.READYZ_REDIS_TIMEOUT_S", 0.05)
    app.state.redis = _StubRedis(stall_s=5.0)

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"


# --------------------------------------------------------------------------- #
# /readyz — the Redis leg, quota enforcement on (D15, ADR-018)
# --------------------------------------------------------------------------- #
async def test_an_unreachable_redis_fails_closed_when_quota_enforcement_is_on(
    client: httpx.AsyncClient, app: FastAPI, engine: _StubEngine
) -> None:
    """The load-bearing consequence of D15. ``QUOTA_ENFORCEMENT`` defaults to
    ``True``, so with no monkeypatching at all a dead Redis must fail the probe:
    the tracker fails every reservation closed, and an instance that will refuse
    every chat request is not ready merely because it can still answer /healthz."""
    app.state.redis = _StubRedis(failure=OSError("connection refused"))

    response = await client.get("/readyz")

    assert response.status_code == 503
    error = assert_envelope(response.json())
    assert error["code"] == "redis_unavailable"
    assert error["request_id"]


async def test_a_hung_redis_fails_closed_when_quota_enforcement_is_on(
    client: httpx.AsyncClient, app: FastAPI, engine: _StubEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed verdict applies to a Redis that never answers, not only
    one that refuses the connection outright — ``probe()`` treats both as
    unreachable, so both paths must reach the same 503."""
    monkeypatch.setattr("app.main.READYZ_REDIS_TIMEOUT_S", 0.05)
    app.state.redis = _StubRedis(stall_s=5.0)

    async with asyncio.timeout(3.0):
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert assert_envelope(response.json())["code"] == "redis_unavailable"


async def test_a_healthy_redis_still_reads_ready_with_enforcement_on(
    client: httpx.AsyncClient, engine: _StubEngine
) -> None:
    """The common case, asserted explicitly: enforcement being on must not make
    the probe pessimistic about a Redis that is actually reachable."""
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def test_an_unreachable_database_is_still_a_503_whatever_redis_says(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """The asymmetry, from the other side. Redis cannot fail the probe; Postgres
    always can, and a healthy Redis does not soften that."""
    app.state.db_engine = _StubEngine(failure=OSError("connection refused"))

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert assert_envelope(response.json())["code"] == "database_unavailable"


async def test_redis_is_not_probed_when_the_database_already_failed(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Not an optimization — an ordering guarantee. The 503 branch returns before
    the Redis leg, so a probe that is already failing cannot also block on a
    second unreachable dependency."""
    app.state.db_engine = _StubEngine(failure=OSError("connection refused"))
    redis = _StubRedis()
    app.state.redis = redis

    await client.get("/readyz")

    assert redis.pings == 0


async def test_liveness_does_not_depend_on_redis_either(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Same reasoning as the database: liveness answering false gets the process
    restarted, and restarting a healthy app cannot fix someone else's Redis."""
    app.state.redis = _StubRedis(failure=OSError("connection refused"))

    response = await client.get("/healthz")

    assert response.status_code == 200
