"""``GET /metrics`` end to end (Phase 7 Step 4, D49).

Three claims worth proving through the real app rather than against
``render_exposition`` directly, because all three live in the route and not in
the renderer:

* **The access rules.** Disabled is a 404 and not a 403 — an endpoint that is
  switched off should not advertise that it exists — and a token, once set, is
  required rather than merely accepted.
* **The counters are wired to real traffic.** ``usage/logger.py``'s facades are
  the one funnel every terminal outcome passes through; that is the whole
  argument for putting the increment there, and it is only true if a real chat
  turn actually moves the number.
* **Redis down still serves.** The gauges are read live at scrape time, so they
  are the half that can fail. A metrics endpoint that 500s during an incident is
  useless exactly when it is needed.

The exposition format itself — cumulative buckets, escaping, the UUID rule — is
``tests/unit/test_metrics.py``'s job, where the unit under test is a string.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.config import get_settings
from app.providers.registry import build_registry
from app.usage.metrics import (
    BREAKER_STATE,
    DURATION_MS,
    QUOTA_REMAINING,
    REQUESTS_TOTAL,
    MetricsRegistry,
)
from tests import provider_fixtures
from tests.conftest import TokenFactory, assert_envelope

pytestmark = pytest.mark.integration

METRICS = "/metrics"
TOKEN = "scrape-me"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Hand ``app.main`` a copy of ``Settings`` with one or two fields moved.

    ``Settings`` is frozen — failing loudly at startup is the point — so the
    cached singleton cannot be mutated in place. Same shape
    ``test_health.py::_with_quota_enforcement`` already uses, and for the same
    reason: the route reads ``get_settings()`` per request, so patching the
    accessor is what lets a test flip a switch the app was not built with.
    """
    patched = get_settings().model_copy(update=overrides)
    monkeypatch.setattr("app.main.get_settings", lambda: patched)


@pytest.fixture
async def groq_streaming(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The streaming sibling: every request gets the recorded ``stream_success``
    SSE body, so a streamed turn can move the histogram's ``stream`` series."""
    upstream = provider_fixtures.client_streaming(
        [provider_fixtures.read_sse("groq", "stream_success").encode("utf-8")]
    )
    app.state.provider_registry = build_registry(client=upstream)
    try:
        yield upstream
    finally:
        await upstream.aclose()


@pytest.fixture
async def groq_only(app: FastAPI) -> AsyncIterator[provider_fixtures.RecordingHandler]:
    """One scripted upstream, so a chat turn can move a counter without the
    network — the same registry swap every other endpoint suite makes."""
    handler = provider_fixtures.RecordingHandler(provider_fixtures.load("groq", "success"))
    client = handler.client()
    app.state.provider_registry = build_registry(client=client)
    try:
        yield handler
    finally:
        await client.aclose()


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #
async def test_metrics_needs_no_session(client: httpx.AsyncClient) -> None:
    """A scrape is not a user. Prometheus holds no Supabase session and never
    will, which is exactly why ``METRICS_TOKEN`` exists as a separate answer."""
    response = await client.get(METRICS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in response.headers["content-type"]


async def test_disabled_metrics_are_a_404_not_a_403(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint that is switched off should not advertise that it exists —
    and a scraper reading 403 keeps retrying a thing that is never coming
    back."""
    _patch_settings(monkeypatch, METRICS_ENABLED=False)

    response = await client.get(METRICS)

    assert response.status_code == 404
    assert assert_envelope(response.json())["code"] == "not_found"


async def test_a_configured_token_is_required(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_settings(monkeypatch, METRICS_TOKEN=SecretStr(TOKEN))

    assert (await client.get(METRICS)).status_code == 401
    assert (await client.get(METRICS, headers=_bearer("wrong"))).status_code == 401
    assert (await client.get(METRICS, headers={"Authorization": TOKEN})).status_code == 401

    ok = await client.get(METRICS, headers=_bearer(TOKEN))
    assert ok.status_code == 200


async def test_a_wrong_token_says_nothing_about_the_right_one(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_settings(monkeypatch, METRICS_TOKEN=SecretStr(TOKEN))

    response = await client.get(METRICS, headers=_bearer("wrong"))

    assert assert_envelope(response.json())["code"] == "unauthorized"
    assert TOKEN not in response.text


# --------------------------------------------------------------------------- #
# The counters, against real traffic
# --------------------------------------------------------------------------- #
async def test_a_served_turn_moves_the_request_counter(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_only: provider_fixtures.RecordingHandler,
) -> None:
    """The one claim that justifies the increment living in ``usage/logger.py``:
    the facades really are the funnel every terminal outcome passes through."""
    before = await client.get(METRICS)
    assert f'{REQUESTS_TOTAL}{{provider="groq"' not in before.text

    turn = await client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers=_headers(make_jwt),
    )
    assert turn.status_code == 200

    after = await client.get(METRICS)

    assert 'provider="groq"' in after.text
    assert 'status="ok"' in after.text
    # A shared-pool turn, so the pool the counter names is the shared one — the
    # same D42 fact the response body discloses.
    assert 'key_pool="shared"' in after.text
    # And it was timed, under the mode it actually ran in.
    assert f'{DURATION_MS}_count{{provider="groq",mode="complete"}} 1' in after.text


async def test_a_streamed_turn_is_timed_under_the_stream_mode(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    groq_streaming: httpx.AsyncClient,
) -> None:
    """Total streaming latency is dominated by output length, so filing it under
    ``complete`` would quietly poison the non-streaming distribution."""
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fast",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_headers(make_jwt),
    ) as streamed:
        assert streamed.status_code == 200
        async for _ in streamed.aiter_bytes():
            pass

    body = (await client.get(METRICS)).text

    assert f'{DURATION_MS}_count{{provider="groq",mode="stream"}} 1' in body
    assert f'{DURATION_MS}_count{{provider="groq",mode="complete"}}' not in body


# --------------------------------------------------------------------------- #
# The gauges
# --------------------------------------------------------------------------- #
async def test_the_gauges_are_read_live_for_every_candidate(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """Live from Redis at scrape time, so they are correct on whichever worker
    answers — unlike the counters, which are that worker's own sample."""
    body = (await client.get(METRICS)).text

    assert f"# TYPE {BREAKER_STATE} gauge" in body
    assert f"# TYPE {QUOTA_REMAINING} gauge" in body
    # Every candidate the registry knows, closed and unspent on a fresh Redis.
    assert f'{BREAKER_STATE}{{provider="groq",model=' in body
    assert f'{QUOTA_REMAINING}{{provider="groq",model=' in body


async def test_redis_down_still_returns_the_counters(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """D49's rule, and the reason it is a rule: a metrics endpoint that 500s
    during an incident is a metrics endpoint that is useless exactly when it is
    needed. The gauges are the half that can fail, so they are the half that is
    dropped."""

    class _DeadRedis:
        def __getattr__(self, name: str) -> Any:
            async def boom(*args: Any, **kwargs: Any) -> Any:
                raise ConnectionError("redis is down")

            return boom

        def pipeline(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis is down")

    registry: MetricsRegistry = app.state.metrics
    registry.record_request(provider="groq", model="m", status="ok", key_pool="shared")
    app.state.redis = _DeadRedis()

    response = await client.get(METRICS)

    assert response.status_code == 200
    assert f'{REQUESTS_TOTAL}{{provider="groq",model="m",status="ok",key_pool="shared"}} 1' in (
        response.text
    )
    assert BREAKER_STATE not in response.text
    assert QUOTA_REMAINING not in response.text


async def test_the_body_reads_as_prometheus_text(client: httpx.AsyncClient) -> None:
    """The step's own definition of done: readable by eye, and shaped the way
    ``promtool check metrics`` would want. Every non-comment line is
    ``name{labels} value``, every family declares a TYPE before its samples, and
    the body ends in a newline."""
    body = (await client.get(METRICS)).text

    assert body.endswith("\n")
    typed: set[str] = set()
    for line in body.splitlines():
        if line.startswith("# TYPE "):
            typed.add(line.split()[2])
            continue
        if line.startswith("#"):
            continue
        head, _, value = line.rpartition(" ")
        float(value)
        name = head.split("{", 1)[0]
        family = name.removesuffix("_bucket").removesuffix("_sum").removesuffix("_count")
        assert family in typed, f"{name} has no # TYPE above it"
