"""The OpenRouter adapter — OpenAI-shaped, with teeth.

OpenRouter aggregates dozens of upstreams behind one OpenAI-compatible API, which
makes its *payload* the least interesting adapter in the pool and its *failure
modes* the most. It is a proxy in front of providers it does not control, so
availability changes without notice and a 200 does not mean the request worked.

Nothing here is shared with ``groq.py`` even though the payload shapes match.
That is deliberate: the two providers agree on the request body and disagree on
almost everything else, and a shared base would need a per-provider flag on the
first divergence — the leak ``base.py``'s docstring warns about. The duplication
is visible; a flag pretending to be an abstraction would not be.

Six quirks live here rather than in a comment somewhere:

**The ``:free`` suffix is part of the model name.** ``deepseek/deepseek-r1:free``
and ``deepseek/deepseek-r1`` are different models with different billing. Dropping
the suffix silently routes to the paid variant, which on a project whose premise is
"runs entirely on free tiers" is the worst kind of silent success. It needs no code
here — it rides through as part of ``spec.model`` — and a config test asserts every
declared OpenRouter model still carries it.

**402 is a rate limit, not an auth failure.** Credit exhaustion is a quota
condition. Classifying it as :class:`AuthFailed` would set the ``alert`` flag and
page someone about a free tier working exactly as designed.

**A 200 can carry an error.** OpenRouter answers some upstream failures with HTTP
200 and a top-level ``error`` object instead of ``choices``. Reading that as
"empty response" would record the wrong error code in the attempt trail *and* skip
the breaker, since :class:`EmptyResponse` is deliberately breaker-ineligible.

**``error.code`` is an integer.** Every other provider in the pool sends a string.

**A 403 may be moderation.** ``error.metadata.reasons`` distinguishes "your input
was flagged" from "your key is bad" — the same page-someone argument as the 402.

**The reset header is a unix timestamp in milliseconds**, not a duration and not a
delta, so converting it needs to know the current time. Hence the injected clock.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar, TypeGuard

import httpx

from app.core.clock import SYSTEM_CLOCK, Clock
from app.core.logging import get_logger
from app.memory.canonical import CanonicalMessage
from app.memory.render import document_envelope
from app.providers.base import HttpProviderAdapter
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
from app.providers.types import (
    Completion,
    FinishReason,
    GenParams,
    KeyValidation,
    ModelSpec,
    QuotaHint,
    ResolvedAttachment,
    Usage,
)

logger = get_logger("app.providers.openrouter")

CHAT_COMPLETIONS_PATH = "/chat/completions"
MODELS_PATH = "/models"

VALIDATE_KEY_TIMEOUT_S = 10.0
"""A liveness check that takes longer than this has answered the question."""

CHARS_PER_TOKEN = 4
PER_MESSAGE_TOKEN_OVERHEAD = 4

REMAINING_HEADER = "x-ratelimit-remaining"
RESET_HEADER = "x-ratelimit-reset"

_MS_EPOCH_FLOOR = 1e11
"""Above this, a value is milliseconds since the epoch (~1973 in seconds)."""

_S_EPOCH_FLOOR = 1e9
"""Above this but below the ms floor, seconds since the epoch (~2001)."""

_CREDIT_CODES = frozenset({"402", "insufficient_credits", "insufficient_quota"})
_RATE_LIMIT_CODES = frozenset({"429", "rate_limit_exceeded", "rate_limited"})
_CONTEXT_ERROR_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})
_CONTENT_FILTER_CODES = frozenset({"content_filter", "content_policy_violation"})

_CONTEXT_ERROR_PHRASES = (
    "reduce the length",
    "context length",
    "maximum context",
    "too long for model",
    "token limit",
)

_LIMIT_TOKENS_RE = re.compile(
    r"(?:maximum context length is|context length of|limit is|limit)\s+(\d[\d_,]*)",
    re.IGNORECASE,
)

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "error": "error",
}


class OpenRouterAdapter(HttpProviderAdapter):
    """Satisfies :class:`~app.providers.base.ProviderAdapter` structurally."""

    name: ClassVar[str] = "openrouter"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        options: Mapping[str, str] | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        """``clock`` is an addition, and not a contract change.

        ``AdapterFactory.__call__`` is ``(*, client, base_url, options)``; an extra
        *defaulted* keyword still satisfies it, so the registry constructs this
        adapter exactly as it constructs the other two. It exists because
        :meth:`rate_limit_headers` has to turn a unix timestamp into a delta, and
        the project's standing rule is that anything comparing timestamps takes a
        :class:`~app.core.clock.Clock` rather than reaching for ``time.time()`` —
        which is also what makes that conversion testable without waiting.
        """
        super().__init__(client=client, base_url=base_url, options=options)
        self._clock = clock

    # ----------------------------------------------------------------- #
    # Payload construction — pure
    # ----------------------------------------------------------------- #
    def build_payload(
        self,
        messages: list[CanonicalMessage],
        spec: ModelSpec,
        params: GenParams,
        attachments: list[ResolvedAttachment],
    ) -> dict[str, Any]:
        """Canonical history → an OpenAI-shaped chat completion body.

        Pure: the output depends on nothing but the four arguments, which is what
        makes the golden-file test meaningful.

        OpenRouter's ``supports_system_field`` is ``False`` for every model, so the
        system prompt stays as the first element of ``messages``. The ``:free``
        suffix needs no handling — it is part of ``spec.model`` and reaches the wire
        verbatim, which is precisely the property a test pins.
        """
        by_hash = {attachment.file_hash: attachment for attachment in attachments}

        rendered = [
            {
                "role": message.role,
                "content": _render_content(message, by_hash),
            }
            for message in messages
        ]

        payload: dict[str, Any] = {
            "model": spec.model,
            "messages": rendered,
            "temperature": params.temperature,
            "stream": params.stream,
        }

        if params.max_tokens is not None:
            # Clamped rather than passed through: a caller asking for more output
            # than the model can produce gets a 400, and turning a config-knowable
            # failure into a round trip wastes a request.
            payload["max_tokens"] = min(params.max_tokens, spec.max_output_tokens)
        if params.top_p is not None:
            payload["top_p"] = params.top_p
        if params.stop:
            payload["stop"] = list(params.stop)

        return payload

    # ----------------------------------------------------------------- #
    # Execution
    # ----------------------------------------------------------------- #
    async def complete(self, payload: dict[str, Any], key: str, timeout: float) -> Completion:
        model = str(payload.get("model", "unknown"))

        response = await self._request(
            "POST",
            CHAT_COMPLETIONS_PATH,
            headers=self._auth_headers(key),
            json=payload,
            timeout_s=timeout,
            model=model,
        )

        if response.status_code >= 400:
            error = self.parse_error(response, model=model)
            logger.warning("provider.request_failed", **error.log_fields())
            raise error

        body = _json_object(response)
        if body is None:
            raise EmptyResponse(
                "response body was not a JSON object",
                provider=self.name,
                model=model,
                status_code=response.status_code,
            )

        text, finish_reason = self._read_choice(body, status=response.status_code, model=model)

        usage = self.extract_usage(body)
        if usage.estimated:
            # `extract_usage` only sees the response and so cannot know the input
            # size. This is the one place that holds both halves.
            usage = replace(usage, tokens_in=self.estimate_tokens(payload))

        raw_id = body.get("id")
        return Completion(
            text=text,
            usage=usage,
            finish_reason=finish_reason,
            raw_id=raw_id if isinstance(raw_id, str) else None,
        )

    def _read_choice(
        self, body: dict[str, Any], *, status: int, model: str
    ) -> tuple[str, FinishReason]:
        """Pull the generated text out of a 200, or say why there isn't any."""
        # A 200 whose body is an error object. Falling through to the
        # no-choices branch below would record `empty_response` in the attempt
        # trail — the wrong error code, and one that is deliberately *not*
        # breaker-eligible, so a provider returning these all day would never get
        # taken out of rotation.
        if isinstance(body.get("error"), dict):
            error = self._classify(status=status, body=body, model=model, response=None)
            logger.warning("provider.error_in_success_body", **error.log_fields())
            raise error

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise EmptyResponse(
                "response carried no choices", provider=self.name, model=model, raw=body
            )

        choice = choices[0]
        if not isinstance(choice, dict):
            raise EmptyResponse(
                "malformed choice object", provider=self.name, model=model, raw=body
            )

        raw_finish = choice.get("finish_reason")
        if raw_finish == "content_filter":
            raise ContentFiltered(
                "the model declined to generate a response",
                provider=self.name,
                model=model,
                raw=body,
            )

        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None

        # `not content.strip()` and not merely `not content`: a completion of pure
        # whitespace is the same non-answer as an empty one. OpenRouter's upstreams
        # produce both, more often than a first-party API would.
        if not isinstance(content, str) or not content.strip():
            raise EmptyResponse(
                "response carried no usable content",
                provider=self.name,
                model=model,
                raw=body,
            )

        finish_reason = _FINISH_REASONS.get(raw_finish, "stop") if raw_finish else "stop"
        return content, finish_reason

    # ----------------------------------------------------------------- #
    # Normalization
    # ----------------------------------------------------------------- #
    def parse_error(
        self, exc: Exception | httpx.Response, *, model: str | None = None
    ) -> ProviderError:
        """Every OpenRouter failure mode, collapsed into the seven normalized classes.

        Never raises. It runs on the failure path, and an exception escaping here
        would replace a clean 502 with a traceback in the catch-all handler.
        """
        try:
            if isinstance(exc, httpx.Response):
                return self._classify(
                    status=exc.status_code,
                    body=_json_object(exc),
                    model=model,
                    response=exc,
                )
            return self._error_from_exception(exc, model=model)
        except Exception:  # pragma: no cover — belt and braces around a defensive path
            logger.exception("provider.parse_error_failed", provider=self.name)
            return Unavailable(
                "provider failure could not be classified",
                provider=self.name,
                model=model or "unknown",
            )

    def _error_from_exception(self, exc: Exception, *, model: str | None) -> ProviderError:
        """Transport-level failures — nothing here ever produced a response.

        All of them are :class:`Unavailable`: a refused connection carries no
        information about whether the request itself was sound, so the transient
        reading is the only defensible one.
        """
        if isinstance(exc, httpx.TimeoutException):
            detail = "request to openrouter timed out"
        elif isinstance(exc, httpx.ConnectError):
            detail = "could not connect to openrouter"
        elif isinstance(exc, httpx.RemoteProtocolError):
            detail = "openrouter closed the connection mid-response"
        else:
            detail = f"transport failure talking to openrouter: {type(exc).__name__}"

        return Unavailable(detail, provider=self.name, model=model or "unknown")

    def _classify(
        self,
        *,
        status: int,
        body: dict[str, Any] | None,
        model: str | None,
        response: httpx.Response | None,
    ) -> ProviderError:
        """One ladder, two callers.

        Both a genuine HTTP error and a 200 carrying an ``error`` object come
        through here, which is what stops the two from drifting. ``response`` is
        ``None`` for the 200 case — there are no rate-limit headers worth reading
        on a response that also carried a body we are treating as an error, and the
        body's own ``code`` is a better classifier than the status anyway.
        """
        code, message, metadata = _read_error_fields(body)
        resolved_model = model or (_model_from_request(response) if response else None) or "unknown"

        common: dict[str, Any] = {
            "provider": self.name,
            "model": resolved_model,
            "status_code": status,
            "raw": body,
        }
        detail = message or f"openrouter returned HTTP {status}"

        # The body's `code` is consulted before the status, because on the
        # 200-with-an-error path there is no meaningful status to read, and on the
        # error path OpenRouter's code is the more specific of the two.
        if code in _CREDIT_CODES:
            # Credit exhaustion is a quota condition. `AuthFailed` would fire the
            # `alert` flag and page someone about a free tier working as designed.
            return RateLimited(detail, retry_after_s=self._read_retry_after(response), **common)
        if code in _RATE_LIMIT_CODES:
            return RateLimited(detail, retry_after_s=self._read_retry_after(response), **common)
        if code in _CONTEXT_ERROR_CODES:
            return ContextTooLong(detail, limit_tokens=_read_limit_tokens(message), **common)
        if code in _CONTENT_FILTER_CODES:
            return ContentFiltered(detail, **common)

        if status == 402:
            return RateLimited(detail, retry_after_s=self._read_retry_after(response), **common)
        if status == 403 and _looks_like_moderation(metadata):
            # Ahead of the 401/403 → AuthFailed rule below. A moderation refusal is
            # not a credential problem, and treating it as one would page someone
            # *and* launder the refusal onto the next provider.
            return ContentFiltered(detail, **common)
        if status in (401, 403):
            return AuthFailed(detail, **common)
        if status == 429:
            return RateLimited(detail, retry_after_s=self._read_retry_after(response), **common)
        if status in (400, 422):
            if _looks_like_context_overflow(message):
                return ContextTooLong(detail, limit_tokens=_read_limit_tokens(message), **common)
            return BadRequest(detail, **common)
        if status == 404:
            # "No endpoints found for <model>". Our config names something
            # OpenRouter does not route, so it is our bug — trying the same wire
            # name on the next provider would be nonsense.
            return BadRequest(detail, **common)
        if status >= 500:
            return Unavailable(detail, **common)

        # Unrecognized. `Unavailable` rather than `BadRequest`: guessing
        # "transient" costs one wasted failover attempt, guessing "permanent" fails
        # a request that might have succeeded anywhere else. OpenRouter proxies
        # providers it does not control, so the transient reading is the likelier
        # one here even more than elsewhere.
        return Unavailable(detail, **common)

    def extract_usage(self, response_body: dict[str, Any]) -> Usage:
        """Read OpenRouter's usage block, or flag that there wasn't one.

        The fallback deliberately reports ``tokens_in=0`` with ``estimated=True``
        rather than guessing: this method never sees the request. :meth:`complete`
        fills the input side in, and Phase 3's tracker keys on ``estimated`` to
        decide whether a counter can be trusted as ground truth.
        """
        usage = response_body.get("usage")
        if isinstance(usage, dict):
            tokens_in = usage.get("prompt_tokens")
            tokens_out = usage.get("completion_tokens")
            if _is_int(tokens_in) and _is_int(tokens_out):
                return Usage(tokens_in=tokens_in, tokens_out=tokens_out, estimated=False)

        return Usage(tokens_in=0, tokens_out=_estimate_output_tokens(response_body), estimated=True)

    def estimate_tokens(self, payload: dict[str, Any]) -> int:
        """A characters-over-four estimate of the payload's input size.

        Used to size a quota reservation before the call, when no real count exists
        yet. Reconciled against reported usage at commit time, so being cheap
        matters more than being exact.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return 1

        total = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                total += len(content) // CHARS_PER_TOKEN
            total += PER_MESSAGE_TOKEN_OVERHEAD

        return max(1, total)

    # ----------------------------------------------------------------- #
    # Operations
    # ----------------------------------------------------------------- #
    async def validate_key(self, key: str) -> KeyValidation:
        """List models, which is the cheapest call OpenRouter offers.

        Honest caveat: ``/models`` is a *public* catalogue and answers without a
        key at all, so a 200 proves the service is reachable and the key was not
        actively rejected — it does not prove the key can spend. A 1-token
        completion would prove more and cost quota on every BYOK add; that trade is
        Phase 6's to revisit if the weaker check turns out to admit bad keys.
        """
        response = await self._request(
            "GET",
            MODELS_PATH,
            headers=self._auth_headers(key),
            timeout_s=VALIDATE_KEY_TIMEOUT_S,
        )

        if response.status_code in (401, 403):
            return KeyValidation(
                valid=False,
                provider=self.name,
                detail=(
                    "OpenRouter rejected this key. "
                    "Check that it is active and has not been revoked."
                ),
            )

        if response.status_code >= 400:
            # Not `valid=False`: we did not learn anything about the key. §9.2 is
            # explicit that telling a user their key is bad when the provider was
            # merely down is the confusing failure worth avoiding.
            raise self.parse_error(response)

        body = _json_object(response) or {}
        entries = body.get("data")
        models = (
            tuple(
                entry["id"]
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            )
            if isinstance(entries, list)
            else ()
        )
        return KeyValidation(valid=True, provider=self.name, models=models)

    def rate_limit_headers(self, response: httpx.Response) -> QuotaHint | None:
        """Remaining requests, and a reset that needs converting.

        OpenRouter publishes no token counters, so those two fields stay ``None``
        and ``is_empty()`` handles a response with no headers at all.
        """
        hint = QuotaHint(
            requests_remaining=_read_int_header(response, REMAINING_HEADER),
            requests_reset_s=self._reset_seconds(response.headers.get(RESET_HEADER)),
        )
        return None if hint.is_empty() else hint

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #
    def _auth_headers(self, key: str) -> dict[str, str]:
        """Attribution headers from ``providers.yaml``, then the credential.

        ``options`` is merged *first* so the credential wins: these values come
        from config rather than from a request, but a header map that lets
        deployment config overwrite ``Authorization`` is a trap worth not setting.
        """
        headers: dict[str, str] = dict(self.options)
        headers["Authorization"] = f"Bearer {key}"
        headers["Accept"] = "application/json"
        return headers

    def _read_retry_after(self, response: httpx.Response | None) -> float | None:
        """``Retry-After`` first, then the reset timestamp."""
        if response is None:
            return None

        raw = response.headers.get("retry-after")
        if raw:
            try:
                return float(raw.strip())
            except ValueError:
                # An HTTP-date form. The breaker's exponential backoff is a fine
                # substitute for parsing it.
                pass

        return self._reset_seconds(response.headers.get(RESET_HEADER))

    def _reset_seconds(self, raw: str | None) -> float | None:
        """Turn ``X-RateLimit-Reset`` into seconds from now.

        OpenRouter sends a unix timestamp in *milliseconds*. :class:`QuotaHint`
        wants a delta, so this needs the current time — and reading it off an
        injected clock is what keeps the conversion testable. The magnitude
        branches exist because some routes have been observed sending seconds
        instead, and a 1000x error in a cooldown is the difference between a
        working failover and a provider parked for an hour.
        """
        if raw is None:
            return None
        try:
            value = float(raw.strip())
        except ValueError:
            return None

        now = self._clock.now().timestamp()
        if value > _MS_EPOCH_FLOOR:
            return max(0.0, value / 1000.0 - now)
        if value > _S_EPOCH_FLOOR:
            return max(0.0, value - now)
        # Small enough to already be a delta rather than a timestamp.
        return max(0.0, value)


# --------------------------------------------------------------------------- #
# Pure helpers — deliberately module-level, so `build_payload` cannot reach
# instance state and quietly stop being pure.
# --------------------------------------------------------------------------- #
def _render_content(
    message: CanonicalMessage,
    attachments: dict[str, ResolvedAttachment],
) -> str:
    """Flatten one message's content blocks into the string OpenRouter expects."""
    parts: list[str] = []

    for block in message.content:
        # Narrowing reads off `block["type"]` in the condition itself; assigning
        # it to a local first would leave `block` as the full union.
        if block["type"] == "text":
            parts.append(block["text"])

        elif block["type"] == "omission_marker":
            # The scar D4 leaves. Rendered into the prompt, not just recorded, so
            # the model knows the gap is missing context rather than a topic the
            # user never raised.
            parts.append(f"[{block['omitted_count']} earlier messages omitted]")

        elif block["type"] == "file_ref":
            attachment = attachments.get(block["file_hash"])
            if attachment is None:
                raise ValueError(
                    f"file_ref {block['file_hash'][:12]}… reached build_payload unresolved; "
                    "render step 1 must resolve every attachment first"
                )
            parts.append(_render_attachment(attachment))

    return "\n\n".join(parts)


def _render_attachment(attachment: ResolvedAttachment) -> str:
    """Refuse native files; hand injected ones to the shared envelope.

    The envelope lives in :func:`app.memory.render.document_envelope` rather than
    here, because delimiting extracted text so the model can tell document content
    from user instruction is a prompt-safety decision every provider must make
    identically.

    The refusal is this adapter's own business: the declared OpenRouter models are
    text-only, so a native attachment reaching here means render step 1 routed a
    file past the perception lane. Some OpenRouter upstreams *are* multimodal, and
    building for them before ``capability_registry`` can say which is how a request
    ends up sending image bytes to a text model.
    """
    if attachment.mode == "native":
        raise NotImplementedError(
            f"openrouter's configured models cannot read {attachment.mime} natively; "
            "render step 1 must route this file through the perception lane"
        )

    return document_envelope(attachment)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """Parse a response body, tolerating everything.

    OpenRouter sits behind Cloudflare, so a 502 is an HTML interstitial, a
    truncated response is invalid JSON, and a challenge page is neither. All three
    reach ``parse_error``, which is not allowed to raise.
    """
    try:
        parsed = response.json()
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_error_fields(
    body: dict[str, Any] | None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Dig ``code``, ``message`` and ``metadata`` out of whatever error shape arrived.

    ``code`` is coerced to a string because OpenRouter sends it as an **integer**
    where every other provider sends a string. Stringifying rather than special-
    casing keeps the classifier a single set-membership test, and it is what lets
    the 200-with-an-error path — which has no meaningful HTTP status — classify on
    the body alone.
    """
    if body is None:
        return None, None, {}

    error = body.get("error")
    if isinstance(error, str):
        return None, error, {}
    if not isinstance(error, dict):
        message = body.get("message")
        return None, message if isinstance(message, str) else None, {}

    raw_code = error.get("code")
    if isinstance(raw_code, str):
        code = raw_code
    elif isinstance(raw_code, int) and not isinstance(raw_code, bool):
        code = str(raw_code)
    else:
        code = None

    message = error.get("message")
    metadata = error.get("metadata")
    return (
        code,
        message if isinstance(message, str) else None,
        metadata if isinstance(metadata, dict) else {},
    )


def _looks_like_moderation(metadata: dict[str, Any]) -> bool:
    """Whether a 403 is a content refusal rather than a credential problem."""
    return "reasons" in metadata or "flagged_input" in metadata


def _looks_like_context_overflow(message: str | None) -> bool:
    """Last resort when the body carried no machine-readable ``code``."""
    if not message:
        return False
    lowered = message.lower()
    return any(phrase in lowered for phrase in _CONTEXT_ERROR_PHRASES)


def _read_limit_tokens(message: str | None) -> int | None:
    """Recover the model's advertised limit from the prose, when it is stated.

    Worth the regex: ``ContextTooLong`` is the one error with a retry path, and
    re-running the fitting step against a real number beats halving the history
    and hoping.
    """
    if not message:
        return None
    match = _LIMIT_TOKENS_RE.search(message)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", "").replace("_", ""))
    except ValueError:  # pragma: no cover — the group is \d+ by construction
        return None


def _read_int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _model_from_request(response: httpx.Response) -> str | None:
    """Recover the target model from the request that produced this response.

    Lets the contract's one-argument ``parse_error(response)`` still name the
    model, so an error can open the right circuit breaker instead of one attributed
    to ``"unknown"``.
    """
    try:
        # `.request` raises rather than returning None when unset, which is the
        # case for a hand-built Response in a unit test.
        content = response.request.content
    except (RuntimeError, httpx.StreamConsumed):  # pragma: no cover
        return None
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(parsed, dict):
        model = parsed.get("model")
        if isinstance(model, str):
            return model
    return None


def _estimate_output_tokens(response_body: dict[str, Any]) -> int:
    """Estimate generated tokens from the text, when usage was absent."""
    choices = response_body.get("choices")
    if not isinstance(choices, list) or not choices:
        return 0
    choice = choices[0]
    if not isinstance(choice, dict):
        return 0
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return 0
    return len(content) // CHARS_PER_TOKEN


def _is_int(value: object) -> TypeGuard[int]:
    """``bool`` is an ``int`` subclass; for a token count it never means one."""
    return isinstance(value, int) and not isinstance(value, bool)
