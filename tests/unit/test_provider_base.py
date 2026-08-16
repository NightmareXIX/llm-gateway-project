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


# --------------------------------------------------------------------------- #
# _stream_events — Step 7's shared SSE plumbing
# --------------------------------------------------------------------------- #
async def _collect_frames(
    adapter: HttpProviderAdapter, *, idle_timeout_s: float = 5.0
) -> list[str]:
    return [
        frame
        async for frame in adapter._stream_events(
            "POST",
            "/x",
            headers={},
            json={},
            timeout_s=5.0,
            idle_timeout_s=idle_timeout_s,
            model="m",
        )
    ]


async def test_stream_events_assembles_multiline_data_and_skips_comment_lines() -> None:
    """A comment (keepalive) line carries no data and must not surface as an
    empty event — only a real `data:` field is a frame."""
    async with fx.client_streaming(
        [b": keepalive\n\n", b"data: line one\ndata: line two\n\n", b"data: [DONE]\n\n"]
    ) as client:
        frames = await _collect_frames(_BareAdapter(client=client, base_url=BASE_URL))

    assert frames == ["line one\nline two"]


async def test_stream_events_stops_without_yielding_the_done_sentinel() -> None:
    async with fx.client_streaming([b"data: hello\n\n", b"data: [DONE]\n\n"]) as client:
        frames = await _collect_frames(_BareAdapter(client=client, base_url=BASE_URL))

    assert frames == ["hello"]


async def test_stream_events_yields_a_dangling_frame_with_no_trailing_blank_line() -> None:
    """A connection that dies mid-event still has one buffered frame worth
    handing to the adapter — which is what turns it into a decode error instead
    of the delta silently vanishing."""
    async with fx.client_streaming([b"data: unterminated"]) as client:
        frames = await _collect_frames(_BareAdapter(client=client, base_url=BASE_URL))

    assert frames == ["unterminated"]


async def test_stream_events_raises_unavailable_on_an_idle_stall() -> None:
    """A provider that accepts the connection and then goes quiet trips the
    idle timeout rather than hanging the request open."""
    async with fx.client_streaming([b"data: hi\n\n", 10.0, b"data: [DONE]\n\n"]) as client:
        adapter = _BareAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(Unavailable, match="idle stall"):
            await _collect_frames(adapter, idle_timeout_s=0.05)


async def test_stream_events_keepalive_comments_do_not_reset_the_idle_timer() -> None:
    """Trap 8. Each individual gap here is under the idle timeout, but nothing
    but comments arrives, so the deadline — set once and never pushed forward
    by a keepalive — must still expire.

    OpenRouter's ``: OPENROUTER PROCESSING`` lines are indistinguishable from
    genuine liveness by byte count alone; the property under test is that they
    are indistinguishable from *silence* to the idle clock, because that is
    what a stalled generation dressed up in keepalives actually is.

    The margins are wide (150ms gaps against a 250ms budget, three of them)
    rather than tight, because Windows' default event-loop timer resolution is
    coarse enough that a `sleep(0.03)` can silently take closer to 0.05 — a
    flaky assertion here would look like a regression in the fix it exists to
    guard.
    """
    async with fx.client_streaming(
        [b": keep\n\n", 0.15, b": keep\n\n", 0.15, b": keep\n\n", 0.15, b"data: [DONE]\n\n"]
    ) as client:
        adapter = _BareAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(Unavailable, match="idle stall"):
            await _collect_frames(adapter, idle_timeout_s=0.25)


async def test_stream_events_a_data_line_does_reset_the_idle_timer() -> None:
    """The mirror of the test above: real progress, not just a live socket,
    is what should buy a stalled-looking gap more time. Two 150ms gaps sum to
    more than the 250ms budget, so this only passes if each `data:` line
    genuinely pushes the deadline forward rather than the deadline being fixed
    at the first one."""
    async with fx.client_streaming(
        [b"data: a\n\n", 0.15, b"data: b\n\n", 0.15, b"data: [DONE]\n\n"]
    ) as client:
        adapter = _BareAdapter(client=client, base_url=BASE_URL)

        frames = await _collect_frames(adapter, idle_timeout_s=0.25)

    assert frames == ["a", "b"]


async def test_stream_events_normalizes_a_mid_stream_protocol_error() -> None:
    """A truncated body raises `httpx.RemoteProtocolError` partway through
    iteration; it must come out normalized, not as a bare transport exception."""
    async with fx.client_streaming(
        [b"data: hi\n\n", httpx.RemoteProtocolError("truncated")]
    ) as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(Unavailable):
            await _collect_frames(adapter)


async def test_stream_events_raises_the_normalized_error_for_a_non_2xx_status() -> None:
    """Deciding what the status means is still `parse_error`'s job — the
    streaming path checks the status and delegates exactly like the
    non-streaming one, rather than deciding for itself."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    async with fx.client_from(handler) as client:
        adapter = _BareAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(Unavailable):
            await _collect_frames(adapter)
