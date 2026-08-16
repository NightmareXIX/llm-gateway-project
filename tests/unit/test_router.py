"""The failover loop: when to retry, when to move on, and when to stop.

Every test scripts a *sequence* of provider responses through
``httpx.MockTransport`` and asserts on what actually left the process. Faking at
the transport layer rather than behind a fake adapter means the router is driven
by errors that came out of the real ``parse_error`` — the flags it branches on are
the ones production will hand it, not the ones a test author assumed.

What these defend, in order of how expensive the bug is:

**Abort on a non-failover error.** Walking a ``BadRequest`` down three providers
turns one fast failure into three slow ones and burns quota doing it. It is a bug
wearing resilience's clothes, which is why it has to be asserted rather than
assumed.

**Breaker skips must not burn attempts.** Three open breakers at the head of the
chain would otherwise exhaust the budget without a single request being made,
while healthy providers sat at position four — an outage produced entirely by our
own bookkeeping.

**The inversion bug (D11, ADR-014).** A provider that 429s in 80ms is the fastest
thing in the fleet by wall clock. If failures updated the latency table, ``auto``
would learn to prefer whatever is most broken, and the symptom is not an error —
it is a gateway that gets *worse* the longer it runs.

**One budget, and a retry that yields (ADR-015).** Three upstream calls, whether
they went to three candidates or twice to one. A same-provider retry that refused
to yield its last attempt would let one flaky provider starve the failover this
module exists to perform.

Time is injected, so latencies are deterministic and no test sleeps. ``sleep`` is
injected too — the retry backoff is real code and would otherwise cost the suite
a quarter-second per retry test.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import random
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from app.cache import keys
from app.cache.client import LuaScriptRegistry
from app.config import LimitsConfig, ProviderEntry, ProvidersConfig, Slot, SlotCandidate
from app.core.clock import FixedClock
from app.memory.canonical import CanonicalMessage
from app.providers.errors import (
    BadRequest,
    ContentFiltered,
    ContextTooLong,
    EmptyResponse,
    ProviderError,
    RateLimited,
    Unavailable,
)
from app.providers.registry import ProviderRegistry, build_registry
from app.providers.types import GenParams, ModelSpec
from app.quota.tracker import QuotaTracker
from app.routing import router
from app.routing.circuit_breaker import CircuitBreaker
from app.usage.metrics import COMPLETE, MIN_SAMPLES, LatencyTable
from tests import provider_fixtures

PROVIDER = "groq"
FLEET = ("model-a", "model-b", "model-c", "model-d")

# `max_tokens` is set on purpose: `input_budget` reserves the model's whole output
# ceiling otherwise, and the ContextTooLong re-fit below needs room left over
# after narrowing the window to the limit the provider reported.
PARAMS = GenParams(temperature=0.0, max_tokens=512)


def _candidate(model: str) -> SlotCandidate:
    return SlotCandidate(
        provider=PROVIDER,
        model=model,
        context_tokens=131072,
        max_output_tokens=32768,
        capabilities=("text",),
        supports_streaming=True,
    )


FLEET_CONFIG = ProvidersConfig(
    version=1,
    providers={
        PROVIDER: ProviderEntry(
            enabled=True,
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
        )
    },
    slots={
        # Four candidates on one provider, so a chain longer than the attempt cap
        # exists without needing the Step 6 adapters. Every request goes to the
        # same URL and differs only by the `model` field in the body, which is
        # what `ScriptedHandler.models()` reads back.
        "general": Slot(
            description="test slot", candidates=(_candidate("model-a"), _candidate("model-b"))
        ),
        "fast": Slot(
            description="test slot", candidates=(_candidate("model-c"), _candidate("model-d"))
        ),
    },
)


# --------------------------------------------------------------------------- #
# Fixtures and drivers
# --------------------------------------------------------------------------- #
@pytest.fixture
def history() -> list[CanonicalMessage]:
    return provider_fixtures.canonical_history()


@pytest.fixture
def breaker(redis_client: FakeRedis, frozen_clock: FixedClock) -> CircuitBreaker:
    return CircuitBreaker(redis_client, clock=frozen_clock)


def _quota_limits(*, rpm: int = 1, tpm: int = 100_000) -> LimitsConfig:
    """Every fleet model declares the same small budget, real Lua scripts and
    all — real enough that a blocked reservation here is the same one Redis
    would produce in production, not a stand-in for it."""
    model_limits = {
        "rpm": rpm,
        "tpm": tpm,
        "reset": {"rpm": "rolling_60s", "tpm": "rolling_60s"},
    }
    return LimitsConfig.model_validate(
        {
            "version": 1,
            "limits": {PROVIDER: dict.fromkeys(FLEET, model_limits)},
            "gateway": {"free": {"rpm": 20, "rpd": 500}},
        }
    )


@pytest.fixture
def quota(redis_client: FakeRedis, frozen_clock: FixedClock) -> QuotaTracker:
    scripts = LuaScriptRegistry(redis_client)
    scripts.load_dir()
    return QuotaTracker(redis_client, scripts, _quota_limits(), clock=frozen_clock, headroom=0.0)


def _spec_for(model: str) -> ModelSpec:
    """A minimal, standalone :class:`ModelSpec` for driving the tracker directly
    — the exhaustion setup below has no registry in hand yet."""
    return ModelSpec(
        slot="setup",
        provider=PROVIDER,
        model=model,
        context_window=131072,
        max_output_tokens=32768,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


async def exhaust_quota(
    quota: QuotaTracker, model: str, *, request_id: str = "pre-exhausted"
) -> None:
    """Spend one candidate's whole ``rpm`` budget, the way a prior request would
    have — never released, so the window stays exhausted for the test."""
    decision = await quota.reserve(
        _spec_for(model), scope=keys.SYSTEM_SCOPE, estimated_tokens=1, request_id=request_id
    )
    assert decision.allowed, f"test setup failed: could not exhaust {model}'s quota"


def _sse_frame(**event: Any) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def _sse_deltas(*texts: str) -> list[bytes]:
    return [
        _sse_frame(choices=[{"index": 0, "delta": {"content": text}, "finish_reason": None}])
        for text in texts
    ]


def _sse_finished(*, tokens_in: int = 10, tokens_out: int = 5) -> list[bytes]:
    """The terminal chunk plus ``[DONE]`` — a stream that ended because it was
    done, in Groq's ``x_groq.usage`` shape."""
    return [
        _sse_frame(
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            x_groq={
                "usage": {
                    "prompt_tokens": tokens_in,
                    "completion_tokens": tokens_out,
                    "total_tokens": tokens_in + tokens_out,
                }
            },
        ),
        b"data: [DONE]\n\n",
    ]


class _StreamScript:
    """Serves a sequence of streaming upstream attempts, in order.

    The streaming twin of :func:`scripted`, kept local and minimal rather than
    imported from ``test_orchestrator.py``'s ``StreamingHandler`` — that module
    already imports from this one, and a streaming test belongs in this file
    per Step 5's own file list.
    """

    def __init__(self, *scripts: list[bytes | BaseException]) -> None:
        self._scripts = list(scripts)
        self.calls = 0

    def __call__(self, _request: httpx.Request) -> httpx.Response:
        item = self._scripts[self.calls]
        self.calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=provider_fixtures.ScriptedByteStream(item),
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def _no_sleep(_seconds: float) -> None:
    """The backoff, minus the waiting. The delay itself is tested by its own
    function; making twelve tests pay for it in wall clock is not worth it."""
    return None


def scripted(*names: str) -> provider_fixtures.ScriptedHandler:
    """A handler serving these Groq fixtures, in this order."""
    return provider_fixtures.ScriptedHandler(
        *(provider_fixtures.load(PROVIDER, name) for name in names)
    )


def context_too_long(limit: int) -> provider_fixtures.RecordedResponse:
    """A 400 whose prose names a *different* limit from the configured window.

    Synthetic rather than the recorded fixture because the point of the re-fit is
    that the model's real window is smaller than the config claimed, and the
    recording states exactly the configured number.
    """
    return provider_fixtures.RecordedResponse(
        name="context_too_long_narrow",
        source="synthetic",
        status=400,
        headers={"content-type": "application/json"},
        body={
            "error": {
                "message": (
                    f"Please reduce the length of the messages. This model's maximum "
                    f"context length is {limit} tokens."
                ),
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
            }
        },
        text=None,
    )


def registry_for(handler: provider_fixtures.ScriptedHandler) -> ProviderRegistry:
    return build_registry(client=handler.client(), config=FLEET_CONFIG)


async def drive(
    handler: provider_fixtures.ScriptedHandler,
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    *,
    requested: str = "general",
    **kwargs: Any,
) -> router.RouterOutcome:
    return await router.route(
        registry=registry_for(handler),
        breaker=breaker,
        history=history,
        params=PARAMS,
        requested=requested,
        rng=random.Random(0),
        sleep=_no_sleep,
        **kwargs,
    )


@contextlib.contextmanager
def failing_with(expected: type[ProviderError], *, match: str | None = None) -> Iterator[None]:
    """Assert the router gave up, and that the error inside says why.

    ``RoutingFailed`` is the wrapper the endpoint needs, because it carries the
    trail; the normalized class inside it is what ``to_app_error`` maps and what
    these tests are actually about. Asserting on the wrapper alone would pass for
    a router that aborted on the wrong error class.
    """
    with pytest.raises(router.RoutingFailed) as failure:
        yield

    assert isinstance(failure.value.error, expected), (
        f"expected {expected.__name__} inside RoutingFailed, "
        f"got {type(failure.value.error).__name__}"
    )
    if match is not None:
        assert match in str(failure.value.error)


async def open_breaker(breaker: CircuitBreaker, model: str) -> None:
    """Take one candidate's breaker to open, the way the router would."""
    decision = await breaker.allows(PROVIDER, model)
    await breaker.record_failure(
        decision, RateLimited("quota gone", provider=PROVIDER, model=model, status_code=429)
    )


# --------------------------------------------------------------------------- #
# Failing over
# --------------------------------------------------------------------------- #
async def test_a_rate_limited_candidate_fails_over_to_the_next(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    handler = scripted("rate_limited", "success")

    outcome = await drive(handler, breaker, history)

    assert outcome.spec.model == "model-b"
    assert outcome.attempts == 2
    assert handler.models() == ["model-a", "model-b"]
    assert [record.outcome for record in outcome.trail] == ["error", "ok"]
    assert outcome.trail[0].error_code == "rate_limited"


async def test_the_chain_spills_out_of_the_named_slot(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """D10 end to end: `fast` runs out and `general` answers, which is the only
    way `substituted` ever becomes true."""
    handler = scripted("rate_limited", "rate_limited", "success")

    outcome = await drive(handler, breaker, history, requested="fast")

    assert handler.models() == ["model-c", "model-d", "model-a"]
    assert outcome.spec.slot == "general"


async def test_an_auth_failure_fails_over_rather_than_ending_the_request(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """It is an ops problem and it is loud, but a dead key must not take the
    product down while two other providers are standing by."""
    handler = scripted("auth_failed", "success")

    outcome = await drive(handler, breaker, history)

    assert outcome.spec.model == "model-b"


# --------------------------------------------------------------------------- #
# Stopping
# --------------------------------------------------------------------------- #
async def test_a_bad_request_aborts_on_the_first_candidate(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Asserted, not assumed. A payload the provider could not parse is equally
    unparseable everywhere, so walking it down the chain converts one fast failure
    into three slow ones."""
    handler = scripted("bad_request")

    with failing_with(BadRequest):
        await drive(handler, breaker, history)

    assert handler.calls == 1


async def test_a_content_filter_refusal_is_terminal(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Failing over would shop the same prompt around until some model answers it.
    That is laundering a refusal, not resilience."""
    handler = scripted("content_filtered")

    with failing_with(ContentFiltered):
        await drive(handler, breaker, history)

    assert handler.calls == 1


async def test_a_model_the_provider_does_not_serve_aborts(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Groq normalizes a 404 to `BadRequest`: our config is wrong, and trying the
    same wire name on the next candidate would be nonsense."""
    handler = scripted("model_not_found")

    with failing_with(BadRequest):
        await drive(handler, breaker, history)

    assert handler.calls == 1


# --------------------------------------------------------------------------- #
# ContextTooLong — the one failure with a fix
# --------------------------------------------------------------------------- #
async def test_a_context_overflow_refits_against_the_reported_limit(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The retry is honest rather than hopeful: the model's real window is smaller
    than the config claimed, so the second render is fitted to the truth."""
    handler = provider_fixtures.ScriptedHandler(
        context_too_long(8000), provider_fixtures.load(PROVIDER, "success")
    )

    outcome = await drive(handler, breaker, history)

    assert handler.models() == ["model-a", "model-a"]
    assert outcome.spec.context_window == 8000
    assert outcome.attempts == 2


async def test_a_second_context_overflow_gives_up_rather_than_failing_over(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """A history that overflowed here does not fit better on a candidate whose
    window is usually smaller, so there is nothing to fail over to."""
    handler = provider_fixtures.ScriptedHandler(context_too_long(8000), context_too_long(8000))

    with failing_with(ContextTooLong):
        await drive(handler, breaker, history)

    assert handler.calls == 2


async def test_a_context_overflow_with_no_stated_limit_does_not_retry(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Without a number there is nothing to re-fit *to*, and a retry would re-send
    the identical payload for the identical error."""
    handler = provider_fixtures.ScriptedHandler(
        provider_fixtures.RecordedResponse(
            name="context_too_long_vague",
            source="synthetic",
            status=400,
            headers={"content-type": "application/json"},
            body={
                "error": {
                    "message": "Please reduce the length of the messages.",
                    "code": "context_length_exceeded",
                }
            },
            text=None,
        )
    )

    with failing_with(ContextTooLong):
        await drive(handler, breaker, history)

    assert handler.calls == 1


# --------------------------------------------------------------------------- #
# Same-provider retries, and the one budget
# --------------------------------------------------------------------------- #
async def test_a_transient_failure_is_retried_against_the_same_candidate(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    handler = scripted("server_error_html", "success")

    outcome = await drive(handler, breaker, history, pinned=f"{PROVIDER}/model-a")

    assert handler.models() == ["model-a", "model-a"]
    assert outcome.attempts == 2


async def test_a_rate_limit_is_never_retried_against_the_same_candidate(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Hammering a 429 is how a free-tier key gets banned. With a pin there is
    nowhere to fail over to, so the request simply ends."""
    handler = scripted("rate_limited")

    with failing_with(RateLimited):
        await drive(handler, breaker, history, pinned=f"{PROVIDER}/model-a")

    assert handler.calls == 1


async def test_a_retry_yields_its_last_attempt_to_an_untried_candidate(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """ADR-015. One budget of three: with the third attempt the only one left, a
    candidate that has not failed yet is a better bet than a second go at one that
    just did."""
    handler = scripted("rate_limited", "server_error_html", "success")

    outcome = await drive(handler, breaker, history)

    assert handler.models() == ["model-a", "model-b", "model-c"]
    assert outcome.spec.model == "model-c"


async def test_the_retry_goes_ahead_when_nothing_else_remains(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Yielding only makes sense if there is something to yield *to*.

    A pin plus a budget of two puts the retry on the last attempt with an empty
    chain behind it — the exact condition the yield rule tests — and it must go
    ahead rather than give the attempt back to nobody.
    """
    handler = scripted("server_error_html", "success")

    outcome = await drive(handler, breaker, history, pinned=f"{PROVIDER}/model-a", max_attempts=2)

    assert outcome.attempts == 2
    assert handler.models() == ["model-a", "model-a"]


async def test_only_one_retry_per_candidate(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    handler = scripted("server_error_html", "server_error_html")

    with failing_with(Unavailable):
        await drive(handler, breaker, history, pinned=f"{PROVIDER}/model-a")

    assert handler.calls == 2


async def test_an_empty_response_is_retried_once(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    handler = scripted("empty_response", "success")

    outcome = await drive(handler, breaker, history, pinned=f"{PROVIDER}/model-a")

    assert outcome.attempts == 2


async def test_an_empty_response_leaves_the_breaker_closed(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """A free tier returning 200-with-nothing is annoying, not broken. Opening on
    it takes a working provider out of rotation for a whole cooldown."""
    handler = scripted("empty_response", "success")

    await drive(handler, breaker, history, pinned=f"{PROVIDER}/model-a")

    decision = await breaker.allows(PROVIDER, "model-a")
    assert decision.allowed is True
    assert decision.state == "closed"
    assert decision.failures == 0


# --------------------------------------------------------------------------- #
# The attempt cap (D12)
# --------------------------------------------------------------------------- #
async def test_no_more_than_three_requests_leave_the_process(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The fourth candidate exists and is never tried. `ScriptedHandler` raises on
    an unscripted request, so a regression here fails loudly rather than passing
    with an extra round trip."""
    handler = scripted("rate_limited", "rate_limited", "rate_limited")

    with failing_with(RateLimited):
        await drive(handler, breaker, history)

    assert handler.calls == router.MAX_ATTEMPTS


async def test_breaker_skips_do_not_burn_attempts(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Three open breakers still reach candidate four. Truncating the candidate
    list to the cap would produce an outage entirely of our own making."""
    for model in FLEET[:3]:
        await open_breaker(breaker, model)
    handler = scripted("success")

    outcome = await drive(handler, breaker, history)

    assert outcome.spec.model == "model-d"
    assert outcome.attempts == 1
    assert handler.calls == 1
    assert [record.outcome for record in outcome.trail] == [
        "skipped_breaker",
        "skipped_breaker",
        "skipped_breaker",
        "ok",
    ]


async def test_the_trail_can_be_longer_than_the_attempt_count(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Two counters, and they are not the same number: `requests.attempts` holds
    every event, `meta.attempts` holds the round trips."""
    await open_breaker(breaker, "model-a")
    handler = scripted("success")

    outcome = await drive(handler, breaker, history)

    assert len(outcome.trail) == 2
    assert outcome.attempts == 1


async def test_every_breaker_open_produces_a_normalized_failure(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """A fleet-wide outage on the request *after* the one that opened everything.
    It has to arrive as a `ProviderError` so the endpoint's `to_app_error` handles
    it like any other upstream failure."""
    for model in FLEET:
        await open_breaker(breaker, model)
    handler = scripted()

    with failing_with(Unavailable, match="cooldown"):
        await drive(handler, breaker, history)

    assert handler.calls == 0


# --------------------------------------------------------------------------- #
# Quota (Phase 3 Step 5) — the router stops guessing
# --------------------------------------------------------------------------- #
async def test_a_quota_exhausted_candidate_fails_over_without_a_round_trip(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    """The milestone this step exists for: the exhausted candidate receives
    *zero* requests, asserted on the mock transport rather than inferred."""
    await exhaust_quota(quota, "model-a")
    handler = scripted("success")

    outcome = await drive(handler, breaker, history, quota=quota)

    assert outcome.spec.model == "model-b"
    assert outcome.attempts == 1
    assert handler.calls == 1
    assert handler.models() == ["model-b"]
    assert [record.outcome for record in outcome.trail] == ["skipped_quota", "ok"]
    assert outcome.trail[0].blocked_window == "rpm"


async def test_a_chain_of_three_quota_exhausted_candidates_still_reaches_the_fourth(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    """Same reasoning as the breaker's version of this test: three exhausted
    candidates at the head of the chain must not spend the budget of three
    before a healthy provider at position four is ever asked."""
    for model in FLEET[:3]:
        await exhaust_quota(quota, model)
    handler = scripted("success")

    outcome = await drive(handler, breaker, history, quota=quota)

    assert outcome.spec.model == "model-d"
    assert outcome.attempts == 1
    assert handler.calls == 1
    assert [record.outcome for record in outcome.trail] == [
        "skipped_quota",
        "skipped_quota",
        "skipped_quota",
        "ok",
    ]


async def test_requesting_an_exhausted_slot_explicitly_still_gets_an_answer(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    """D2, not the development plan's `slot_unavailable` error: quota knowledge
    only changes *when* the spill happens — before the round trip instead of
    after a 429 — never whether it happens."""
    await exhaust_quota(quota, "model-c")
    await exhaust_quota(quota, "model-d")
    handler = scripted("success")

    outcome = await drive(handler, breaker, history, requested="fast", quota=quota)

    assert outcome.spec.slot == "general"
    assert outcome.spec.model == "model-a"


async def test_a_quota_skip_costs_no_attempt(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    """Trap 8: a quota rejection must not consume one of the three tries a
    message gets, exactly as a breaker skip does not (ADR-015). Two skips ahead
    of two *real* candidates still leaves both of them a genuine attempt — if a
    skip cost one, the second candidate's failure would exhaust the chain
    instead of failing over to the third."""
    await exhaust_quota(quota, "model-a")
    await exhaust_quota(quota, "model-b")
    handler = scripted("rate_limited", "success")

    outcome = await drive(handler, breaker, history, quota=quota)

    assert outcome.spec.model == "model-d"
    assert outcome.attempts == 2
    assert [record.outcome for record in outcome.trail] == [
        "skipped_quota",
        "skipped_quota",
        "error",
        "ok",
    ]


async def test_every_candidate_blocked_on_quota_reports_rate_limited(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    """D17's generalization of `_all_skipped`: a fleet blocked entirely on quota
    is a `RateLimited` with a `Retry-After` a client can use, not the breaker's
    `Unavailable` — the gateway declined to spend a budget it knows is gone,
    which is a different fact from every candidate being in cooldown."""
    for model in FLEET:
        await exhaust_quota(quota, model)
    handler = scripted()

    with failing_with(RateLimited, match="quota"):
        await drive(handler, breaker, history, quota=quota)

    assert handler.calls == 0


async def test_a_mix_of_quota_and_breaker_skips_still_reports_rate_limited(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    """Any quota skip in the trail tips the verdict — a client should not be told
    to wait out a breaker cooldown when the real blocker is its own budget."""
    await open_breaker(breaker, "model-a")
    await exhaust_quota(quota, "model-b")
    await exhaust_quota(quota, "model-c")
    await exhaust_quota(quota, "model-d")
    handler = scripted()

    with failing_with(RateLimited):
        await drive(handler, breaker, history, quota=quota)


async def test_the_skipped_quota_record_serializes_the_blocked_window(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    await exhaust_quota(quota, "model-a")
    handler = scripted("success")

    outcome = await drive(handler, breaker, history, quota=quota)
    skip = outcome.trail[0].to_json()

    assert skip["outcome"] == "skipped_quota"
    assert skip["blocked_window"] == "rpm"
    assert skip["retry_after_s"] == pytest.approx(60.0)


async def test_quota_disabled_behaves_exactly_like_phase_2(
    breaker: CircuitBreaker, history: list[CanonicalMessage], quota: QuotaTracker
) -> None:
    """``quota=None`` is the escape hatch (D15/`QUOTA_ENFORCEMENT`): reservation
    is skipped entirely and an exhausted counter is never consulted."""
    await exhaust_quota(quota, "model-a")
    handler = scripted("success")

    outcome = await drive(handler, breaker, history)  # no `quota=` kwarg

    assert outcome.spec.model == "model-a"
    assert handler.calls == 1


async def test_a_successful_attempt_commits_the_real_token_count(
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    quota: QuotaTracker,
    redis_client: FakeRedis,
) -> None:
    """Success corrects the reservation's estimate to the truth, in either
    direction — asserted against the real counter a Lua script wrote.

    Stripped of its rate-limit headers so Step 6's hint reconciliation makes
    no further correction here; that effect is the next test's whole point,
    and conflating the two would leave neither cleanly isolated.
    """
    fixture = provider_fixtures.load(PROVIDER, "success")
    handler = provider_fixtures.ScriptedHandler(dataclasses.replace(fixture, headers={}))

    await drive(handler, breaker, history, quota=quota)

    used = await redis_client.get(keys.quota(keys.SYSTEM_SCOPE, PROVIDER, "model-a", "tpm"))
    expected = fixture.body["usage"]["total_tokens"]  # type: ignore[index]
    assert used == str(expected)


async def test_a_quota_hint_corrects_the_counter_past_what_commit_wrote(
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    quota: QuotaTracker,
    redis_client: FakeRedis,
) -> None:
    """D18, end to end. The `success` fixture's own
    `x-ratelimit-remaining-tokens` header describes far more cumulative usage
    (other requests against the same real Groq window) than the 75 tokens
    this one call generated — the case a hint exists to fix: the local
    tracker undercounted, and Groq's own number is allowed to move the
    counter up past what the reservation lifecycle alone would ever write.
    """
    handler = scripted("success")

    await drive(handler, breaker, history, quota=quota)

    used = await redis_client.get(keys.quota(keys.SYSTEM_SCOPE, PROVIDER, "model-a", "tpm"))
    fixture = provider_fixtures.load(PROVIDER, "success")
    committed = int(fixture.body["usage"]["total_tokens"])  # type: ignore[index]
    remaining = int(fixture.headers["x-ratelimit-remaining-tokens"])
    hinted = 100_000 - remaining  # `_quota_limits`'s `tpm`, headroom off

    assert hinted > committed
    assert used == str(hinted)


async def test_a_mid_stream_abort_commits_the_tokens_it_really_generated(
    breaker: CircuitBreaker,
    history: list[CanonicalMessage],
    quota: QuotaTracker,
    redis_client: FakeRedis,
) -> None:
    """D1 step 4 / D17 trap 7: a candidate that dies mid-sentence still generated
    text the free tier charged for. The counter has to move on *that*
    candidate, and it has to be exactly the discarded estimate — no more, no
    less, and never lost to the restart that follows it."""
    died: list[bytes | BaseException] = [
        *_sse_deltas("hello there"),
        httpx.RemoteProtocolError("peer closed the connection"),
    ]
    finished: list[bytes | BaseException] = [*_sse_deltas("hi"), *_sse_finished()]
    script = _StreamScript(died, finished)
    registry = build_registry(client=script.client(), config=FLEET_CONFIG)

    events = router.route_stream(
        registry=registry,
        breaker=breaker,
        history=history,
        params=PARAMS,
        requested="general",
        sleep=_no_sleep,
        quota=quota,
    )
    async for _event in events:
        pass

    wasted = len("hello there") // router.DISCARDED_CHARS_PER_TOKEN
    used = await redis_client.get(keys.quota(keys.SYSTEM_SCOPE, PROVIDER, "model-a", "tpm"))
    assert used == str(wasted)
    assert script.calls == 2


# --------------------------------------------------------------------------- #
# The trail
# --------------------------------------------------------------------------- #
async def test_the_trail_serializes_to_the_documented_shape(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """`requests.attempts` is read by Phase 7's dashboard and by whoever is asking
    "why did this answer look weird?". The shape is the contract."""
    handler = scripted("rate_limited", "success")

    outcome = await drive(handler, breaker, history)
    first = outcome.trail[0].to_json()

    assert set(first) == {
        "n",
        "slot",
        "provider",
        "model",
        "outcome",
        "error_code",
        "latency_ms",
        "wasted_tokens_out",
        "breaker",
    }
    assert first["n"] == 1
    assert first["slot"] == "general"
    assert first["provider"] == PROVIDER
    assert first["outcome"] == "error"
    assert first["wasted_tokens_out"] == 0


async def test_a_failed_request_carries_its_whole_trail(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The reason `RoutingFailed` exists. A failure that walked three providers is
    the one `requests` row where "what did it try?" is the entire question, and the
    normalized error names exactly one of them."""
    handler = scripted("rate_limited", "rate_limited", "rate_limited")

    with pytest.raises(router.RoutingFailed) as failure:
        await drive(handler, breaker, history)

    assert failure.value.attempts == 3
    assert [record.model for record in failure.value.trail] == ["model-a", "model-b", "model-c"]
    assert failure.value.spec is not None
    assert failure.value.spec.model == "model-c"


async def test_a_failure_before_any_attempt_names_no_spec(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """Every breaker open, so there is no "last candidate attempted" to report and
    the `requests` row's `served_slot` is honestly null."""
    for model in FLEET:
        await open_breaker(breaker, model)
    handler = scripted()

    with pytest.raises(router.RoutingFailed) as failure:
        await drive(handler, breaker, history)

    assert failure.value.spec is None
    assert failure.value.attempts == 0
    assert len(failure.value.trail) == len(FLEET)


def test_the_wrapper_is_not_a_provider_error() -> None:
    """It is a routing outcome. Making it a `ProviderError` would let it slip
    through an `except ProviderError` written to mean something narrower —
    including the router's own, which would re-enter the loop that raised it."""
    assert not issubclass(router.RoutingFailed, ProviderError)


async def test_a_skip_records_when_the_candidate_is_worth_trying_again(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    await open_breaker(breaker, "model-a")
    handler = scripted("success")

    outcome = await drive(handler, breaker, history)
    skip = outcome.trail[0].to_json()

    assert skip["outcome"] == "skipped_breaker"
    assert skip["retry_after_s"] == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# The latency table (D11) — the inversion bug
# --------------------------------------------------------------------------- #
async def test_only_successful_attempts_update_the_latency_table(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The bug that makes a gateway get worse the longer it runs. A provider that
    429s in 80ms is the fastest thing in the fleet by wall clock, and feeding that
    into the average teaches `auto` to prefer whatever is most broken."""
    metrics = LatencyTable()
    handler = scripted("rate_limited", "success")

    await drive(handler, breaker, history, metrics=metrics)

    assert set(metrics.snapshot(COMPLETE).entries) == {(PROVIDER, "model-b")}


async def test_an_aborted_request_updates_nothing(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    metrics = LatencyTable()
    handler = scripted("bad_request")

    with failing_with(BadRequest):
        await drive(handler, breaker, history, metrics=metrics)

    assert metrics.snapshot(COMPLETE).entries == {}


async def test_the_router_tries_the_faster_candidate_first(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    metrics = LatencyTable()
    for _ in range(MIN_SAMPLES):
        metrics.record(PROVIDER, "model-a", COMPLETE, 900.0)
        metrics.record(PROVIDER, "model-b", COMPLETE, 90.0)
    handler = scripted("success")

    await drive(handler, breaker, history, metrics=metrics)

    assert handler.models() == ["model-b"]


async def test_the_kill_switch_restores_config_order(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """`ROUTING_LATENCY_RANKING` has to be switchable in one deploy, which means it
    has to be a genuine no-op when off."""
    metrics = LatencyTable()
    for _ in range(MIN_SAMPLES):
        metrics.record(PROVIDER, "model-a", COMPLETE, 900.0)
        metrics.record(PROVIDER, "model-b", COMPLETE, 90.0)
    handler = scripted("success")

    await drive(handler, breaker, history, metrics=metrics, rank_by_latency=False)

    assert handler.models() == ["model-a"]


# --------------------------------------------------------------------------- #
# Degraded infrastructure, and the seam
# --------------------------------------------------------------------------- #
async def test_a_dead_redis_still_serves_the_request(
    frozen_clock: FixedClock, history: list[CanonicalMessage]
) -> None:
    """ADR-010's fail-open, from the router's side. The breaker is an optimization;
    an optimization that can take the gateway down is worse than none."""

    class _DeadRedis:
        def __getattr__(self, _name: str) -> Any:
            async def boom(*_args: Any, **_kwargs: Any) -> Any:
                raise ConnectionError("redis is gone")

            return boom

    breaker = CircuitBreaker(_DeadRedis(), clock=frozen_clock)  # type: ignore[arg-type]
    handler = scripted("success")

    outcome = await drive(handler, breaker, history)

    assert outcome.spec.model == "model-a"
    assert outcome.attempts == 1


async def test_a_stream_that_never_produces_a_delta_is_an_empty_response(
    breaker: CircuitBreaker, history: list[CanonicalMessage]
) -> None:
    """The streaming form of the 200-with-nothing free tiers produce all day.

    It has to normalize to the same class the non-streaming path uses, or
    invariant 4 — "an empty generation is an error, not a stored message" — would
    be enforced on one path and quietly not on the other. Asserted here rather
    than through the orchestrator because it is the *classification* under test.
    """
    handler = provider_fixtures.client_from(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=provider_fixtures.ScriptedByteStream([b"data: [DONE]\n\n"]),
        )
    )
    events = router.route_stream(
        registry=build_registry(client=handler, config=FLEET_CONFIG),
        breaker=breaker,
        history=history,
        params=PARAMS,
        requested="general",
        pinned=f"{PROVIDER}/model-a",
        sleep=_no_sleep,
    )

    with pytest.raises(router.RoutingFailed) as failure:
        async for _event in events:
            pass

    assert isinstance(failure.value.error, EmptyResponse)
    # Retryable, so the one pinned candidate is tried twice and then given up on.
    assert failure.value.attempts == 2


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
def test_the_backoff_is_jittered_within_its_ceiling() -> None:
    """Full jitter, because retries that all fire at the same offset
    re-synchronize into the herd the backoff was meant to break up."""
    rng = random.Random(1)
    delays = [router._retry_delay_s(1, rng) for _ in range(50)]

    assert all(0.0 <= delay <= router.RETRY_BASE_DELAY_S for delay in delays)
    assert len(set(delays)) > 1


def test_the_backoff_ceiling_holds() -> None:
    rng = random.Random(1)

    assert router._retry_delay_s(20, rng) <= router.RETRY_MAX_DELAY_S


# --------------------------------------------------------------------------- #
# A guard on the fixtures these tests are built from
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("rate_limited", RateLimited),
        ("server_error_html", Unavailable),
        ("bad_request", BadRequest),
    ],
    ids=["rate_limited", "unavailable", "bad_request"],
)
def test_the_scripted_fixtures_normalize_as_these_tests_assume(
    fixture: str, expected: type[ProviderError]
) -> None:
    """If a fixture's classification ever moves, the router tests above would
    quietly start asserting something else. This is what tells you which one
    broke."""
    from app.providers.groq import GroqAdapter

    adapter = GroqAdapter(
        client=provider_fixtures.client_from(lambda _r: None), base_url="http://x"
    )
    error = adapter.parse_error(provider_fixtures.load(PROVIDER, fixture).to_response())

    assert isinstance(error, expected)
