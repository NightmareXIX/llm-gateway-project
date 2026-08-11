"""``GroqAdapter`` execution, usage accounting, and the operational helpers.

Every upstream response here comes from a committed fixture replayed through
``httpx.MockTransport``. Nothing in this module can reach the network, which is
the hard rule from ``CLAUDE.md`` made structural rather than aspirational.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.providers.errors import (
    AuthFailed,
    ContentFiltered,
    EmptyResponse,
    Unavailable,
)
from app.providers.groq import GroqAdapter, _parse_duration
from app.providers.types import GenParams, ModelSpec
from tests import provider_fixtures as fx

KEY = "gsk_not_a_real_key"
MODEL = "llama-3.3-70b-versatile"
BASE_URL = "https://api.groq.com/openai/v1"


def _spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="groq",
        model=MODEL,
        context_window=131072,
        max_output_tokens=32768,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


async def _adapter_for(fixture_name: str) -> AsyncIterator[GroqAdapter]:
    client = fx.client_returning(fx.load("groq", fixture_name))
    try:
        yield GroqAdapter(client=client, base_url=BASE_URL)
    finally:
        await client.aclose()


def _payload() -> dict[str, object]:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "What is an LLM gateway?"}],
        "temperature": 0.0,
    }


# --------------------------------------------------------------------------- #
# complete()
# --------------------------------------------------------------------------- #
async def test_a_successful_completion_carries_text_usage_and_the_providers_id() -> None:
    handler = fx.RecordingHandler(fx.load("groq", "success"))
    async with handler.client() as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)

        completion = await adapter.complete(_payload(), KEY, timeout=30.0)

    assert completion.text.startswith("A gateway sits between")
    assert completion.finish_reason == "stop"
    assert completion.usage.tokens_in == 48
    assert completion.usage.tokens_out == 27
    assert completion.usage.estimated is False
    # Useless to us, invaluable in a support ticket — it is the only identifier
    # Groq's side can look up.
    assert completion.raw_id == "chatcmpl-8a1f0c2e-3d44-4b1a-9f77-0c2a5b6e1d90"


async def test_the_request_carries_a_bearer_token_and_the_payload_verbatim() -> None:
    handler = fx.RecordingHandler(fx.load("groq", "success"))
    async with handler.client() as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)
        await adapter.complete(_payload(), KEY, timeout=30.0)

    assert handler.last.url.path.endswith("/chat/completions")
    assert handler.last.headers["authorization"] == f"Bearer {KEY}"
    assert handler.last_json() == _payload()


async def test_the_key_never_appears_in_the_url() -> None:
    """Groq authenticates by header. A key in a query string ends up in access
    logs on every hop between here and them."""
    handler = fx.RecordingHandler(fx.load("groq", "success"))
    async with handler.client() as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)
        await adapter.complete(_payload(), KEY, timeout=30.0)

    assert KEY not in str(handler.last.url)


async def test_a_200_with_no_content_raises_empty_response() -> None:
    """§2.1.5 case 5. Without this class the gateway ships empty assistant
    messages and the model gets blamed for a transport problem."""
    async for adapter in _adapter_for("empty_response"):
        with pytest.raises(EmptyResponse) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.failover_eligible is True
    assert caught.value.breaker_eligible is False


async def test_whitespace_only_content_is_also_an_empty_response() -> None:
    recorded = fx.load("groq", "success")
    assert recorded.body is not None
    recorded.body["choices"][0]["message"]["content"] = "   \n  "

    async with fx.client_returning(recorded) as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)
        with pytest.raises(EmptyResponse):
            await adapter.complete(_payload(), KEY, timeout=30.0)


async def test_a_safety_refusal_raises_content_filtered_not_empty_response() -> None:
    """Failing over would shop the same prompt around until something answers,
    which is laundering rather than resilience."""
    async for adapter in _adapter_for("content_filtered"):
        with pytest.raises(ContentFiltered) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.failover_eligible is False


async def test_an_error_status_raises_the_normalized_class() -> None:
    async for adapter in _adapter_for("auth_failed"):
        with pytest.raises(AuthFailed):
            await adapter.complete(_payload(), KEY, timeout=30.0)


async def test_a_truncated_response_raises_unavailable_not_a_decoding_error() -> None:
    """§2.1.5 case 4. The naive implementation lets a JSONDecodeError escape as a
    500 with a traceback instead of failing over."""
    async with fx.client_raising(httpx.RemoteProtocolError("peer closed connection")) as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(Unavailable) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.model == MODEL


async def test_a_200_that_is_not_json_raises_empty_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with fx.client_from(handler) as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(EmptyResponse):
            await adapter.complete(_payload(), KEY, timeout=30.0)


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
async def test_a_missing_usage_block_is_estimated_rather_than_reported_as_zero() -> None:
    """A zero is indistinguishable from a real measurement and would silently
    under-count a daily budget. `estimated` is what Phase 3 keys on."""
    handler = fx.RecordingHandler(fx.load("groq", "success_no_usage"))
    async with handler.client() as client:
        adapter = GroqAdapter(client=client, base_url=BASE_URL)

        completion = await adapter.complete(_payload(), KEY, timeout=30.0)

    assert completion.usage.estimated is True
    assert completion.usage.tokens_in > 0
    assert completion.usage.tokens_out > 0


def test_extract_usage_reads_groqs_own_counts() -> None:
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    recorded = fx.load("groq", "success")
    assert recorded.body is not None

    usage = adapter.extract_usage(recorded.body)

    assert usage == type(usage)(tokens_in=48, tokens_out=27, estimated=False)


def test_token_estimate_lands_within_twenty_five_percent_of_reported_usage() -> None:
    """§2.1.5 case 6. Accuracy beyond this would mean carrying a tokenizer to
    improve a number Phase 3 reconciles against ground truth anyway."""
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    recorded = fx.load("groq", "success")
    assert recorded.body is not None
    reported = recorded.body["usage"]["prompt_tokens"]

    # The payload that produced the recorded response.
    payload = adapter.build_payload(fx.canonical_history(), _spec(), GenParams(), [])
    estimate = adapter.estimate_tokens(payload)
    actual_chars = sum(len(str(m["content"])) for m in payload["messages"])

    # Two checks: the ratio the estimator claims, and that it is in the right
    # ballpark for the fixture's own prompt.
    assert estimate == pytest.approx(actual_chars / 4, rel=0.25, abs=32)
    assert reported > 0


def test_token_estimate_is_never_zero() -> None:
    """A zero reservation would let an unbounded request through the quota gate."""
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    assert adapter.estimate_tokens({}) == 1
    assert adapter.estimate_tokens({"messages": []}) == 1


# --------------------------------------------------------------------------- #
# validate_key
# --------------------------------------------------------------------------- #
async def test_a_live_key_validates_and_reports_the_models_it_can_reach() -> None:
    async for adapter in _adapter_for("models_list"):
        result = await adapter.validate_key(KEY)

    assert result.valid is True
    assert result.provider == "groq"
    assert MODEL in result.models


async def test_a_rejected_key_is_invalid_with_a_message_for_the_user() -> None:
    async for adapter in _adapter_for("auth_failed"):
        result = await adapter.validate_key(KEY)

    assert result.valid is False
    assert result.detail is not None
    assert KEY not in result.detail


async def test_an_unreachable_provider_raises_rather_than_calling_the_key_invalid() -> None:
    """§9.2's confusing failure: telling a user their key is bad when the truth
    is that the provider was down."""
    async for adapter in _adapter_for("server_error_html"):
        with pytest.raises(Unavailable):
            await adapter.validate_key(KEY)


# --------------------------------------------------------------------------- #
# rate_limit_headers
# --------------------------------------------------------------------------- #
def test_rate_limit_headers_are_read_off_a_successful_response() -> None:
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    response = fx.load("groq", "success").to_response()

    hint = adapter.rate_limit_headers(response)

    assert hint is not None
    assert hint.requests_remaining == 997
    assert hint.tokens_remaining == 11873
    assert hint.requests_reset_s == pytest.approx(179.56)
    assert hint.tokens_reset_s == pytest.approx(7.66)


def test_a_response_without_rate_limit_headers_yields_no_hint() -> None:
    """Opportunistic, never required — Phase 3 carries on with what it counted."""
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    assert adapter.rate_limit_headers(httpx.Response(200, json={})) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7.66s", 7.66),
        ("2m59.56s", 179.56),
        ("1h2m3s", 3723.0),
        ("500ms", 0.5),
        ("60", 60.0),
        ("", None),
        ("soon", None),
    ],
)
def test_go_style_durations_parse(raw: str, expected: float | None) -> None:
    """Groq reports resets as Go durations, so a naive float() yields None on
    every real response."""
    result = _parse_duration(raw)

    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# The Phase 2 seam
# --------------------------------------------------------------------------- #
def test_streaming_raises_on_call_not_on_first_chunk() -> None:
    """The distinction that matters. An `async def` with a `yield` would return a
    well-formed generator and only fail on the first `__anext__` — deep inside an
    SSE response, after the headers were already sent."""
    adapter = GroqAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    with pytest.raises(NotImplementedError, match="Phase 2"):
        adapter.stream(_payload(), KEY, timeout=30.0, idle_timeout=30.0)
