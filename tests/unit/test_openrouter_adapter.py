"""``OpenRouterAdapter`` execution, usage accounting, and the operational helpers.

Every upstream response here comes from a committed fixture replayed through
``httpx.MockTransport``. Nothing in this module can reach the network, which is
the hard rule from ``CLAUDE.md`` made structural rather than aspirational.

OpenRouter's payload is the least interesting in the pool and its failure modes
are the most, so the weight of this file sits in ``parse_error``. Four cases exist
because getting them wrong is not an error, it is a *plausible-looking* error:
a 402 read as auth pages someone about a free tier working as designed, a 403 read
as auth launders a moderation refusal onto the next provider, a 200 carrying an
error is recorded as "empty" and skips the breaker, and a reset timestamp read as a
delta parks a healthy provider for fifty-six thousand years.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from app.core.clock import FixedClock
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    ContentFiltered,
    ContextTooLong,
    EmptyResponse,
    ProviderError,
    RateLimited,
    Unavailable,
)
from app.providers.openrouter import OpenRouterAdapter
from app.providers.types import GenParams, ModelSpec
from tests import provider_fixtures as fx

KEY = "sk-or-v1-not-a-real-key"
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
BASE_URL = "https://openrouter.ai/api/v1"

OPTIONS = {"HTTP-Referer": "https://llm-gateway-sed.fly.dev", "X-Title": "LLM Gateway"}

# The `x-ratelimit-reset` in the fixtures is 1786622460000 ms — 2026-08-13T12:01Z,
# one minute after this instant. Pinning "now" is what makes the conversion
# assertable at all; against a real clock the expected value moves every second.
NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


def _spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="openrouter",
        model=MODEL,
        context_window=262144,
        max_output_tokens=262144,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=2,
    )


def _adapter(client: httpx.AsyncClient) -> OpenRouterAdapter:
    return OpenRouterAdapter(
        client=client, base_url=BASE_URL, options=OPTIONS, clock=FixedClock(NOW)
    )


async def _adapter_for(fixture_name: str) -> AsyncIterator[OpenRouterAdapter]:
    client = fx.client_returning(fx.load("openrouter", fixture_name))
    try:
        yield _adapter(client)
    finally:
        await client.aclose()


def _offline() -> OpenRouterAdapter:
    return _adapter(httpx.AsyncClient())


def _payload() -> dict[str, object]:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "What is an LLM gateway?"}],
        "temperature": 0.0,
    }


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
async def test_a_successful_completion_carries_text_usage_and_the_providers_id() -> None:
    async for adapter in _adapter_for("success"):
        completion = await adapter.complete(_payload(), KEY, timeout=30.0)

    assert completion.text.startswith("A gateway sits between")
    assert completion.finish_reason == "stop"
    assert completion.usage.tokens_in == 48
    assert completion.usage.tokens_out == 27
    assert completion.usage.estimated is False
    # Useless to us, invaluable in a support ticket — it is the only identifier
    # OpenRouter's side can look up, and the only way to find out which upstream
    # actually served a given generation.
    assert completion.raw_id == "gen-1786622400-Xk2p9wz8qf7ta3n6m0v4"


async def test_the_attribution_headers_from_config_are_sent() -> None:
    """Not secrets, and they vary per deployment, which is why they come from
    ``providers.yaml`` ``options`` rather than from ``Settings``."""
    handler = fx.RecordingHandler(fx.load("openrouter", "success"))
    async with handler.client() as client:
        adapter = _adapter(client)
        await adapter.complete(_payload(), KEY, timeout=30.0)

    assert handler.last.headers["http-referer"] == OPTIONS["HTTP-Referer"]
    assert handler.last.headers["x-title"] == OPTIONS["X-Title"]
    assert handler.last.headers["authorization"] == f"Bearer {KEY}"
    assert handler.last_json() == _payload()


async def test_config_options_cannot_overwrite_the_credential() -> None:
    """``options`` is trusted config rather than user input, so this is a guard
    rather than a fix — but a header map where deployment config can clobber
    ``Authorization`` is a trap worth not setting."""
    handler = fx.RecordingHandler(fx.load("openrouter", "success"))
    async with handler.client() as client:
        adapter = OpenRouterAdapter(
            client=client,
            base_url=BASE_URL,
            options={"Authorization": "Bearer smuggled"},
            clock=FixedClock(NOW),
        )
        await adapter.complete(_payload(), KEY, timeout=30.0)

    assert handler.last.headers["authorization"] == f"Bearer {KEY}"


async def test_the_key_never_appears_in_the_url() -> None:
    handler = fx.RecordingHandler(fx.load("openrouter", "success"))
    async with handler.client() as client:
        adapter = _adapter(client)
        await adapter.complete(_payload(), KEY, timeout=30.0)

    assert KEY not in str(handler.last.url)


# --------------------------------------------------------------------------- #
# complete()
# --------------------------------------------------------------------------- #
async def test_a_200_with_no_content_raises_empty_response() -> None:
    """§2.1.5 case 5. Ordinary rather than exceptional here, since OpenRouter
    proxies upstreams it does not control."""
    async for adapter in _adapter_for("empty_response"):
        with pytest.raises(EmptyResponse) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.failover_eligible is True
    assert caught.value.breaker_eligible is False


async def test_a_safety_refusal_raises_content_filtered_not_empty_response() -> None:
    """Failing over would shop the same prompt around until something answers,
    which is laundering rather than resilience."""
    async for adapter in _adapter_for("content_filtered"):
        with pytest.raises(ContentFiltered) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.failover_eligible is False


async def test_an_error_inside_a_200_is_classified_not_treated_as_empty() -> None:
    """One of OpenRouter's teeth, through ``complete``.

    Reading this as ``EmptyResponse`` would record the wrong code in the attempt
    trail *and* skip the breaker — ``EmptyResponse`` is deliberately not
    breaker-eligible — so a provider returning these all day would never be taken
    out of rotation.
    """
    async for adapter in _adapter_for("error_body_200"):
        with pytest.raises(RateLimited) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.breaker_eligible is True
    assert caught.value.status_code == 200


def test_an_error_inside_a_200_classifies_through_parse_error_too() -> None:
    """The same ladder serves both callers, which is what stops them drifting.

    It works because the body's own ``code`` is consulted before the status — there
    being no meaningful status to consult on this path.
    """
    adapter = _offline()
    response = fx.load("openrouter", "error_body_200").to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert isinstance(error, RateLimited)


async def test_a_truncated_response_raises_unavailable_not_a_decoding_error() -> None:
    """§2.1.5 case 4. The naive implementation lets a JSONDecodeError escape as a
    500 with a traceback instead of failing over."""
    async with fx.client_raising(httpx.RemoteProtocolError("peer closed connection")) as client:
        adapter = _adapter(client)

        with pytest.raises(Unavailable) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.model == MODEL


async def test_a_200_that_is_not_json_raises_empty_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>cloudflare challenge</html>")

    async with fx.client_from(handler) as client:
        adapter = _adapter(client)

        with pytest.raises(EmptyResponse):
            await adapter.complete(_payload(), KEY, timeout=30.0)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("auth_failed", AuthFailed),
        ("credits_exhausted", RateLimited),
        ("bad_request", BadRequest),
        ("context_too_long", ContextTooLong),
        ("rate_limited", RateLimited),
        ("moderated", ContentFiltered),
        ("model_not_found", BadRequest),
        ("error_body_200", RateLimited),
        ("server_error_html", Unavailable),
    ],
)
def test_every_recorded_error_maps_to_its_normalized_class(
    fixture_name: str, expected: type[ProviderError]
) -> None:
    adapter = _offline()
    response = fx.load("openrouter", fixture_name).to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert type(error) is expected
    assert error.provider == "openrouter"
    assert error.model == MODEL


def test_credit_exhaustion_is_a_rate_limit_and_pages_nobody() -> None:
    """The flag is the actual harm, so the flag is what gets asserted.

    ``AuthFailed`` would set ``alert`` and page someone about a free tier working
    exactly as designed. Credit exhaustion is a quota condition, and the router
    should treat it the way it treats every other quota condition: fail over,
    open the breaker, carry on.
    """
    adapter = _offline()
    response = fx.load("openrouter", "credits_exhausted").to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert isinstance(error, RateLimited)
    assert error.alert is False
    assert error.failover_eligible is True
    assert error.breaker_eligible is True


def test_a_moderated_403_is_a_refusal_not_an_auth_failure() -> None:
    """``error.metadata.reasons`` is the only thing distinguishing the two, and
    reading this as auth would page someone *and* launder the refusal onto the next
    provider."""
    adapter = _offline()
    response = fx.load("openrouter", "moderated").to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert isinstance(error, ContentFiltered)
    assert error.failover_eligible is False


def test_a_403_without_moderation_metadata_is_still_an_auth_failure() -> None:
    """The other side of that branch — otherwise a genuine credential problem would
    be silently classified as a content refusal and never alert."""
    adapter = _offline()

    error = adapter.parse_error(
        httpx.Response(403, json={"error": {"message": "Forbidden", "code": 403}}),
        model=MODEL,
    )

    assert isinstance(error, AuthFailed)


def test_an_integer_error_code_classifies_without_raising() -> None:
    """Every other provider in the pool sends ``code`` as a string. An adapter that
    assumes that here does not crash — it silently matches nothing and falls
    through to the status ladder, which is why this is asserted rather than
    trusted."""
    adapter = _offline()

    error = adapter.parse_error(
        httpx.Response(200, json={"error": {"message": "Insufficient credits", "code": 402}}),
        model=MODEL,
    )

    assert isinstance(error, RateLimited)


def test_a_context_overflow_recovers_the_real_limit_from_the_prose() -> None:
    """``ContextTooLong`` is the one error with a retry path, and re-running the
    fitting step against a real number beats halving the history and hoping."""
    adapter = _offline()
    response = fx.load("openrouter", "context_too_long").to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert isinstance(error, ContextTooLong)
    assert error.limit_tokens == 262144
    assert error.failover_eligible is False


def test_parse_error_never_raises_even_on_nonsense() -> None:
    """It runs on the failure path, where an exception replaces a clean 502 with a
    traceback."""
    adapter = _offline()

    error = adapter.parse_error(httpx.Response(418, text="\x00\x01"), model=MODEL)

    assert isinstance(error, Unavailable)


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #
async def test_a_missing_usage_block_is_estimated_rather_than_reported_as_zero() -> None:
    """A zero is indistinguishable from a real measurement and would silently
    under-count a daily budget. `estimated` is what Phase 3 keys on."""
    async for adapter in _adapter_for("success_no_usage"):
        completion = await adapter.complete(_payload(), KEY, timeout=30.0)

    assert completion.usage.estimated is True
    assert completion.usage.tokens_in > 0
    assert completion.usage.tokens_out > 0


def test_extract_usage_reads_openrouters_own_counts() -> None:
    adapter = _offline()
    recorded = fx.load("openrouter", "success")
    assert recorded.body is not None

    usage = adapter.extract_usage(recorded.body)

    assert usage.tokens_in == 48
    assert usage.tokens_out == 27
    assert usage.estimated is False


def test_token_estimate_lands_within_twenty_five_percent_of_the_character_count() -> None:
    """§2.1.5 case 6. Accuracy beyond this would mean carrying a tokenizer to
    improve a number Phase 3 reconciles against ground truth anyway."""
    adapter = _offline()
    payload = adapter.build_payload(fx.canonical_history(), _spec(), GenParams(), [])

    estimate = adapter.estimate_tokens(payload)
    actual_chars = sum(len(str(message["content"])) for message in payload["messages"])

    assert estimate == pytest.approx(actual_chars / 4, rel=0.25, abs=32)


def test_token_estimate_is_never_zero() -> None:
    """A zero reservation would let an unbounded request through the quota gate."""
    adapter = _offline()

    assert adapter.estimate_tokens({}) == 1
    assert adapter.estimate_tokens({"messages": []}) == 1


# --------------------------------------------------------------------------- #
# validate_key
# --------------------------------------------------------------------------- #
async def test_a_live_key_validates_and_reports_the_models_it_can_reach() -> None:
    async for adapter in _adapter_for("models_list"):
        result = await adapter.validate_key(KEY)

    assert result.valid is True
    assert result.provider == "openrouter"
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
# rate_limit_headers — the millisecond-epoch trap
# --------------------------------------------------------------------------- #
def test_rate_limit_headers_are_read_off_a_successful_response() -> None:
    adapter = _offline()
    response = fx.load("openrouter", "success").to_response()

    hint = adapter.rate_limit_headers(response)

    assert hint is not None
    assert hint.requests_remaining == 19
    # The fixture's reset is one minute after the pinned clock.
    assert hint.requests_reset_s == pytest.approx(60.0)
    # OpenRouter publishes no token counters at all.
    assert hint.tokens_remaining is None
    assert hint.tokens_reset_s is None


def test_a_response_without_rate_limit_headers_yields_no_hint() -> None:
    """Opportunistic, never required — Phase 3 carries on with what it counted."""
    adapter = _offline()

    assert adapter.rate_limit_headers(httpx.Response(200, json={})) is None


def test_the_retry_hint_comes_from_the_reset_timestamp_when_there_is_no_header() -> None:
    """A 429 with no ``Retry-After`` is the normal case here, and the breaker reads
    ``retry_after_s`` to size its cooldown."""
    adapter = _offline()
    recorded = fx.load("openrouter", "rate_limited")
    assert "retry-after" not in {name.lower() for name in recorded.headers}

    error = adapter.parse_error(recorded.to_response(), model=MODEL)

    assert isinstance(error, RateLimited)
    assert error.retry_after_s == pytest.approx(60.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Milliseconds since the epoch — what OpenRouter actually sends.
        ("1786622460000", 60.0),
        # Seconds since the epoch, observed on some routes. A 1000x error here is
        # the difference between a working failover and a provider parked forever.
        ("1786622460", 60.0),
        # Small enough to already be a delta rather than a timestamp.
        ("30", 30.0),
        # In the past: clamped, never negative — a negative cooldown would make the
        # breaker's arithmetic reopen instantly or, worse, go backwards.
        ("1786622340000", 0.0),
        (None, None),
        ("", None),
        ("soon", None),
    ],
)
def test_the_reset_header_converts_to_seconds_from_now(
    raw: str | None, expected: float | None
) -> None:
    adapter = _offline()
    headers = {} if raw is None else {"x-ratelimit-reset": raw}

    hint = adapter.rate_limit_headers(httpx.Response(429, headers=headers, json={}))

    if expected is None:
        assert hint is None
    else:
        assert hint is not None
        assert hint.requests_reset_s == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# The Phase 2 seam
# --------------------------------------------------------------------------- #
def test_streaming_raises_on_call_not_on_first_chunk() -> None:
    """Step 7's job. An `async def` with a `yield` would return a well-formed
    generator and only fail on the first `__anext__` — deep inside an SSE
    response, after the headers were already sent."""
    adapter = _offline()

    with pytest.raises(NotImplementedError, match="Phase 2"):
        adapter.stream(_payload(), KEY, timeout=30.0, idle_timeout=30.0)
