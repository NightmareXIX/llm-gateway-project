"""``HttpProviderAdapter`` — the shared HTTP layer beneath every adapter.

Two things belong to this layer rather than to any adapter: mapping transport
failures (which never produce a response, so nothing downstream could normalize
them) and applying a per-request timeout that is not the shared client's.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_READ_TIMEOUT_S,
    HttpProviderAdapter,
    ProviderAdapter,
)
from app.providers.errors import Unavailable
from app.providers.groq import GroqAdapter
from tests import provider_fixtures as fx

BASE_URL = "https://api.groq.com/openai/v1"


class _BareAdapter(HttpProviderAdapter):
    """The base class alone, with no adapter overriding anything."""

    name = "bare"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("silent"),
        httpx.WriteTimeout("stalled"),
        httpx.PoolTimeout("no connection available"),
        httpx.RemoteProtocolError("truncated"),
    ],
)
async def test_every_transport_failure_leaves_as_a_normalized_error(exc: Exception) -> None:
    """Never as a raw httpx exception. A router that had to catch
    `httpx.ConnectError` would be a router that knows its providers speak HTTP,
    which is exactly what the adapter layer exists to hide."""
    async with fx.client_raising(exc) as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(Unavailable) as caught:
            await adapter._request("POST", "/chat/completions", headers={}, model="m")

    assert caught.value.provider == "groq"
    assert caught.value.model == "m"
    assert caught.value.__cause__ is exc


async def test_a_non_2xx_response_is_returned_rather_than_raised() -> None:
    """Deciding what a 429 means is `parse_error`'s job. Raising here would put
    that decision in two places, free to disagree."""
    async with fx.client_returning(fx.load("groq", "rate_limited")) as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)

        response = await adapter._request("POST", "/chat/completions", headers={})

    assert response.status_code == 429


async def test_each_request_carries_its_own_read_timeout() -> None:
    """The shared client's read timeout is sized for the JWKS fetch on the auth
    path. A completion takes tens of seconds, so adapters share the connection
    pool but never the deadline."""
    # httpx flattens the `Timeout` object into a plain mapping on the way to the
    # transport, which is the only place the per-request override is observable.
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, json={})

    async with fx.client_from(handler) as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)
        await adapter._request("POST", "/chat/completions", headers={}, timeout_s=45.0)

    assert seen[0]["read"] == 45.0
    assert seen[0]["connect"] == DEFAULT_CONNECT_TIMEOUT_S


def test_the_default_read_timeout_is_sized_for_a_completion() -> None:
    """Guards the constant against being 'tidied' back down to the client's 10s."""
    assert DEFAULT_READ_TIMEOUT_S >= 60.0


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        (BASE_URL, "/chat/completions", f"{BASE_URL}/chat/completions"),
        (f"{BASE_URL}/", "/chat/completions", f"{BASE_URL}/chat/completions"),
        (BASE_URL, "chat/completions", f"{BASE_URL}/chat/completions"),
    ],
)
def test_urls_join_without_doubling_or_dropping_a_slash(
    base: str, path: str, expected: str
) -> None:
    """A trailing slash in providers.yaml should not produce a 404."""
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=base)

    assert adapter._url(path) == expected


def test_the_base_class_falls_back_to_unavailable_rather_than_attribute_error() -> None:
    """`_request` calls `parse_error`, so a half-built adapter must still fail as
    a routable error rather than a 500."""
    adapter = _BareAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    error = adapter.parse_error(httpx.ConnectError("refused"))

    assert isinstance(error, Unavailable)
    assert error.model == "unknown"


def test_the_base_class_publishes_no_rate_limit_hint() -> None:
    adapter = _BareAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    assert adapter.rate_limit_headers(httpx.Response(200)) is None


def test_the_groq_adapter_satisfies_the_protocol_at_runtime_too() -> None:
    """mypy proves this statically via `registry._conformance_check`; this catches
    the case where someone reaches for `isinstance` at runtime and finds it works."""
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    assert isinstance(adapter, ProviderAdapter)
