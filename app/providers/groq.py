"""The Groq adapter — Contract A's reference implementation.

Groq is OpenAI-compatible, which makes it the right provider to build the
abstraction against first: the shape is familiar, so the interesting work is
visibly the *normalization*, not the translation. Phase 2's Gemini adapter is
where the translation gets interesting.

Three quirks are captured here rather than in a comment somewhere:

**A 413 is usually not a context error.** Groq answers a tokens-per-minute
overage with HTTP 413 and ``code: "rate_limit_exceeded"``. Reading the status
alone would classify a quota problem as ``ContextTooLong`` — failover-ineligible
— and strand the request on an exhausted model when three others were free. So
the body's ``code`` is consulted *before* the status.

**Rate limits are org-level.** A second Groq key adds nothing, which is why
``keys_resolution`` (Phase 6) can treat a user's private Groq key as a billing
change rather than a capacity one.

**The reset headers are Go durations.** ``x-ratelimit-reset-requests`` reads
``"2m59.56s"``, not a number of seconds — hence :func:`_parse_duration`.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, ClassVar, TypeGuard

import httpx

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
    StreamChunk,
    Usage,
)

logger = get_logger("app.providers.groq")

CHAT_COMPLETIONS_PATH = "/chat/completions"
MODELS_PATH = "/models"

VALIDATE_KEY_TIMEOUT_S = 10.0
"""A liveness check that takes longer than this has answered the question."""

CHARS_PER_TOKEN = 4
"""The standard rough ratio for English text. §2.1.5 requires estimates within
±25% of reported usage, which this clears comfortably; anything better would mean
carrying a tokenizer dependency to improve a number that Phase 3 reconciles
against ground truth at commit time anyway."""

PER_MESSAGE_TOKEN_OVERHEAD = 4
"""Role name plus the delimiters every chat format wraps a message in."""

_CONTEXT_ERROR_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})
_RATE_LIMIT_ERROR_CODES = frozenset({"rate_limit_exceeded", "tokens_exceeded", "quota_exceeded"})
_CONTENT_FILTER_CODES = frozenset({"content_filter", "content_policy_violation"})

_CONTEXT_ERROR_PHRASES = (
    "reduce the length",
    "context length",
    "maximum context",
    "too long for model",
)

_LIMIT_TOKENS_RE = re.compile(
    r"(?:maximum context length is|context length of|limit)\s+(\d[\d_,]*)",
    re.IGNORECASE,
)

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "content_filter",
}


class GroqAdapter(HttpProviderAdapter):
    """Satisfies :class:`~app.providers.base.ProviderAdapter` structurally."""

    name: ClassVar[str] = "groq"

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

        Pure: the output depends on nothing but the four arguments. That is what
        makes the golden-file test meaningful — a diff in the committed payload
        is always a deliberate change to this function, never an artefact of when
        or where the suite ran.

        Groq's ``supports_system_field`` is ``False`` for every model, so the
        system prompt stays as the first element of ``messages``. Gemini's
        adapter is where the top-level-field branch lives; putting a dead branch
        here would be untested code pretending to be a capability.
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
            # Clamped rather than passed through. A caller asking for more output
            # than the model can produce gets a 400 from Groq, and turning a
            # config-knowable failure into a round-trip is a waste of a request.
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

        text, finish_reason = self._read_choice(body, model=model)

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

    def _read_choice(self, body: dict[str, Any], *, model: str) -> tuple[str, FinishReason]:
        """Pull the generated text out of a 200, or say why there isn't any."""
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

        # `not content.strip()` and not merely `not content`: a completion of
        # pure whitespace is the same non-answer as an empty one, and free tiers
        # produce both. Shipping either as an assistant message makes the model
        # look broken when the transport was.
        if not isinstance(content, str) or not content.strip():
            raise EmptyResponse(
                "response carried no usable content",
                provider=self.name,
                model=model,
                raw=body,
            )

        finish_reason = _FINISH_REASONS.get(raw_finish, "stop") if raw_finish else "stop"
        return content, finish_reason

    async def stream(
        self,
        payload: dict[str, Any],
        key: str,
        timeout: float,
        idle_timeout: float,
    ) -> AsyncIterator[StreamChunk]:
        """Groq's OpenAI-shaped SSE stream, one delta per chunk.

        ``base.py``'s ``_stream_events`` owns framing, the idle timeout and every
        transport-level fault; everything here is specific to Groq's JSON
        shape — where the delta text lives, and that usage rides on the final
        chunk under ``x_groq`` rather than the ``usage`` key ``extract_usage``
        reads from a non-streaming body.
        """
        model = str(payload.get("model", "unknown"))

        async for frame in self._stream_events(
            "POST",
            CHAT_COMPLETIONS_PATH,
            headers=self._auth_headers(key),
            json=payload,
            timeout_s=timeout,
            idle_timeout_s=idle_timeout,
            model=model,
        ):
            try:
                event = json.loads(frame)
            except json.JSONDecodeError as exc:
                # An event that arrived is not the same as an event that arrived
                # whole — a connection cut mid-write leaves valid SSE framing
                # wrapped around invalid JSON, and that must not escape as a
                # decoding traceback deep inside a streaming response.
                raise Unavailable(
                    "groq sent a malformed stream frame",
                    provider=self.name,
                    model=model,
                ) from exc

            if isinstance(event, dict):
                yield _stream_chunk_from_event(event)

    # ----------------------------------------------------------------- #
    # Normalization
    # ----------------------------------------------------------------- #
    def parse_error(
        self, exc: Exception | httpx.Response, *, model: str | None = None
    ) -> ProviderError:
        """Every Groq failure mode, collapsed into the seven normalized classes.

        Never raises. It runs on the failure path, and an exception escaping here
        would replace a clean 502 with a traceback in the catch-all handler.
        """
        try:
            if isinstance(exc, httpx.Response):
                return self._error_from_response(exc, model=model)
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

        All of them are :class:`Unavailable`: retryable, failover-eligible and
        breaker-eligible. A refused connection carries no information about
        whether the request itself was sound, so the transient reading is the
        only defensible one.
        """
        if isinstance(exc, httpx.TimeoutException):
            detail = "request to groq timed out"
        elif isinstance(exc, httpx.ConnectError):
            detail = "could not connect to groq"
        elif isinstance(exc, httpx.RemoteProtocolError):
            # A truncated response. §2.1.5 case 4 exists because the naive
            # implementation lets a JSON decode error escape as a 500 instead.
            detail = "groq closed the connection mid-response"
        else:
            detail = f"transport failure talking to groq: {type(exc).__name__}"

        return Unavailable(detail, provider=self.name, model=model or "unknown")

    def _error_from_response(self, response: httpx.Response, *, model: str | None) -> ProviderError:
        status = response.status_code
        body = _json_object(response)
        code, message = _read_error_fields(body)
        resolved_model = model or _model_from_request(response) or "unknown"

        common: dict[str, Any] = {
            "provider": self.name,
            "model": resolved_model,
            "status_code": status,
            "raw": body,
        }
        detail = message or f"groq returned HTTP {status}"

        # The body's `code` is consulted before the status, because Groq's status
        # codes are ambiguous where its codes are not — see the module docstring
        # on 413.
        if code in _CONTEXT_ERROR_CODES:
            return ContextTooLong(detail, limit_tokens=_read_limit_tokens(message), **common)
        if code in _RATE_LIMIT_ERROR_CODES:
            return RateLimited(detail, retry_after_s=_read_retry_after(response), **common)
        if code in _CONTENT_FILTER_CODES:
            return ContentFiltered(detail, **common)

        if status in (401, 403):
            return AuthFailed(detail, **common)
        if status == 429:
            return RateLimited(detail, retry_after_s=_read_retry_after(response), **common)
        if status == 413:
            return ContextTooLong(detail, limit_tokens=_read_limit_tokens(message), **common)
        if status in (400, 422):
            if _looks_like_context_overflow(message):
                return ContextTooLong(detail, limit_tokens=_read_limit_tokens(message), **common)
            return BadRequest(detail, **common)
        if status == 404:
            # A model we asked for that Groq no longer serves. Our config is
            # wrong, so it is a `BadRequest` — trying the same wire name on the
            # next provider would be nonsense.
            return BadRequest(detail, **common)
        if status >= 500:
            return Unavailable(detail, **common)

        # Unrecognized. `Unavailable` rather than `BadRequest`: guessing
        # "transient" costs one wasted failover attempt, guessing "permanent"
        # fails a request that might have succeeded anywhere else.
        return Unavailable(detail, **common)

    def extract_usage(self, response_body: dict[str, Any]) -> Usage:
        """Read Groq's usage block, or flag that there wasn't one.

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

        Used to size a quota reservation before the call, when no real count
        exists yet. Reconciled against reported usage at commit time, so being
        cheap matters more than being exact.
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
        """List models — Groq's cheapest liveness check, and it costs no quota."""
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
                detail="Groq rejected this key. Check that it is active and has not been revoked.",
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
        """Groq publishes remaining-requests/tokens and Go-formatted reset durations."""
        hint = QuotaHint(
            requests_remaining=_read_int_header(response, "x-ratelimit-remaining-requests"),
            tokens_remaining=_read_int_header(response, "x-ratelimit-remaining-tokens"),
            requests_reset_s=_read_duration_header(response, "x-ratelimit-reset-requests"),
            tokens_reset_s=_read_duration_header(response, "x-ratelimit-reset-tokens"),
        )
        return None if hint.is_empty() else hint

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #
    def _auth_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


# --------------------------------------------------------------------------- #
# Pure helpers — deliberately module-level, so `build_payload` cannot reach
# instance state and quietly stop being pure.
# --------------------------------------------------------------------------- #
def _render_content(
    message: CanonicalMessage,
    attachments: dict[str, ResolvedAttachment],
) -> str:
    """Flatten one message's content blocks into the string Groq expects."""
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
                # A pure function's only sane response to a caller bug. Render
                # step 1 owns resolution; reaching here means it did not run, and
                # silently dropping the file would answer a question about a
                # document the model never saw.
                raise ValueError(
                    f"file_ref {block['file_hash'][:12]}… reached build_payload unresolved; "
                    "render step 1 must resolve every attachment first"
                )
            parts.append(_render_attachment(attachment))

    return "\n\n".join(parts)


def _render_attachment(attachment: ResolvedAttachment) -> str:
    """Refuse native files; hand injected ones to the shared envelope.

    The envelope itself lives in :func:`app.memory.render.document_envelope`
    rather than here. Delimiting extracted text so the model can tell document
    content from user instruction is a prompt-safety decision every provider must
    make identically, and a per-adapter copy would let one of them drift while its
    own golden file stayed green.

    What *is* Groq's business is the refusal below: it is a text-only provider, so
    a native attachment reaching this point means render step 1 routed a file past
    the perception lane.
    """
    if attachment.mode == "native":
        raise NotImplementedError(
            f"groq cannot read {attachment.mime} natively; "
            "render step 1 must route this file through the perception lane"
        )

    return document_envelope(attachment)


def _stream_chunk_from_event(event: dict[str, Any]) -> StreamChunk:
    """One decoded ``chat.completion.chunk`` -> one :class:`StreamChunk`.

    Tolerant of whichever fields a given chunk carries: the first chunk is a
    bare role announcement with no content, most chunks carry only a delta, and
    only the last carries a ``finish_reason`` and Groq's ``x_groq.usage`` block.
    """
    delta_text = ""
    finish_reason: str | None = None

    choices = event.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    delta_text = content

            raw_finish = choice.get("finish_reason")
            if isinstance(raw_finish, str):
                finish_reason = _FINISH_REASONS.get(raw_finish, raw_finish)

    usage: Usage | None = None
    x_groq = event.get("x_groq")
    if isinstance(x_groq, dict):
        raw_usage = x_groq.get("usage")
        if isinstance(raw_usage, dict):
            tokens_in = raw_usage.get("prompt_tokens")
            tokens_out = raw_usage.get("completion_tokens")
            if _is_int(tokens_in) and _is_int(tokens_out):
                usage = Usage(tokens_in=tokens_in, tokens_out=tokens_out, estimated=False)

    return StreamChunk(delta=delta_text, finish_reason=finish_reason, usage=usage)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """Parse a response body, tolerating everything.

    A 502 from a proxy in front of Groq is HTML, a truncated response is invalid
    JSON, and a gateway timeout is plain text. All three reach ``parse_error``,
    which is not allowed to raise.
    """
    try:
        parsed = response.json()
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_error_fields(body: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Dig ``code`` and ``message`` out of whatever error shape arrived."""
    if body is None:
        return None, None

    error = body.get("error")
    if isinstance(error, str):
        return None, error
    if not isinstance(error, dict):
        message = body.get("message")
        return None, message if isinstance(message, str) else None

    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


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


def _read_retry_after(response: httpx.Response) -> float | None:
    """``Retry-After`` first, then Groq's own reset headers.

    The header is authoritative when present; the reset durations are what Groq
    actually sends on most 429s.
    """
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return float(raw.strip())
        except ValueError:
            # An HTTP-date form. Rare from Groq, and the breaker's exponential
            # backoff is a fine substitute for parsing it.
            pass

    for header in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        value = _read_duration_header(response, header)
        if value is not None:
            return value
    return None


def _parse_duration(raw: str) -> float | None:
    """Parse a Go-style duration — ``"2m59.56s"``, ``"7.66s"``, ``"1h2m3s"``.

    Groq reports resets this way rather than as a number of seconds, so a naive
    ``float()`` yields ``None`` on every real response.
    """
    text = raw.strip()
    if not text:
        return None

    matches = _DURATION_RE.findall(text)
    if matches:
        scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
        return sum(float(value) * scale[unit] for value, unit in matches)

    try:
        return float(text)
    except ValueError:
        return None


def _read_int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _read_duration_header(response: httpx.Response, name: str) -> float | None:
    raw = response.headers.get(name)
    return _parse_duration(raw) if raw is not None else None


def _model_from_request(response: httpx.Response) -> str | None:
    """Recover the target model from the request that produced this response.

    Lets the contract's one-argument ``parse_error(response)`` still name the
    model, so a conformance test written to §2.1.3 exactly does not produce
    errors attributed to ``"unknown"``.
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
