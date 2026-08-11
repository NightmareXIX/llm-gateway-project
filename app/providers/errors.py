"""Contract A's normalized error hierarchy (§2.1.2).

**The router only ever sees these seven classes.** Every provider quirk — a 402
that means "out of credit", a 200 with an empty body, a 429 whose retry hint is
in the body rather than a header — dies inside some adapter's ``parse_error`` and
comes out as one of these. That is the entire reason the failover loop in Phase 2
can be written once instead of three times.

The three routing flags are **class attributes**, not constructor arguments. They
are facts about the *kind* of failure, not about a particular occurrence of it,
and making them per-instance would invite a call site to construct a
``BadRequest`` that claims to be failover-eligible — quietly retrying a malformed
payload against every provider in the chain, which is a bug wearing resilience's
clothes.

| class | same | failover | breaker |
|---|---|---|---|
| ``RateLimited``    | ✗ | ✓ | ✓ |
| ``Unavailable``    | ✓ | ✓ | ✓ |
| ``AuthFailed``     | ✗ | ✓ | ✓ |
| ``BadRequest``     | ✗ | ✗ | ✗ |
| ``ContextTooLong`` | ✓ | ✗ | ✗ |
| ``ContentFiltered``| ✗ | ✗ | ✗ |
| ``EmptyResponse``  | ✓ | ✓ | ✗ |

Two rows are worth reading twice. ``ContentFiltered`` is failover-ineligible
because retrying a safety refusal on another provider is not resilience, it is
laundering — if one model declined, the honest answer is that it declined.
``EmptyResponse`` is breaker-ineligible because a free tier returning 200-with-
nothing is annoying rather than broken, and opening a circuit on it would take a
working provider out of rotation for the rest of the cooldown.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.errors import AppError, InvalidRequest, UpstreamUnavailable


class ProviderError(Exception):
    """Base of the normalized hierarchy. Never raised directly.

    ``raw`` carries the provider's own parsed body when there was one. It exists
    for the log line and the ``requests.attempts`` JSONB array; it must never
    reach a client, because a provider's error prose is written for whoever holds
    the account, not for our users.
    """

    retryable_same_provider: ClassVar[bool] = False
    failover_eligible: ClassVar[bool] = False
    breaker_eligible: ClassVar[bool] = False

    code: ClassVar[str] = "provider_error"
    """Stable identifier, written to ``requests.error_code``. Independent of the
    class name so renaming a class does not rewrite history's meaning."""

    alert: ClassVar[bool] = False
    """Whether an occurrence is an operational problem rather than a routine one.
    Only ``AuthFailed`` sets it: a dead key does not fix itself."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str,
        status_code: int | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.raw = raw
        super().__init__(message)

    def __str__(self) -> str:
        return f"[{self.provider}/{self.model}] {super().__str__()}"

    def log_fields(self) -> dict[str, Any]:
        """The structured fields every provider failure is logged with."""
        return {
            "provider": self.provider,
            "model": self.model,
            "error_code": self.code,
            "status_code": self.status_code,
            "retryable_same_provider": self.retryable_same_provider,
            "failover_eligible": self.failover_eligible,
            "breaker_eligible": self.breaker_eligible,
        }


class RateLimited(ProviderError):
    """429, or a 200/402 whose body says the quota is gone.

    Not retryable against the same provider — the whole point of the quota
    tracker is that we should have predicted this, and hammering the same model
    is how a free-tier key gets banned.
    """

    retryable_same_provider = False
    failover_eligible = True
    breaker_eligible = True
    code = "rate_limited"

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str,
        retry_after_s: float | None = None,
        status_code: int | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(message, provider=provider, model=model, status_code=status_code, raw=raw)

    def log_fields(self) -> dict[str, Any]:
        return {**super().log_fields(), "retry_after_s": self.retry_after_s}


class Unavailable(ProviderError):
    """5xx, connection reset, timeout, or a stream that stalled mid-flight.

    The catch-all for "this might work in a moment", and deliberately the most
    permissive class: retryable, failover-eligible, breaker-eligible. Anything
    unrecognized normalizes to this, because treating an unknown failure as
    transient degrades gracefully while treating it as permanent does not.
    """

    retryable_same_provider = True
    failover_eligible = True
    breaker_eligible = True
    code = "provider_unavailable"


class AuthFailed(ProviderError):
    """401/403 — the key is invalid, revoked, or lacks access to this model.

    Failover-eligible so a dead key does not take the product down, but it is an
    ops problem and must be loud: ``alert`` is set, and the caller is expected to
    log it at error level rather than warning.
    """

    retryable_same_provider = False
    failover_eligible = True
    breaker_eligible = True
    code = "provider_auth_failed"
    alert = True


class BadRequest(ProviderError):
    """400 — we sent something malformed. Our bug, not theirs.

    Neither retryable nor failover-eligible, and that is the important part: a
    payload the provider could not parse will be equally unparseable everywhere,
    so retrying it down the whole candidate chain converts one fast failure into
    three slow ones and burns quota doing it.
    """

    retryable_same_provider = False
    failover_eligible = False
    breaker_eligible = False
    code = "bad_request"


class ContextTooLong(BadRequest):
    """The assembled prompt exceeds the model's window.

    A ``BadRequest`` subtype worth separating because it has a *fix*: re-run the
    fitting step (D4) against ``limit_tokens`` and retry once against the same
    provider. Still not failover-eligible — a bigger history does not fit better
    somewhere else, and the next model in the chain usually has a smaller window,
    not a larger one.
    """

    retryable_same_provider = True
    failover_eligible = False
    breaker_eligible = False
    code = "context_too_long"

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str,
        limit_tokens: int | None = None,
        status_code: int | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.limit_tokens = limit_tokens
        super().__init__(message, provider=provider, model=model, status_code=status_code, raw=raw)

    def log_fields(self) -> dict[str, Any]:
        return {**super().log_fields(), "limit_tokens": self.limit_tokens}


class ContentFiltered(ProviderError):
    """The provider refused on safety grounds.

    Terminal on purpose. Failing over would shop the same prompt around until
    some model answers it, which is not a resilience feature.
    """

    retryable_same_provider = False
    failover_eligible = False
    breaker_eligible = False
    code = "content_filtered"


class EmptyResponse(ProviderError):
    """HTTP 200 with nothing usable in it.

    Matters more than it looks. Free-tier endpoints return this often enough that
    without a class for it the gateway ships empty assistant messages and the
    model gets the blame. Retryable once and failover-eligible, but *not*
    breaker-eligible — see the module docstring.
    """

    retryable_same_provider = True
    failover_eligible = True
    breaker_eligible = False
    code = "empty_response"


# --------------------------------------------------------------------------- #
# Translation to the client-facing hierarchy
# --------------------------------------------------------------------------- #
_CLIENT_FACING_MESSAGE: dict[str, str] = {
    "bad_request": "The request could not be processed by the model provider.",
    "context_too_long": "This conversation is too long for the selected model.",
    "content_filtered": "The model declined to answer this request.",
    "rate_limited": "The model provider is rate limiting this gateway. Try again shortly.",
    "provider_auth_failed": "The gateway could not authenticate with the model provider.",
    "provider_unavailable": "The model provider is currently unavailable.",
    "empty_response": "The model returned an empty response.",
}


def to_app_error(exc: ProviderError) -> AppError:
    """Map a normalized provider failure onto the client-facing envelope.

    Called by the chat endpoint (Step 8) once the router has exhausted every
    candidate, so translation lives here rather than being re-derived at each
    call site.

    Two rules shape the mapping. **The provider's own prose never survives** —
    ``exc`` carries a message written for whoever owns the upstream account, and
    a substituted message from this table is what the caller actually sees; the
    original goes to the log with the ``request_id``. And **only our own bugs
    become 4xx**: a ``BadRequest`` means the gateway built a bad payload, which
    a client cannot fix, but ``ContextTooLong`` and ``ContentFiltered`` genuinely
    are about what was asked, so they surface as 400s the user can act on.
    """
    message = _CLIENT_FACING_MESSAGE.get(exc.code, "The request could not be completed.")

    if isinstance(exc, ContextTooLong):
        details: dict[str, Any] = {}
        if exc.limit_tokens is not None:
            details["limit_tokens"] = exc.limit_tokens
        return InvalidRequest(message, code=exc.code, details=details)

    if isinstance(exc, ContentFiltered):
        return InvalidRequest(message, code=exc.code)

    if isinstance(exc, BadRequest):
        # Deliberately a 502, not the 400 the upstream status suggests. The
        # malformed payload was ours; the caller did nothing wrong and has
        # nothing to correct.
        return UpstreamUnavailable(message, code=exc.code)

    if isinstance(exc, RateLimited):
        headers = (
            {"Retry-After": str(int(exc.retry_after_s))} if exc.retry_after_s is not None else None
        )
        return UpstreamUnavailable(message, code=exc.code, headers=headers)

    return UpstreamUnavailable(message, code=exc.code)
