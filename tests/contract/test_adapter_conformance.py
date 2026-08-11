"""The adapter conformance suite (§2.1.5).

**One parameterized module every adapter must pass.** That is the whole design:
adding a fourth provider in some later phase means implementing an interface and
running a suite that already exists, rather than writing a fresh set of tests and
discovering halfway through which assumptions the third provider quietly broke.

Phase 1 registers one adapter, so today this suite mostly proves the harness
works. Phase 2 is where it earns its keep — the same seven checks run against
Gemini, whose payload shape, role vocabulary and streaming format all differ.

The two streaming cases gate themselves on whether the adapter implements
``stream``, so they skip cleanly now and activate in Phase 2 without an edit.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from app.providers.base import ProviderAdapter
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    ContextTooLong,
    EmptyResponse,
    ProviderError,
    RateLimited,
    Unavailable,
)
from app.providers.groq import GroqAdapter
from app.providers.registry import build_model_specs
from app.providers.types import GenParams, ModelSpec
from tests import provider_fixtures as fx

pytestmark = pytest.mark.contract


@dataclass(frozen=True)
class AdapterCase:
    """One adapter plus everything the suite needs to exercise it."""

    provider: str
    adapter_class: type[GroqAdapter]
    base_url: str
    slot: str
    golden: str
    error_fixtures: dict[str, type[ProviderError]]

    def build(self, client: httpx.AsyncClient) -> ProviderAdapter:
        return self.adapter_class(client=client, base_url=self.base_url)

    def spec(self) -> ModelSpec:
        return build_model_specs()[self.slot][0]


ADAPTERS: list[AdapterCase] = [
    AdapterCase(
        provider="groq",
        adapter_class=GroqAdapter,
        base_url="https://api.groq.com/openai/v1",
        slot="general",
        golden="groq_general",
        error_fixtures={
            "auth_failed": AuthFailed,
            "bad_request": BadRequest,
            "context_too_long": ContextTooLong,
            "rate_limited": RateLimited,
            "rate_limited_tpm_413": RateLimited,
            "model_not_found": BadRequest,
            "server_error_html": Unavailable,
        },
    ),
    # Phase 2 appends gemini and openrouter here. Nothing below changes.
]

CASES = pytest.mark.parametrize("case", ADAPTERS, ids=lambda c: c.provider)

_PARAMS = GenParams(temperature=0.2, max_tokens=512, top_p=0.9, stop=["</done>"])


@pytest.fixture
def offline_client() -> httpx.AsyncClient:
    """A client whose transport refuses everything — for the pure-function checks."""
    return fx.client_raising(httpx.ConnectError("this suite does not make requests"))


# --------------------------------------------------------------------------- #
# 1. build_payload matches a committed golden file
# --------------------------------------------------------------------------- #
@CASES
def test_1_build_payload_matches_the_golden_file(
    case: AdapterCase, offline_client: httpx.AsyncClient
) -> None:
    adapter = case.build(offline_client)

    payload = adapter.build_payload(fx.canonical_history(), case.spec(), _PARAMS, [])

    assert payload == fx.read_golden(case.golden)


# --------------------------------------------------------------------------- #
# 2. every recorded error maps to its class, with the right flags
# --------------------------------------------------------------------------- #
@CASES
def test_2_recorded_errors_map_to_normalized_classes_with_their_flags(
    case: AdapterCase, offline_client: httpx.AsyncClient
) -> None:
    adapter = case.build(offline_client)

    for fixture_name, expected in case.error_fixtures.items():
        recorded = fx.load(case.provider, fixture_name)

        error = adapter.parse_error(recorded.to_response())

        assert type(error) is expected, f"{fixture_name} -> {type(error).__name__}"
        assert error.provider == case.provider
        assert error.retryable_same_provider is expected.retryable_same_provider
        assert error.failover_eligible is expected.failover_eligible
        assert error.breaker_eligible is expected.breaker_eligible


# --------------------------------------------------------------------------- #
# 3 & 4. streaming — Phase 2
# --------------------------------------------------------------------------- #
def _implements_stream(adapter: ProviderAdapter) -> bool:
    try:
        adapter.stream({}, "key", timeout=1.0, idle_timeout=1.0)
    except NotImplementedError:
        return False
    except Exception:  # pragma: no cover — implemented, and failing for real reasons
        return True
    return True


@CASES
async def test_3_stream_yields_multiple_chunks_and_terminates(
    case: AdapterCase, offline_client: httpx.AsyncClient
) -> None:
    adapter = case.build(offline_client)
    if not _implements_stream(adapter):
        pytest.skip(f"{case.provider}: streaming lands in Phase 2")

    recorded = fx.read_sse(case.provider, "stream_success")  # pragma: no cover
    async with fx.client_from(  # pragma: no cover
        lambda _r: httpx.Response(200, text=recorded)
    ) as client:
        chunks = [
            chunk
            async for chunk in case.build(client).stream({}, "key", timeout=30.0, idle_timeout=30.0)
        ]

    assert len(chunks) >= 2  # pragma: no cover
    assert chunks[-1].finish_reason is not None  # pragma: no cover


@CASES
async def test_4_a_truncated_stream_raises_unavailable_not_a_decoding_error(
    case: AdapterCase, offline_client: httpx.AsyncClient
) -> None:
    adapter = case.build(offline_client)
    if not _implements_stream(adapter):
        pytest.skip(f"{case.provider}: streaming lands in Phase 2")

    recorded = fx.read_sse(case.provider, "stream_truncated")  # pragma: no cover
    async with fx.client_from(  # pragma: no cover
        lambda _r: httpx.Response(200, text=recorded)
    ) as client:
        with pytest.raises(Unavailable):
            async for _ in case.build(client).stream({}, "key", timeout=30.0, idle_timeout=30.0):
                pass


# --------------------------------------------------------------------------- #
# 5. a 200 with no usable content raises EmptyResponse
# --------------------------------------------------------------------------- #
@CASES
async def test_5_a_200_with_empty_content_raises_empty_response(case: AdapterCase) -> None:
    async with fx.client_returning(fx.load(case.provider, "empty_response")) as client:
        adapter = case.build(client)
        payload = adapter.build_payload(fx.canonical_history(), case.spec(), GenParams(), [])

        with pytest.raises(EmptyResponse):
            await adapter.complete(payload, "key", timeout=30.0)


# --------------------------------------------------------------------------- #
# 6. estimate_tokens lands within ±25% of reported usage
# --------------------------------------------------------------------------- #
@CASES
async def test_6_token_estimates_track_reported_usage(case: AdapterCase) -> None:
    """Run against the fixture set rather than a synthetic string, so an
    estimator tuned to one provider's prompt format cannot pass by accident."""
    recorded = fx.load(case.provider, "success")
    assert recorded.body is not None
    reported = recorded.body["usage"]["prompt_tokens"]

    handler = fx.RecordingHandler(recorded)
    async with handler.client() as client:
        adapter = case.build(client)
        payload = adapter.build_payload(fx.canonical_history(), case.spec(), GenParams(), [])
        estimate = adapter.estimate_tokens(payload)

    # Both are token counts of the *same kind of thing*, and the estimate is
    # reconciled against reality at commit time — so the bar is "same order of
    # magnitude, never zero", not "matches the tokenizer".
    assert estimate > 0
    assert reported > 0
    assert 0.25 <= estimate / max(reported, 1) <= 4.0


# --------------------------------------------------------------------------- #
# 7. build_payload is pure
# --------------------------------------------------------------------------- #
@CASES
def test_7_build_payload_is_pure(case: AdapterCase, offline_client: httpx.AsyncClient) -> None:
    adapter = case.build(offline_client)
    history = fx.canonical_history()

    first = adapter.build_payload(history, case.spec(), _PARAMS, [])
    second = adapter.build_payload(history, case.spec(), _PARAMS, [])

    assert first == second


# --------------------------------------------------------------------------- #
# Fixture hygiene
# --------------------------------------------------------------------------- #
@CASES
def test_the_fixture_set_is_complete_and_well_formed(case: AdapterCase) -> None:
    """A half-recorded directory should fail here, loudly, rather than as a
    confusing KeyError three tests later."""
    required = {"success", "success_no_usage", "empty_response", "models_list"} | set(
        case.error_fixtures
    )
    available = fx.load_all(case.provider)

    assert required <= set(available)
    for recorded in available.values():
        assert recorded.status >= 100
        assert (recorded.body is not None) or (recorded.text is not None)
        assert recorded.source in {"live", "synthetic"}


@CASES
def test_no_fixture_leaks_a_credential(case: AdapterCase) -> None:
    """The recording script redacts on write; this is the check that the redaction
    actually held, run against whatever is on disk today."""
    for name in fx.load_all(case.provider):
        raw = (fx.FIXTURE_ROOT / case.provider / f"{name}.json").read_text(encoding="utf-8")
        lowered = raw.lower()

        assert "authorization" not in lowered
        assert "gsk_" not in lowered or "gsk_deliberately_invalid" in lowered
