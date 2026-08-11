"""Contract A's error normalization — the flag matrix and every recorded failure.

This is the highest-value test module in the provider layer. Every routing
decision Phase 2 makes reads the three flags asserted here, so a class that
carries the wrong ones produces a bug that looks like bad luck: requests that
should have failed over silently do not, or malformed payloads get retried
against every provider in the chain.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import InvalidRequest, UpstreamUnavailable
from app.providers.errors import (
    AuthFailed,
    BadRequest,
    ContentFiltered,
    ContextTooLong,
    EmptyResponse,
    ProviderError,
    RateLimited,
    Unavailable,
    to_app_error,
)
from app.providers.groq import GroqAdapter
from tests import provider_fixtures as fx

MODEL = "llama-3.3-70b-versatile"


@pytest.fixture
def adapter() -> GroqAdapter:
    """An adapter with no usable client — every test here is offline by construction."""
    return GroqAdapter(client=httpx.AsyncClient(), base_url="https://api.groq.com/openai/v1")


# --------------------------------------------------------------------------- #
# The flag matrix (§2.1.2)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("error_class", "code", "same", "failover", "breaker"),
    [
        (RateLimited, "rate_limited", False, True, True),
        (Unavailable, "provider_unavailable", True, True, True),
        (AuthFailed, "provider_auth_failed", False, True, True),
        (BadRequest, "bad_request", False, False, False),
        (ContextTooLong, "context_too_long", True, False, False),
        (ContentFiltered, "content_filtered", False, False, False),
        (EmptyResponse, "empty_response", True, True, False),
    ],
)
def test_each_error_class_carries_its_frozen_routing_flags(
    error_class: type[ProviderError],
    code: str,
    same: bool,
    failover: bool,
    breaker: bool,
) -> None:
    assert error_class.code == code
    assert error_class.retryable_same_provider is same
    assert error_class.failover_eligible is failover
    assert error_class.breaker_eligible is breaker


def test_only_auth_failure_is_flagged_as_an_ops_problem() -> None:
    """A dead key does not fix itself; everything else is routine."""
    assert AuthFailed.alert is True
    for other in (RateLimited, Unavailable, BadRequest, ContentFiltered, EmptyResponse):
        assert other.alert is False


def test_context_too_long_is_a_bad_request_subtype() -> None:
    """§2.1.2 calls it 'a BadRequest subtype worth separating'.

    The inheritance matters: a router catching BadRequest to abort the failover
    loop must catch this too, since a longer history does not fit better on the
    next provider either.
    """
    assert issubclass(ContextTooLong, BadRequest)
    assert ContextTooLong.failover_eligible is False


def test_log_fields_name_the_model_and_the_routing_decision() -> None:
    error = RateLimited("slow down", provider="groq", model=MODEL, retry_after_s=12.0)
    fields = error.log_fields()

    assert fields["provider"] == "groq"
    assert fields["model"] == MODEL
    assert fields["error_code"] == "rate_limited"
    assert fields["failover_eligible"] is True
    assert fields["retry_after_s"] == 12.0


def test_str_identifies_which_model_failed() -> None:
    error = Unavailable("upstream is down", provider="groq", model=MODEL)
    assert str(error) == f"[groq/{MODEL}] upstream is down"


# --------------------------------------------------------------------------- #
# Recorded fixtures -> normalized classes (§2.1.5 case 2)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("auth_failed", AuthFailed),
        ("bad_request", BadRequest),
        ("context_too_long", ContextTooLong),
        ("rate_limited", RateLimited),
        ("rate_limited_tpm_413", RateLimited),
        ("model_not_found", BadRequest),
        ("server_error_html", Unavailable),
    ],
)
def test_recorded_error_maps_to_its_normalized_class(
    adapter: GroqAdapter, fixture_name: str, expected: type[ProviderError]
) -> None:
    recorded = fx.load("groq", fixture_name)

    error = adapter.parse_error(recorded.to_response(), model=MODEL)

    assert type(error) is expected
    assert error.provider == "groq"
    assert error.model == MODEL
    assert error.status_code == recorded.status


def test_a_413_for_tokens_per_minute_is_rate_limiting_not_a_context_error(
    adapter: GroqAdapter,
) -> None:
    """The quirk the adapter exists to absorb.

    Groq answers a TPM overage with HTTP 413. Classifying on status alone gives
    ContextTooLong — failover-ineligible — which strands the request on an
    exhausted model while other providers sit idle. Reading the body's `code`
    first is what makes this a RateLimited.
    """
    recorded = fx.load("groq", "rate_limited_tpm_413")

    error = adapter.parse_error(recorded.to_response(), model=MODEL)

    assert isinstance(error, RateLimited)
    assert error.failover_eligible is True
    # From `x-ratelimit-reset-tokens: 1m4.2s`, a Go duration rather than seconds.
    assert error.retry_after_s == pytest.approx(64.2)


def test_context_error_recovers_the_advertised_limit_from_the_prose(
    adapter: GroqAdapter,
) -> None:
    """The number is what the retry re-truncates against, so it is worth parsing."""
    recorded = fx.load("groq", "context_too_long")

    error = adapter.parse_error(recorded.to_response(), model=MODEL)

    assert isinstance(error, ContextTooLong)
    assert error.limit_tokens == 131072


def test_a_429_prefers_the_retry_after_header(adapter: GroqAdapter) -> None:
    recorded = fx.load("groq", "rate_limited")

    error = adapter.parse_error(recorded.to_response(), model=MODEL)

    assert isinstance(error, RateLimited)
    assert error.retry_after_s == 12.0


def test_an_html_error_body_does_not_crash_the_parser(adapter: GroqAdapter) -> None:
    """A proxy in front of Groq answers in HTML.

    `parse_error` runs on the failure path; an exception here would replace a
    clean 502 with a traceback in the catch-all handler.
    """
    recorded = fx.load("groq", "server_error_html")

    error = adapter.parse_error(recorded.to_response(), model=MODEL)

    assert isinstance(error, Unavailable)
    assert error.raw is None


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("too slow"),
        httpx.ReadTimeout("no response"),
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.PoolTimeout("no free connection"),
    ],
)
def test_every_transport_failure_normalizes_to_unavailable(
    adapter: GroqAdapter, exc: Exception
) -> None:
    """A refused connection says nothing about whether the request was sound, so
    the transient reading is the only defensible one."""
    error = adapter.parse_error(exc, model=MODEL)

    assert isinstance(error, Unavailable)
    assert error.failover_eligible is True
    assert error.model == MODEL


def test_an_unrecognized_status_degrades_to_unavailable(adapter: GroqAdapter) -> None:
    """Guessing 'transient' costs one wasted failover; guessing 'permanent' fails
    a request that might have succeeded anywhere else."""
    error = adapter.parse_error(httpx.Response(418, json={}), model=MODEL)

    assert isinstance(error, Unavailable)


def test_parse_error_recovers_the_model_from_the_request_when_not_told(
    adapter: GroqAdapter,
) -> None:
    """Keeps the contract's one-argument form from attributing errors to 'unknown'."""
    request = httpx.Request(
        "POST",
        "https://api.groq.com/openai/v1/chat/completions",
        json={"model": MODEL, "messages": []},
    )
    response = httpx.Response(500, json={"error": {"message": "boom"}}, request=request)

    error = adapter.parse_error(response)

    assert error.model == MODEL


# --------------------------------------------------------------------------- #
# Translation to the client-facing envelope
# --------------------------------------------------------------------------- #
def test_our_own_malformed_payload_becomes_a_502_not_a_400() -> None:
    """A BadRequest means the gateway built a bad body. The caller did nothing
    wrong and has nothing to fix, so a 4xx would be a lie about whose bug it is."""
    app_error = to_app_error(BadRequest("boom", provider="groq", model=MODEL))

    assert isinstance(app_error, UpstreamUnavailable)
    assert app_error.status_code == 502
    assert app_error.code == "bad_request"


@pytest.mark.parametrize(
    ("provider_error", "expected_status"),
    [
        (ContextTooLong("too long", provider="groq", model=MODEL, limit_tokens=131072), 400),
        (ContentFiltered("declined", provider="groq", model=MODEL), 400),
        (RateLimited("slow down", provider="groq", model=MODEL), 502),
        (AuthFailed("bad key", provider="groq", model=MODEL), 502),
        (Unavailable("down", provider="groq", model=MODEL), 502),
        (EmptyResponse("nothing", provider="groq", model=MODEL), 502),
    ],
)
def test_provider_errors_map_onto_the_client_facing_hierarchy(
    provider_error: ProviderError, expected_status: int
) -> None:
    """Only failures a client could act on surface as 4xx."""
    app_error = to_app_error(provider_error)

    assert app_error.status_code == expected_status
    assert app_error.code == provider_error.code


def test_the_providers_own_message_never_reaches_the_client() -> None:
    """Upstream prose is written for whoever owns the account, and can name an
    organization, a key or an internal model. It goes to the log, not the wire."""
    secret = "organization org_01hxyz exceeded its quota"
    app_error = to_app_error(RateLimited(secret, provider="groq", model=MODEL))

    assert secret not in app_error.message
    assert app_error.message


def test_context_overflow_tells_the_client_the_limit() -> None:
    app_error = to_app_error(
        ContextTooLong("too long", provider="groq", model=MODEL, limit_tokens=131072)
    )

    assert isinstance(app_error, InvalidRequest)
    assert app_error.details == {"limit_tokens": 131072}


def test_rate_limiting_forwards_a_retry_after_header() -> None:
    app_error = to_app_error(
        RateLimited("slow down", provider="groq", model=MODEL, retry_after_s=42.7)
    )

    assert app_error.headers["Retry-After"] == "42"
