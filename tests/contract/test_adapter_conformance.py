"""The adapter conformance suite (§2.1.5).

**One parameterized module every adapter must pass.** That is the whole design:
adding a fourth provider in some later phase means implementing an interface and
running a suite that already exists, rather than writing a fresh set of tests and
discovering halfway through which assumptions the third provider quietly broke.

Phase 1 registered one adapter, so the suite mostly proved the harness worked.
Phase 2 Step 6 is where it earns its keep: the same checks now run against Gemini,
whose payload shape, role vocabulary, error envelope and model-in-the-URL all
differ, and against OpenRouter, whose payload is nearly Groq's and whose failure
modes are not. Registering each of them was six lines in ``ADAPTERS`` — the claim
this suite existed to make.

The two streaming cases gate themselves on whether the adapter implements
``stream``, so they skip cleanly now and activate in Step 7 without an edit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from app.providers.base import ProviderAdapter
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
from app.providers.gemini import GeminiAdapter
from app.providers.groq import GroqAdapter
from app.providers.openrouter import OpenRouterAdapter
from app.providers.registry import build_model_specs
from app.providers.types import GenParams, ModelSpec
from tests import provider_fixtures as fx

pytestmark = pytest.mark.contract


@dataclass(frozen=True)
class AdapterCase:
    """One adapter plus everything the suite needs to exercise it."""

    provider: str
    # A callable rather than a `type[...]`: the three adapters are siblings, not
    # subclasses of one another, and `HttpProviderAdapter` deliberately does not
    # satisfy `ProviderAdapter` on its own. Same reasoning as `AdapterFactory`.
    adapter_class: Callable[..., ProviderAdapter]
    base_url: str
    slot: str
    golden: str
    error_fixtures: dict[str, type[ProviderError]]
    # How this provider reports input tokens on a successful response. The literal
    # field names stay here rather than going through `adapter.extract_usage`,
    # because reading the number through the code under test would let a mis-keyed
    # extractor agree with itself. A fixture that drifts from the documented wire
    # shape fails loudly, here.
    reported_input_tokens: Callable[[dict[str, Any]], int]

    def build(self, client: httpx.AsyncClient) -> ProviderAdapter:
        return self.adapter_class(client=client, base_url=self.base_url)

    def spec(self) -> ModelSpec:
        """This provider's candidate in the slot — deliberately not index 0.

        Phase 1 could read ``[0]`` because Groq was the only enabled provider.
        Selecting by provider is what keeps a case independent of the failover
        order in ``providers.yaml``: reordering the candidate list is a routing
        decision, and it should not silently re-point three golden-file tests at
        one provider's model.
        """
        return next(
            spec for spec in build_model_specs()[self.slot] if spec.provider == self.provider
        )


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
        reported_input_tokens=lambda body: int(body["usage"]["prompt_tokens"]),
    ),
    AdapterCase(
        provider="gemini",
        adapter_class=GeminiAdapter,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        slot="general",
        golden="gemini_general",
        error_fixtures={
            "auth_failed": AuthFailed,
            # A dead key is a 400 INVALID_ARGUMENT, not a 401. Read off the status
            # alone it becomes a BadRequest — failover-ineligible — and a revoked
            # key aborts the request instead of falling through to a working one.
            "api_key_invalid": AuthFailed,
            "bad_request": BadRequest,
            "context_too_long": ContextTooLong,
            "rate_limited": RateLimited,
            "model_not_found": BadRequest,
            "unavailable": Unavailable,
            "server_error_html": Unavailable,
        },
        reported_input_tokens=lambda body: int(body["usageMetadata"]["promptTokenCount"]),
    ),
    AdapterCase(
        provider="openrouter",
        adapter_class=OpenRouterAdapter,
        base_url="https://openrouter.ai/api/v1",
        slot="general",
        golden="openrouter_general",
        error_fixtures={
            "auth_failed": AuthFailed,
            # Credit exhaustion is a quota condition. As AuthFailed it would raise
            # the alert flag and page someone about a free tier working as designed.
            "credits_exhausted": RateLimited,
            "bad_request": BadRequest,
            "context_too_long": ContextTooLong,
            "rate_limited": RateLimited,
            "moderated": ContentFiltered,
            "model_not_found": BadRequest,
            # HTTP 200 whose body is an error object. Classified through the same
            # ladder, which is what proves the two callers cannot drift.
            "error_body_200": RateLimited,
            "server_error_html": Unavailable,
        },
        reported_input_tokens=lambda body: int(body["usage"]["prompt_tokens"]),
    ),
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
def test_6_token_estimates_track_reported_usage(
    case: AdapterCase, offline_client: httpx.AsyncClient
) -> None:
    """Estimate the *recorded* prompt, and compare it to that prompt's own usage.

    Earlier this estimated the frozen ``canonical_history()`` and compared the
    result against usage reported for the recorder's much shorter prompt — two
    unrelated prompts, so the comparison passed or failed on how close the two
    happened to be in length rather than on whether the estimator worked. It held
    while every fixture was hand-written and broke the moment a real capture landed
    with a genuine 16-token prompt.

    Using ``request.body`` makes it an actual measurement: this payload produced
    that usage number, and the estimator's job is to have been in the right
    ballpark. The estimate is reconciled against reality at commit time anyway, so
    the bar is "same order of magnitude, never zero", not "matches the tokenizer".
    """
    recorded = fx.load(case.provider, "success")
    assert recorded.body is not None
    assert recorded.request_body is not None, (
        f"{case.provider}/success records no request body, so there is nothing to "
        "estimate; re-record it or add the body by hand"
    )

    adapter = case.build(offline_client)
    estimate = adapter.estimate_tokens(recorded.request_body)
    reported = case.reported_input_tokens(recorded.body)

    assert estimate > 0
    assert reported > 0
    assert 0.25 <= estimate / reported <= 4.0


@CASES
def test_6b_the_estimator_reads_its_own_payload_shape(
    case: AdapterCase, offline_client: httpx.AsyncClient
) -> None:
    """A guard the ratio check above cannot give on its own.

    ``estimate_tokens`` returns ``max(1, …)``, so an estimator pointed at the wrong
    key — Gemini's walking ``messages``, which its payloads do not have — returns
    ``1`` rather than failing. Against a short recorded prompt ``1`` can still land
    inside the ratio band. Scaling with the payload is the property that cannot be
    faked by a stub.
    """
    adapter = case.build(offline_client)
    small = adapter.build_payload(fx.canonical_history()[:2], case.spec(), GenParams(), [])
    large = adapter.build_payload(fx.canonical_history(), case.spec(), GenParams(), [])

    assert adapter.estimate_tokens(large) > adapter.estimate_tokens(small) > 1


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

    # `success` carries the extra obligation of recording what was sent, because
    # test 6 measures the estimator against it.
    assert available["success"].request_body is not None


FORBIDDEN_SUBSTRINGS = ("authorization", "x-goog-api-key", "x-api-key")
"""Header names that carry a credential. Present in a fixture at all means the
recorder's redaction did not hold, whatever the value next to it looks like."""

CREDENTIAL_PREFIXES: dict[str, str] = {
    "gsk_": "gsk_deliberately_invalid",
    # `aizasy`, not `aiza`: the four-character form collides with ordinary
    # base64-ish text and would fail on a fixture that leaked nothing.
    "aizasy": "aizasydeliberatelyinvalid",
    "sk-or-v1-": "sk-or-v1-deliberately-invalid",
}
"""Real key prefix -> the sentinel ``scripts/record_fixtures.py`` writes instead.

The two sides must agree, and cannot import each other (a script depending on the
test package would make recording depend on the suite). If a sentinel changes
there, it changes here.
"""


@CASES
def test_no_fixture_leaks_a_credential(case: AdapterCase) -> None:
    """The recording script redacts on write; this is the check that the redaction
    actually held, run against whatever is on disk today.

    Every prefix is checked against every provider's files, not just its own — a
    Groq key pasted into a Gemini fixture is exactly as leaked, and the
    same-provider-only version of this test would not notice.
    """
    for name in fx.load_all(case.provider):
        raw = (fx.FIXTURE_ROOT / case.provider / f"{name}.json").read_text(encoding="utf-8")
        lowered = raw.lower()

        for header in FORBIDDEN_SUBSTRINGS:
            assert header not in lowered, f"{case.provider}/{name} carries {header}"

        for prefix, sentinel in CREDENTIAL_PREFIXES.items():
            assert prefix not in lowered or sentinel in lowered, (
                f"{case.provider}/{name} looks like it carries a real {prefix!r} key"
            )
