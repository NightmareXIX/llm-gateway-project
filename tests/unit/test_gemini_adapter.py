"""``GeminiAdapter`` execution, usage accounting, and the operational helpers.

Every upstream response here comes from a committed fixture replayed through
``httpx.MockTransport``. Nothing in this module can reach the network, which is
the hard rule from ``CLAUDE.md`` made structural rather than aspirational.

Two tests earn their keep more than the rest, because both guard failures that
look like something else:

* the ``blocked_prompt`` ordering test — a refusal misclassified as
  ``EmptyResponse`` becomes failover-eligible, so the refused prompt gets shopped
  around the pool until something answers it;
* ``api_key_invalid`` → ``AuthFailed`` — Gemini answers a dead key with a 400, and
  the naive reading makes that a ``BadRequest``, which aborts the whole request
  instead of failing over to a provider whose key still works.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

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
from app.providers.gemini import (
    MODEL_FIELD,
    GeminiAdapter,
    _model_from_url,
    _parse_duration,
)
from app.providers.types import GenParams, ModelSpec
from tests import provider_fixtures as fx

KEY = "AIzaSyNotARealKeyAtAll"
MODEL = "gemini-3.6-flash"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="gemini",
        model=MODEL,
        context_window=1048576,
        max_output_tokens=65536,
        supports_streaming=True,
        supports_vision=True,
        supports_pdf=True,
        supports_system_field=True,
        max_file_bytes=20000000,
        priority=1,
    )


async def _adapter_for(fixture_name: str) -> AsyncIterator[GeminiAdapter]:
    client = fx.client_returning(fx.load("gemini", fixture_name))
    try:
        yield GeminiAdapter(client=client, base_url=BASE_URL)
    finally:
        await client.aclose()


def _payload() -> dict[str, object]:
    return {
        MODEL_FIELD: MODEL,
        "contents": [{"role": "user", "parts": [{"text": "What is an LLM gateway?"}]}],
        "generationConfig": {"temperature": 0.0},
    }


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
async def test_the_model_goes_in_the_url_and_the_key_goes_in_a_header() -> None:
    handler = fx.RecordingHandler(fx.load("gemini", "success"))
    async with handler.client() as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)
        await adapter.complete(_payload(), KEY, timeout=30.0)

    assert handler.last.url.path.endswith(f"/models/{MODEL}:generateContent")
    assert handler.last.headers["x-goog-api-key"] == KEY
    assert "authorization" not in handler.last.headers


async def test_the_key_never_appears_in_the_url() -> None:
    """Gemini also accepts ``?key=``, so this is not the redundant test it looks
    like: choosing the header is a decision, and a URL ends up in access logs on
    every hop between here and Google."""
    handler = fx.RecordingHandler(fx.load("gemini", "success"))
    async with handler.client() as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)
        await adapter.complete(_payload(), KEY, timeout=30.0)

    assert KEY not in str(handler.last.url)


async def test_the_gateway_internal_model_key_never_reaches_the_wire() -> None:
    """``_model`` is how the model reaches ``complete`` at all, and Google rejects
    unknown top-level fields with a 400 — so the strip is load-bearing, not tidying."""
    handler = fx.RecordingHandler(fx.load("gemini", "success"))
    async with handler.client() as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)
        await adapter.complete(_payload(), KEY, timeout=30.0)

    body = handler.last_json()
    assert MODEL_FIELD not in body
    assert "model" not in body
    assert body["contents"] == _payload()["contents"]


async def test_a_payload_that_did_not_come_from_build_payload_fails_loudly() -> None:
    """There is no sane default here: guessing a model would send the history to
    whichever one happened to be hardcoded."""
    async for adapter in _adapter_for("success"):
        with pytest.raises(ValueError, match=MODEL_FIELD):
            await adapter.complete({"contents": []}, KEY, timeout=30.0)


# --------------------------------------------------------------------------- #
# complete()
# --------------------------------------------------------------------------- #
async def test_a_successful_completion_carries_text_usage_and_the_providers_id() -> None:
    """Expectations are read out of the fixture rather than pasted from it.

    ``gemini/success.json`` is a live capture, so its text, token counts and
    ``responseId`` all change every time it is re-recorded. Hardcoding them makes
    a routine ``make record-fixtures`` look like four regressions, and the literal
    that would then get updated is never the interesting part. What is interesting
    is which *keys* the adapter read — ``responseId`` and not ``id``,
    ``promptTokenCount`` and not ``prompt_tokens`` — and that survives a re-record.
    """
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    candidate = recorded.body["candidates"][0]
    metadata = recorded.body["usageMetadata"]

    async for adapter in _adapter_for("success"):
        completion = await adapter.complete(_payload(), KEY, timeout=30.0)

    assert completion.text == candidate["content"]["parts"][0]["text"]
    assert candidate["finishReason"] == "STOP"
    assert completion.finish_reason == "stop"
    assert completion.usage.tokens_in == metadata["promptTokenCount"]
    assert (
        completion.usage.tokens_out
        == metadata["candidatesTokenCount"] + metadata["thoughtsTokenCount"]
    )
    assert completion.usage.estimated is False
    # Useless to us, invaluable in a support ticket — it is the only identifier
    # Google's side can look up. Note the key: `responseId`, not `id`.
    assert completion.raw_id == recorded.body["responseId"]


async def test_max_tokens_finish_reason_maps_to_length() -> None:
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    recorded.body["candidates"][0]["finishReason"] = "MAX_TOKENS"

    async with fx.client_returning(recorded) as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)
        completion = await adapter.complete(_payload(), KEY, timeout=30.0)

    assert completion.finish_reason == "length"


async def test_a_200_with_no_candidates_raises_empty_response() -> None:
    """§2.1.5 case 5. Without this class the gateway ships empty assistant
    messages and the model gets blamed for a transport problem."""
    async for adapter in _adapter_for("empty_response"):
        with pytest.raises(EmptyResponse) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.failover_eligible is True
    assert caught.value.breaker_eligible is False


async def test_a_blocked_prompt_is_content_filtered_not_an_empty_response() -> None:
    """The ordering test, and the one most worth having.

    A blocked prompt arrives with ``promptFeedback.blockReason`` *and* an empty
    ``candidates`` list. An adapter that checks emptiness first reports
    ``EmptyResponse``, which is failover-eligible — so the router shops the refused
    prompt to Groq, then OpenRouter, until one of them answers it. That is
    laundering a refusal, and nothing about the resulting response would look
    wrong.
    """
    async for adapter in _adapter_for("blocked_prompt"):
        with pytest.raises(ContentFiltered) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.failover_eligible is False
    assert caught.value.retryable_same_provider is False
    assert "SAFETY" in str(caught.value)


async def test_a_safety_finish_reason_is_also_content_filtered() -> None:
    """The other shape a refusal takes: the prompt passed and the model stopped
    itself mid-generation."""
    async for adapter in _adapter_for("finish_reason_safety"):
        with pytest.raises(ContentFiltered):
            await adapter.complete(_payload(), KEY, timeout=30.0)


async def test_whitespace_only_content_is_an_empty_response() -> None:
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    recorded.body["candidates"][0]["content"]["parts"] = [{"text": "   \n  "}]

    async with fx.client_returning(recorded) as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)
        with pytest.raises(EmptyResponse):
            await adapter.complete(_payload(), KEY, timeout=30.0)


async def test_multiple_text_parts_are_joined_without_a_separator() -> None:
    """Gemini splits one answer across parts; it does not delimit them."""
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    recorded.body["candidates"][0]["content"]["parts"] = [
        {"text": "Restart the stream"},
        {"text": " and discard the partial."},
    ]

    async with fx.client_returning(recorded) as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)
        completion = await adapter.complete(_payload(), KEY, timeout=30.0)

    assert completion.text == "Restart the stream and discard the partial."


async def test_a_truncated_response_raises_unavailable_not_a_decoding_error() -> None:
    """§2.1.5 case 4. The naive implementation lets a JSONDecodeError escape as a
    500 with a traceback instead of failing over."""
    async with fx.client_raising(httpx.RemoteProtocolError("peer closed connection")) as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(Unavailable) as caught:
            await adapter.complete(_payload(), KEY, timeout=30.0)

    assert caught.value.model == MODEL


async def test_a_200_that_is_not_json_raises_empty_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with fx.client_from(handler) as client:
        adapter = GeminiAdapter(client=client, base_url=BASE_URL)

        with pytest.raises(EmptyResponse):
            await adapter.complete(_payload(), KEY, timeout=30.0)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("auth_failed", AuthFailed),
        ("api_key_invalid", AuthFailed),
        ("bad_request", BadRequest),
        ("context_too_long", ContextTooLong),
        ("rate_limited", RateLimited),
        ("model_not_found", BadRequest),
        ("unavailable", Unavailable),
        ("server_error_html", Unavailable),
    ],
)
def test_every_recorded_error_maps_to_its_normalized_class(
    fixture_name: str, expected: type[ProviderError]
) -> None:
    """Gemini's ``status`` strings are the half of this that would otherwise be
    discovered in production, because the HTTP codes alone are ambiguous."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    response = fx.load("gemini", fixture_name).to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert type(error) is expected
    assert error.provider == "gemini"
    assert error.model == MODEL


def test_a_dead_key_is_an_auth_failure_even_though_it_arrives_as_a_400() -> None:
    """The quirk that would otherwise strand a request.

    ``BadRequest`` is neither retryable nor failover-eligible, so classifying a
    revoked ``GEMINI_API_KEY`` that way aborts the request on the first candidate
    while Groq and OpenRouter sit there with working keys. ``AuthFailed`` also
    raises the ``alert`` flag, which is correct: a dead key is an ops problem.
    """
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    response = fx.load("gemini", "api_key_invalid").to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert isinstance(error, AuthFailed)
    assert error.failover_eligible is True
    assert error.alert is True


def test_the_retry_hint_is_read_out_of_the_body_not_a_header() -> None:
    """Every other provider in the pool sends this as a header. An adapter that
    only reads headers finds nothing on every real Gemini 429 and silently falls
    back to the breaker's exponential ladder."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    recorded = fx.load("gemini", "rate_limited")
    assert "retry-after" not in {name.lower() for name in recorded.headers}

    error = adapter.parse_error(recorded.to_response(), model=MODEL)

    assert isinstance(error, RateLimited)
    assert error.retry_after_s == pytest.approx(23.0)


def test_a_context_overflow_recovers_the_real_limit_from_the_prose() -> None:
    """``ContextTooLong`` is the one error with a retry path, and re-running the
    fitting step against a real number beats halving the history and hoping."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    response = fx.load("gemini", "context_too_long").to_response()

    error = adapter.parse_error(response, model=MODEL)

    assert isinstance(error, ContextTooLong)
    assert error.limit_tokens == 1048576
    assert error.failover_eligible is False
    assert error.retryable_same_provider is True


def test_parse_error_never_raises_even_on_nonsense() -> None:
    """It runs on the failure path, where an exception replaces a clean 502 with a
    traceback."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

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


def test_extract_usage_reads_geminis_own_counts_and_not_openais() -> None:
    """The negative half is the one worth having.

    Reading the live capture's own numbers back is close to tautological; asserting
    that an OpenAI-shaped ``usage`` block yields *nothing* is not. A copied Groq
    extractor would sail through the positive assertion on a body that happened to
    carry both, and free-tier proxies do sometimes carry both.
    """
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    metadata = recorded.body["usageMetadata"]

    usage = adapter.extract_usage(recorded.body)

    assert usage.tokens_in == metadata["promptTokenCount"]
    assert usage.estimated is False

    openai_shaped = adapter.extract_usage({"usage": {"prompt_tokens": 48, "completion_tokens": 27}})
    assert openai_shaped.estimated is True
    assert openai_shaped.tokens_in == 0


def test_thinking_tokens_count_towards_output() -> None:
    """``thoughtsTokenCount`` sits outside ``candidatesTokenCount`` but is generated
    and billed as output.

    The live capture is the argument, and the scale of it is why this test exists:
    the recording that produced this fixture spent an order of magnitude more
    tokens thinking than answering. Reading only ``candidatesTokenCount`` therefore
    under-counts a single response by more than 10x, on the model most likely to be
    routed to — so Phase 3's tracker would be wrong in the direction that gets a key
    rate-limited far earlier than it predicted, which presents as a provider problem
    rather than an accounting one.
    """
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    metadata = recorded.body["usageMetadata"]
    answer, thoughts = metadata["candidatesTokenCount"], metadata["thoughtsTokenCount"]
    assert thoughts > answer, "re-record: this fixture no longer demonstrates the problem"

    usage = adapter.extract_usage(recorded.body)

    assert usage.tokens_out == answer + thoughts


def test_usage_without_thinking_tokens_is_just_the_answer() -> None:
    """The other side of that sum. Non-thinking models omit the field entirely, and
    treating a missing count as anything but zero would inflate every one of them."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    answer = recorded.body["usageMetadata"]["candidatesTokenCount"]
    del recorded.body["usageMetadata"]["thoughtsTokenCount"]

    usage = adapter.extract_usage(recorded.body)

    assert usage.tokens_out == answer
    assert usage.estimated is False


def test_token_estimate_lands_near_the_reported_prompt_count() -> None:
    """§2.1.5 case 6, against the prompt that actually produced the number.

    Estimating the frozen ``canonical_history()`` here instead would compare an
    estimate of one prompt against usage reported for a different, much shorter one
    — which is a statement about the two prompts' relative lengths, not about the
    estimator.
    """
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    recorded = fx.load("gemini", "success")
    assert recorded.body is not None
    assert recorded.request_body is not None
    reported = recorded.body["usageMetadata"]["promptTokenCount"]

    estimate = adapter.estimate_tokens(recorded.request_body)

    assert 0.25 <= estimate / reported <= 4.0


def test_the_estimate_counts_the_hoisted_system_prompt() -> None:
    """The system prompt leaves ``contents`` entirely, so an estimator that only
    walked ``contents`` would under-count every request that has one — which is
    every request the gateway sends."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    payload = adapter.build_payload(fx.canonical_history(), _spec(), GenParams(), [])

    with_system = adapter.estimate_tokens(payload)
    without_system = adapter.estimate_tokens(
        {key: value for key, value in payload.items() if key != "system_instruction"}
    )

    assert with_system > without_system


def test_token_estimate_is_never_zero() -> None:
    """A zero reservation would let an unbounded request through the quota gate."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    assert adapter.estimate_tokens({}) == 1
    assert adapter.estimate_tokens({"contents": []}) == 1


# --------------------------------------------------------------------------- #
# validate_key
# --------------------------------------------------------------------------- #
async def test_a_live_key_validates_and_reports_the_models_it_can_reach() -> None:
    """The ``models/`` prefix is stripped, so the result is comparable to a
    ``ModelSpec.model``. Leaving it on makes every such comparison silently false,
    which is what Phase 6's per-provider capability merge depends on."""
    async for adapter in _adapter_for("models_list"):
        result = await adapter.validate_key(KEY)

    assert result.valid is True
    assert result.provider == "gemini"
    assert MODEL in result.models
    assert not any(name.startswith("models/") for name in result.models)


async def test_a_rejected_key_is_invalid_with_a_message_for_the_user() -> None:
    async for adapter in _adapter_for("auth_failed"):
        result = await adapter.validate_key(KEY)

    assert result.valid is False
    assert result.detail is not None
    assert KEY not in result.detail


async def test_a_400_saying_the_key_is_invalid_is_a_rejection_not_a_failure() -> None:
    """The BYOK half of the 400-not-401 quirk. Raising ``BadRequest`` from a method
    whose contract is "raise only when we learned nothing" would tell the user
    their key could not be checked, when in fact it was checked and rejected."""
    async for adapter in _adapter_for("api_key_invalid"):
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
# rate_limit_headers, and the helpers
# --------------------------------------------------------------------------- #
def test_gemini_publishes_no_rate_limit_headers() -> None:
    """Deliberately not overridden. Gemini sends none, so the base's ``None`` is
    the honest answer and a parser here would be code that never runs. This test
    exists so anyone who adds one has to face the fixture first."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)
    response = fx.load("gemini", "success").to_response()

    assert adapter.rate_limit_headers(response) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1beta/models/gemini-3.6-flash:generateContent", "gemini-3.6-flash"),
        ("/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent", "gemini-3.5-flash-lite"),
        # No colon: the catalogue endpoint. Without the guard this yields the
        # model name "models", and every error from a key validation would be
        # attributed to a model that does not exist.
        ("/v1beta/models", None),
        ("/v1beta", None),
    ],
)
def test_the_model_is_recovered_from_the_url(path: str, expected: str | None) -> None:
    """Lets the contract's one-argument ``parse_error(response)`` still name the
    model, so an error opens the right circuit breaker."""
    request = httpx.Request("POST", f"https://generativelanguage.googleapis.com{path}")
    response = httpx.Response(500, request=request)

    assert _model_from_url(response) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("23s", 23.0),
        ("0.5s", 0.5),
        ("60", 60.0),
        (12, 12.0),
        ("", None),
        ("soon", None),
        ("2m59.56s", None),
    ],
)
def test_protobuf_durations_parse(raw: object, expected: float | None) -> None:
    """Gemini's ``retryDelay`` is a protobuf ``Duration`` — a decimal with a
    trailing ``s``. Groq's Go-duration grammar is a different format, which is why
    the parser is not shared: ``"2m59.56s"`` is not something Gemini sends."""
    result = _parse_duration(raw)

    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# The Phase 2 seam
# --------------------------------------------------------------------------- #
def test_streaming_raises_on_call_not_on_first_chunk() -> None:
    """Step 7's job. An `async def` with a `yield` would return a well-formed
    generator and only fail on the first `__anext__` — deep inside an SSE
    response, after the headers were already sent."""
    adapter = GeminiAdapter(client=httpx.AsyncClient(), base_url=BASE_URL)

    with pytest.raises(NotImplementedError, match="Phase 2"):
        adapter.stream(_payload(), KEY, timeout=30.0, idle_timeout=30.0)
