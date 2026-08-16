"""The Gemini adapter — the reason Contract A was written before Groq.

Groq is OpenAI-compatible, so its adapter is mostly *normalization*. Gemini
agrees with nobody: the model is a path segment, the key is a bespoke header, the
system prompt is a top-level field, the assistant is called ``"model"``, the
generation knobs are nested, and a refusal arrives with HTTP 200. If the protocol
had been extracted from ``groq.py`` afterwards it would have needed bending here,
which is the whole argument §2.1 makes for writing the interface first.

Five quirks are captured in code rather than in a comment somewhere:

**The model lives in the URL, not the body.** ``POST /models/{model}:generateContent``.
``complete`` only ever receives ``payload``, so the model has to travel inside it —
see :data:`MODEL_FIELD` for the key that carries it and why the alternatives are
worse.

**A dead key is a 400, not a 401.** Google answers a revoked or malformed key with
``400 INVALID_ARGUMENT`` and ``"API key not valid"``. Reading the status alone
classifies that as :class:`BadRequest`, which is *not* failover-eligible — so a
revoked ``GEMINI_API_KEY`` would abort the whole request instead of failing over to
a provider whose key still works. This is Gemini's counterpart to Groq's 413, and
:func:`_looks_like_api_key_problem` is consulted before the status ladder for the
same reason.

**A refusal arrives as HTTP 200.** ``promptFeedback.blockReason``, or a candidate
with ``finishReason: "SAFETY"``. So the content-filter check lives in
:meth:`GeminiAdapter._read_candidate` on the success path, not in ``parse_error``.

**The retry hint is in the body.** ``error.details[]`` carries a ``RetryInfo`` with
a protobuf duration (``"23s"``); there is no ``Retry-After`` header on most 429s.

**Gemini publishes no rate-limit headers**, so ``rate_limit_headers`` is
deliberately not overridden — the base's ``None`` is the honest answer. Quota is
enforced per Google Cloud *project* rather than per key, which is why a second
Gemini key adds nothing and why D8's 50/50 lane split has to be enforced by us.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, ClassVar, TypeGuard

import httpx

from app.core.logging import get_logger
from app.memory.canonical import CanonicalMessage, Role
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
    ResolvedAttachment,
    StreamChunk,
    Usage,
)

logger = get_logger("app.providers.gemini")

GENERATE_CONTENT_TEMPLATE = "/models/{model}:generateContent"
STREAM_GENERATE_CONTENT_TEMPLATE = "/models/{model}:streamGenerateContent"
"""Step 7's endpoint. Needs ``?alt=sse`` — without it Gemini returns a JSON
*array* that only completes at the end, which streams nothing while looking like
it should."""

MODELS_PATH = "/models"

API_KEY_HEADER = "x-goog-api-key"
"""Not ``Authorization: Bearer``. Gemini also accepts ``?key=`` in the query
string, which is never used here: a URL is logged, cached and reflected in error
messages by half the stack, and a header is not."""

VALIDATE_KEY_TIMEOUT_S = 10.0
"""A liveness check that takes longer than this has answered the question."""

CHARS_PER_TOKEN = 4
PER_MESSAGE_TOKEN_OVERHEAD = 4

MODEL_FIELD = "_model"
"""Where :meth:`GeminiAdapter.build_payload` leaves the model for the URL.

Gemini names the model in the path, and Contract A's ``complete(payload, key,
timeout)`` is frozen — so the model has to ride inside the payload dict and be
stripped before the body goes on the wire. The underscore marks it as
gateway-internal; :func:`_split_model` is the only reader.

The alternatives, and why each is worse:

* *Add a ``model`` or ``spec`` parameter to ``complete``.* Changes a frozen
  signature, and every other adapter would pay for Gemini's URL shape.
* *Remember it on the adapter* (``self._model``). One instance serves the whole
  process and every concurrent request — ``base.py``'s own docstring says so — so
  two simultaneous requests on different Gemini models would cross wires. That is
  the worst class of bug available here, and it would only show up under load.
* *Return a wrapper object.* Changes ``build_payload``'s frozen return type.
* *Send ``model`` inside the body.* It is a path-bound field; Google's JSON
  transcoder rejects unknown top-level names with a 400, and it would duplicate
  the source of truth. Hence the strip rather than a rename.

It is in the committed golden file on purpose: the golden pins the whole return
value of ``build_payload``, which is exactly what ``complete`` receives. A
separate adapter test asserts it never reaches the wire.
"""

_ROLES: dict[Role, str] = {"user": "user", "assistant": "model"}
"""Trap 10, in one place. Sending ``"assistant"`` earns a 400 that reads like a
payload problem, because it is one. ``system`` is absent deliberately — it is
hoisted out of ``contents`` entirely, so a lookup for it is a bug, not a default."""

_BLOCK_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)
"""Finish reasons that mean "the model refused", not "the model failed".

Wider than the two §4 Step 6 names: the newer enum members are the same kind of
refusal, and classifying one of them as :class:`EmptyResponse` would make it
failover-eligible — which is precisely the laundering ``ContentFiltered`` exists
to prevent."""

_FINISH_REASONS: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}

_API_KEY_PHRASES = (
    "api key not valid",
    "api_key_invalid",
    "api key expired",
    "invalid api key",
)

_CONTEXT_ERROR_PHRASES = (
    "input token count",
    "exceeds the maximum number of tokens",
    "request payload size exceeds",
    "context length",
)

_LIMIT_TOKENS_RE = re.compile(
    r"maximum number of tokens allowed[^\d]{0,20}(\d[\d_,]*)",
    re.IGNORECASE,
)

_RETRY_DELAY_RE = re.compile(r"^(\d+(?:\.\d+)?)s?$")

_RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
_AUTH_STATUSES = frozenset({"UNAUTHENTICATED", "PERMISSION_DENIED", "FAILED_PRECONDITION"})
_UNAVAILABLE_STATUSES = frozenset(
    {"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "ABORTED", "CANCELLED", "UNKNOWN"}
)
_BAD_REQUEST_STATUSES = frozenset(
    {"INVALID_ARGUMENT", "OUT_OF_RANGE", "NOT_FOUND", "UNIMPLEMENTED", "ALREADY_EXISTS"}
)


class GeminiAdapter(HttpProviderAdapter):
    """Satisfies :class:`~app.providers.base.ProviderAdapter` structurally."""

    name: ClassVar[str] = "gemini"

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
        """Canonical history → a ``generateContent`` body, plus the URL's model.

        Pure: the output depends on nothing but the four arguments, which is what
        makes the golden file meaningful. Put the three Gemini-shaped decisions
        side by side with Groq's and the abstraction stops being a claim.

        **The system prompt is hoisted unconditionally.** This does *not* read
        ``spec.supports_system_field`` — Gemini has nowhere else to put a system
        prompt, so the ``False`` branch would be dead code, and shipping a dead
        branch is what ``groq.build_payload``'s docstring refuses to do in the
        other direction. The trait exists so the registry can *describe* the wire
        format, not as a switch this method consults.

        **Mixed casing is correct.** ``system_instruction`` is snake_case and
        ``generationConfig`` is camelCase because that is what the API documents;
        proto3 JSON accepts either form for either field, so "fixing" one of them
        would re-bless the golden for no wire-level change.
        """
        by_hash = {attachment.file_hash: attachment for attachment in attachments}

        system_texts = [
            _render_text(message, by_hash) for message in messages if message.role == "system"
        ]
        contents = [
            {
                "role": _ROLES[message.role],
                "parts": [{"text": _render_text(message, by_hash)}],
            }
            for message in messages
            if message.role != "system"
        ]

        generation_config: dict[str, Any] = {"temperature": params.temperature}
        if params.max_tokens is not None:
            # Clamped for the same reason Groq clamps: a caller asking for more
            # output than the model can produce gets a 400, and turning a
            # config-knowable failure into a round trip wastes a request.
            generation_config["maxOutputTokens"] = min(params.max_tokens, spec.max_output_tokens)
        if params.top_p is not None:
            generation_config["topP"] = params.top_p
        if params.stop:
            generation_config["stopSequences"] = list(params.stop)

        payload: dict[str, Any] = {MODEL_FIELD: spec.model}
        if system_texts:
            payload["system_instruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
        payload["contents"] = contents
        payload["generationConfig"] = generation_config

        # No `stream` key, deliberately. Gemini expresses streaming by endpoint
        # (`:streamGenerateContent`), and an unrecognized top-level field is a
        # 400 — so `params.stream` is simply unrepresentable in this body. Its
        # absence next to Groq's `"stream": false` is the point, not an omission.
        return payload

    # ----------------------------------------------------------------- #
    # Execution
    # ----------------------------------------------------------------- #
    async def complete(self, payload: dict[str, Any], key: str, timeout: float) -> Completion:
        model, body = _split_model(payload)

        response = await self._request(
            "POST",
            GENERATE_CONTENT_TEMPLATE.format(model=model),
            headers=self._auth_headers(key),
            json=body,
            timeout_s=timeout,
            model=model,
        )

        if response.status_code >= 400:
            error = self.parse_error(response, model=model)
            logger.warning("provider.request_failed", **error.log_fields())
            raise error

        parsed = _json_object(response)
        if parsed is None:
            raise EmptyResponse(
                "response body was not a JSON object",
                provider=self.name,
                model=model,
                status_code=response.status_code,
            )

        text, finish_reason = self._read_candidate(parsed, model=model)

        usage = self.extract_usage(parsed)
        if usage.estimated:
            # `extract_usage` only sees the response and so cannot know the input
            # size. The full payload is passed, `_model` and all — `estimate_tokens`
            # walks `contents` and ignores everything else.
            usage = replace(usage, tokens_in=self.estimate_tokens(payload))

        raw_id = parsed.get("responseId")
        return Completion(
            text=text,
            usage=usage,
            finish_reason=finish_reason,
            raw_id=raw_id if isinstance(raw_id, str) else None,
        )

    def _read_candidate(self, body: dict[str, Any], *, model: str) -> tuple[str, FinishReason]:
        """Pull the generated text out of a 200, or say why there isn't any.

        **The order of these checks is load-bearing.** A blocked prompt comes back
        as ``promptFeedback.blockReason`` *with an empty ``candidates`` list*, so
        testing for emptiness first would report :class:`EmptyResponse` — which is
        failover-eligible, meaning the refused prompt gets shopped to the next
        provider until one of them answers it. That is exactly the laundering the
        error table forbids, and it is invisible unless this order is pinned by a
        test.
        """
        feedback = body.get("promptFeedback")
        block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        if block_reason:
            raise ContentFiltered(
                f"gemini blocked the prompt ({block_reason})",
                provider=self.name,
                model=model,
                raw=body,
            )

        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise EmptyResponse(
                "response carried no candidates", provider=self.name, model=model, raw=body
            )

        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise EmptyResponse(
                "malformed candidate object", provider=self.name, model=model, raw=body
            )

        raw_finish = candidate.get("finishReason")
        if isinstance(raw_finish, str) and raw_finish in _BLOCK_FINISH_REASONS:
            raise ContentFiltered(
                f"the model declined to generate a response ({raw_finish})",
                provider=self.name,
                model=model,
                raw=body,
            )

        text = _candidate_text(candidate)

        # Whitespace-only is the same non-answer as empty, and free tiers produce
        # both. It is also what a `MAX_TOKENS` finish looks like when the whole
        # output budget went on reasoning, which is honestly an empty answer.
        if not text.strip():
            raise EmptyResponse(
                "response carried no usable content",
                provider=self.name,
                model=model,
                raw=body,
            )

        finish_reason = (
            _FINISH_REASONS.get(raw_finish, "stop") if isinstance(raw_finish, str) else "stop"
        )
        return text, finish_reason

    async def stream(
        self,
        payload: dict[str, Any],
        key: str,
        timeout: float,
        idle_timeout: float,
    ) -> AsyncIterator[StreamChunk]:
        """``streamGenerateContent``, one full ``GenerateContentResponse`` per event.

        ``?alt=sse`` is not optional — see :data:`STREAM_GENERATE_CONTENT_TEMPLATE`.
        ``base.py``'s ``_stream_events`` owns framing, the idle timeout and every
        transport-level fault; everything here is specific to Gemini's shape.

        Unlike Groq's OpenAI-style delta fragments, every event on this stream is a
        complete response object — the same shape :meth:`_read_candidate` reads at
        the end of a non-streaming call, just one incremental candidate at a time.
        That is why the block-reason check has to run again per chunk rather than
        once at the end: a prompt Gemini decides to refuse can be blocked on the
        *first* event, with no ``candidates`` key at all, and a later chunk can
        carry ``finishReason: "SAFETY"`` on an answer that started streaming before
        the refusal was noticed. Both are :class:`ContentFiltered`, not a plain
        empty chunk — the same laundering :meth:`_read_candidate`'s docstring warns
        about, on the streaming path.

        Uses :func:`_split_model_lenient` rather than :func:`_split_model`: a
        payload reaching this method always carries :data:`MODEL_FIELD` in real
        traffic, since ``render()`` always goes through ``build_payload`` first,
        but the URL has to be built before the first byte of a real request has
        gone anywhere to raise a normalized error against — matching
        :meth:`GroqAdapter.stream`'s own ``"unknown"`` fallback for the same
        reason, rather than a bare :exc:`ValueError` escaping the call.
        """
        model, body = _split_model_lenient(payload)

        async for frame in self._stream_events(
            "POST",
            f"{STREAM_GENERATE_CONTENT_TEMPLATE.format(model=model)}?alt=sse",
            headers=self._auth_headers(key),
            json=body,
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
                    "gemini sent a malformed stream frame",
                    provider=self.name,
                    model=model,
                ) from exc

            if not isinstance(event, dict):
                continue

            chunk = _stream_chunk_from_event(event)
            if chunk.finish_reason == "content_filter":
                raise ContentFiltered(
                    "the model declined to generate a response",
                    provider=self.name,
                    model=model,
                    raw=event,
                )
            yield chunk

    # ----------------------------------------------------------------- #
    # Normalization
    # ----------------------------------------------------------------- #
    def parse_error(
        self, exc: Exception | httpx.Response, *, model: str | None = None
    ) -> ProviderError:
        """Every Gemini failure mode, collapsed into the seven normalized classes.

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

        All of them are :class:`Unavailable`: a refused connection carries no
        information about whether the request itself was sound, so the transient
        reading is the only defensible one.
        """
        if isinstance(exc, httpx.TimeoutException):
            detail = "request to gemini timed out"
        elif isinstance(exc, httpx.ConnectError):
            detail = "could not connect to gemini"
        elif isinstance(exc, httpx.RemoteProtocolError):
            detail = "gemini closed the connection mid-response"
        else:
            detail = f"transport failure talking to gemini: {type(exc).__name__}"

        return Unavailable(detail, provider=self.name, model=model or "unknown")

    def _error_from_response(self, response: httpx.Response, *, model: str | None) -> ProviderError:
        status = response.status_code
        body = _json_object(response)
        status_string, message, details = _read_error_fields(body)
        resolved_model = model or _model_from_url(response) or "unknown"

        common: dict[str, Any] = {
            "provider": self.name,
            "model": resolved_model,
            "status_code": status,
            "raw": body,
        }
        detail = message or f"gemini returned HTTP {status}"

        # The message is read before the `status` string, and the `status` string
        # before the HTTP code, because Google's 400 covers a malformed payload, an
        # oversized history and a dead key alike — three different routing
        # decisions behind one status.
        if _looks_like_api_key_problem(message):
            # See the module docstring. A `BadRequest` here would strand the
            # request on a provider whose credential we already know is dead.
            return AuthFailed(detail, **common)
        if _looks_like_context_overflow(message):
            # Ahead of the status map: an `INVALID_ARGUMENT` overflow landing as a
            # plain `BadRequest` would lose the re-fit-and-retry path, which is the
            # one recovery `ContextTooLong` has.
            return ContextTooLong(detail, limit_tokens=_read_limit_tokens(message), **common)

        if status_string == _RESOURCE_EXHAUSTED:
            return RateLimited(detail, retry_after_s=_read_retry_after(response, details), **common)
        if status_string in _AUTH_STATUSES:
            # `FAILED_PRECONDITION` is included on purpose: Google uses it for
            # "the free tier is not available in your country" and "billing is
            # required", which are credential-shaped conditions a human must act
            # on — failover-eligible *and* alert-worthy, which is the right pair.
            return AuthFailed(detail, **common)
        if status_string in _UNAVAILABLE_STATUSES:
            return Unavailable(detail, **common)
        if status_string in _BAD_REQUEST_STATUSES:
            return BadRequest(detail, **common)

        # No usable `status` string. Fall back to the HTTP ladder.
        if status in (401, 403):
            return AuthFailed(detail, **common)
        if status == 429:
            return RateLimited(detail, retry_after_s=_read_retry_after(response, details), **common)
        if status in (400, 404, 422):
            return BadRequest(detail, **common)
        if status >= 500:
            return Unavailable(detail, **common)

        # Unrecognized. `Unavailable` rather than `BadRequest`: guessing
        # "transient" costs one wasted failover attempt, guessing "permanent"
        # fails a request that might have succeeded anywhere else.
        return Unavailable(detail, **common)

    def extract_usage(self, response_body: dict[str, Any]) -> Usage:
        """Read ``usageMetadata``, or flag that there wasn't one.

        ``thoughtsTokenCount`` is added to the output side rather than ignored.
        Thinking tokens are generated and billed as output, and Gemini reports
        them *outside* ``candidatesTokenCount`` — so dropping them under-counts
        exactly the models most likely to be routed to, and Phase 3's tracker
        would then be wrong in the direction that gets a key rate-limited earlier
        than predicted.
        """
        usage = response_body.get("usageMetadata")
        if isinstance(usage, dict):
            tokens_in = usage.get("promptTokenCount")
            tokens_out = usage.get("candidatesTokenCount")
            if _is_int(tokens_in) and _is_int(tokens_out):
                thoughts = usage.get("thoughtsTokenCount")
                return Usage(
                    tokens_in=tokens_in,
                    tokens_out=tokens_out + (thoughts if _is_int(thoughts) else 0),
                    estimated=False,
                )

        return Usage(tokens_in=0, tokens_out=_estimate_output_tokens(response_body), estimated=True)

    def estimate_tokens(self, payload: dict[str, Any]) -> int:
        """A characters-over-four estimate of the payload's input size.

        Walks ``contents`` and ``system_instruction`` and ignores every other key,
        which is what lets :meth:`complete` hand it the payload with
        :data:`MODEL_FIELD` still attached.
        """
        total = 0

        system = payload.get("system_instruction")
        if isinstance(system, dict):
            total += _parts_length(system.get("parts")) // CHARS_PER_TOKEN
            total += PER_MESSAGE_TOKEN_OVERHEAD

        contents = payload.get("contents")
        if isinstance(contents, list):
            for entry in contents:
                if not isinstance(entry, dict):
                    continue
                total += _parts_length(entry.get("parts")) // CHARS_PER_TOKEN
                total += PER_MESSAGE_TOKEN_OVERHEAD

        return max(1, total)

    # ----------------------------------------------------------------- #
    # Operations
    # ----------------------------------------------------------------- #
    async def validate_key(self, key: str) -> KeyValidation:
        """List models — Gemini's cheapest liveness check, and it costs no quota."""
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
                    "Gemini rejected this key. Check that it is active and has not been revoked."
                ),
            )

        if response.status_code >= 400:
            body = _json_object(response)
            _, message, _ = _read_error_fields(body)
            if _looks_like_api_key_problem(message):
                # The module docstring's second quirk, on the BYOK path: Google
                # answers a bad key with a 400, and raising `BadRequest` from a
                # method whose contract is "raise only when we learned nothing"
                # would tell the user their key could not be checked when in fact
                # it was checked and rejected.
                return KeyValidation(
                    valid=False,
                    provider=self.name,
                    detail=(
                        "Gemini rejected this key as invalid. "
                        "Check that it is active and copied in full."
                    ),
                )
            # §9.2: telling a user their key is bad when the provider was merely
            # down is the confusing failure worth avoiding.
            raise self.parse_error(response)

        body = _json_object(response) or {}
        entries = body.get("models")
        models = (
            tuple(
                _strip_models_prefix(entry["name"])
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            )
            if isinstance(entries, list)
            else ()
        )
        # `nextPageToken` is ignored: one page answers a liveness question, and
        # paging a catalogue to validate a credential is a lot of round trips for
        # a boolean.
        return KeyValidation(valid=True, provider=self.name, models=models)

    # `rate_limit_headers` is deliberately *not* overridden. Gemini publishes no
    # rate-limit headers at all, so the base's `None` is the honest answer and a
    # parser here would be code that never runs. A unit test pins the `None`, so a
    # future contributor who invents one has to face the fixture first.

    # ----------------------------------------------------------------- #
    # Internals
    # ----------------------------------------------------------------- #
    def _auth_headers(self, key: str) -> dict[str, str]:
        return {API_KEY_HEADER: key, "Accept": "application/json"}


# --------------------------------------------------------------------------- #
# Pure helpers — deliberately module-level, so `build_payload` cannot reach
# instance state and quietly stop being pure.
# --------------------------------------------------------------------------- #
def _render_text(
    message: CanonicalMessage,
    attachments: dict[str, ResolvedAttachment],
) -> str:
    """Flatten one message's content blocks into a single text part.

    **One part per message, blocks joined with a blank line** — not one part per
    block, which is the reading the aside in ``render.py`` invites. Gemini
    concatenates ``parts`` with *no* separator, so a part-per-block payload
    renders ``[4 earlier messages omitted]Recap: what did we…``: D4's omission
    scar welded onto the next sentence, which is worse than not marking it at all.
    Joining here also keeps the text byte-identical to what the fitting step
    measured, so ``RenderReport.estimated_tokens`` stays comparable to reported
    usage.

    ``parts`` is still the right home for Phase 4's native attachments — a
    ``inline_data`` part sits *beside* this text part. The list is for modalities,
    not for content blocks.
    """
    parts: list[str] = []

    for block in message.content:
        # Narrowing reads off `block["type"]` in the condition itself; assigning
        # it to a local first would leave `block` as the full union.
        if block["type"] == "text":
            parts.append(block["text"])

        elif block["type"] == "omission_marker":
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

    Gemini's refusal says something different from Groq's, and the difference is
    the honest part. Groq *cannot* read a PDF. Gemini can, natively, via
    ``inline_data`` — but no ``file_ref`` can exist before ``POST /v1/files`` does,
    which is Phase 4. Building the branch now would ship an untested code path
    presenting itself as a capability, which is the exact thing the Groq adapter's
    ``build_payload`` docstring refuses to do about the system field.
    """
    if attachment.mode == "native":
        raise NotImplementedError(
            f"gemini could read {attachment.mime} natively via inline_data, but the "
            "perception lane lands in Phase 4 and no file_ref can exist before "
            "POST /v1/files does; render step 1 must not route a native attachment here yet"
        )

    return document_envelope(attachment)


def _split_model(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The model for the URL, and the body without it.

    Exact-key removal rather than an ``_``-prefix sweep: predictable, and a future
    legitimately-underscored field cannot be swallowed by accident.
    """
    model = payload.get(MODEL_FIELD)
    if not isinstance(model, str) or not model:
        raise ValueError(
            f"gemini payload is missing {MODEL_FIELD!r}; every payload must come from "
            "build_payload, which is the only thing that knows which model the URL names"
        )
    return model, {key: value for key, value in payload.items() if key != MODEL_FIELD}


def _split_model_lenient(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """As :func:`_split_model`, but for ``stream()``, which needs a model name
    for the URL the instant it is called — before any frame has arrived to
    raise a normalized error against. Falls back to ``"unknown"`` rather than
    raising, the same posture :meth:`GroqAdapter.stream` takes for its own
    (body-only) model field.
    """
    model = payload.get(MODEL_FIELD)
    resolved = model if isinstance(model, str) and model else "unknown"
    return resolved, {key: value for key, value in payload.items() if key != MODEL_FIELD}


def _stream_chunk_from_event(event: dict[str, Any]) -> StreamChunk:
    """One decoded ``GenerateContentResponse`` -> one :class:`StreamChunk`.

    ``finish_reason="content_filter"`` is a signal back to :meth:`GeminiAdapter.stream`
    to raise, not a value callers see — mirroring how :meth:`_read_candidate`'s
    refusal branch works on the non-streaming path, so the same condition means
    the same thing on both.

    **The block-reason check runs before touching ``candidates``, exactly as in**
    :meth:`_read_candidate`. A blocked prompt's event carries an empty or absent
    ``candidates`` list, and checking emptiness first would report a plain empty
    chunk — which is not failover-eligible in the same way a refusal is required
    to be inert, but *is* the wrong signal, and would let the router treat a
    deliberate refusal as an ordinary stream that produced nothing.
    """
    feedback = event.get("promptFeedback")
    block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
    if block_reason:
        return StreamChunk(delta="", finish_reason="content_filter")

    delta_text = ""
    finish_reason: str | None = None

    candidates = event.get("candidates")
    if isinstance(candidates, list) and candidates:
        candidate = candidates[0]
        if isinstance(candidate, dict):
            delta_text = _candidate_text(candidate)
            raw_finish = candidate.get("finishReason")
            if isinstance(raw_finish, str):
                finish_reason = (
                    "content_filter"
                    if raw_finish in _BLOCK_FINISH_REASONS
                    else _FINISH_REASONS.get(raw_finish, raw_finish)
                )

    usage: Usage | None = None
    raw_usage = event.get("usageMetadata")
    if isinstance(raw_usage, dict):
        tokens_in = raw_usage.get("promptTokenCount")
        tokens_out = raw_usage.get("candidatesTokenCount")
        if _is_int(tokens_in) and _is_int(tokens_out):
            thoughts = raw_usage.get("thoughtsTokenCount")
            usage = Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out + (thoughts if _is_int(thoughts) else 0),
                estimated=False,
            )

    return StreamChunk(delta=delta_text, finish_reason=finish_reason, usage=usage)


def _candidate_text(candidate: dict[str, Any]) -> str:
    """Join a candidate's text parts.

    ``""`` and not ``"\\n\\n"``: these are segments of one answer, and Gemini
    itself concatenates them without a separator.
    """
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    return "".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _parts_length(parts: object) -> int:
    """Total characters across a ``parts`` list, ignoring non-text parts."""
    if not isinstance(parts, list):
        return 0
    return sum(
        len(part["text"])
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """Parse a response body, tolerating everything.

    A 502 from Google's edge is HTML, a truncated response is invalid JSON, and a
    gateway timeout is plain text. All three reach ``parse_error``, which is not
    allowed to raise.
    """
    try:
        parsed = response.json()
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_error_fields(
    body: dict[str, Any] | None,
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Dig ``status``, ``message`` and ``details`` out of Google's error envelope.

    The shape is ``{"error": {"code", "message", "status", "details": [...]}}``.
    The ``status`` string is the machine-readable half — ``code`` merely repeats
    the HTTP status — which is why it is what the ladder reads.
    """
    if body is None:
        return None, None, []

    error = body.get("error")
    if isinstance(error, str):
        return None, error, []
    if not isinstance(error, dict):
        message = body.get("message")
        return None, message if isinstance(message, str) else None, []

    status = error.get("status")
    message = error.get("message")
    raw_details = error.get("details")
    details = (
        [entry for entry in raw_details if isinstance(entry, dict)]
        if isinstance(raw_details, list)
        else []
    )
    return (
        status if isinstance(status, str) else None,
        message if isinstance(message, str) else None,
        details,
    )


def _looks_like_api_key_problem(message: str | None) -> bool:
    """Whether a 400's prose is really about the credential."""
    if not message:
        return False
    lowered = message.lower()
    return any(phrase in lowered for phrase in _API_KEY_PHRASES)


def _looks_like_context_overflow(message: str | None) -> bool:
    """Gemini states an overflow in prose, with no machine-readable marker."""
    if not message:
        return False
    lowered = message.lower()
    return any(phrase in lowered for phrase in _CONTEXT_ERROR_PHRASES)


def _read_limit_tokens(message: str | None) -> int | None:
    """Recover the model's real limit from the prose, when it is stated.

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


def _read_retry_after(response: httpx.Response, details: list[dict[str, Any]]) -> float | None:
    """``Retry-After`` when Google sends one, else ``RetryInfo`` from the body.

    The body is the usual case, which is the quirk: every other provider in the
    pool puts this in a header, so an implementation that only reads headers finds
    nothing on every real Gemini 429 and silently falls back to the breaker's
    exponential ladder.
    """
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return float(raw.strip())
        except ValueError:
            # An HTTP-date form. The breaker's exponential backoff is a fine
            # substitute for parsing it.
            pass

    for entry in details:
        type_url = entry.get("@type")
        is_retry_info = (isinstance(type_url, str) and type_url.endswith("RetryInfo")) or (
            "retryDelay" in entry
        )
        if not is_retry_info:
            continue
        value = _parse_duration(entry.get("retryDelay"))
        if value is not None:
            return value
    return None


def _parse_duration(raw: object) -> float | None:
    """Parse a protobuf ``Duration`` in its JSON form — ``"23s"``, ``"0.5s"``.

    Deliberately not Groq's Go-duration parser: this grammar is a decimal number
    with a trailing ``s``, and borrowing a parser that also accepts ``"2m59.56s"``
    would be sharing code between two providers that do not share a format.
    """
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str):
        return None
    match = _RETRY_DELAY_RE.match(raw.strip())
    if match is None:
        return None
    return float(match.group(1))


def _model_from_url(response: httpx.Response) -> str | None:
    """Recover the target model from the request URL.

    ``/v1beta/models/gemini-3.6-flash:generateContent`` → ``gemini-3.6-flash``.
    Lets the contract's one-argument ``parse_error(response)`` still name the
    model, so an error can open the right circuit breaker.

    The colon check is what stops ``GET /models`` from yielding the model name
    ``"models"``.
    """
    try:
        # `.request` raises rather than returning None when unset, which is the
        # case for a hand-built Response in a unit test.
        path = response.request.url.path
    except RuntimeError:
        return None

    segment = path.rsplit("/", 1)[-1]
    if ":" not in segment:
        return None
    return segment.split(":", 1)[0] or None


def _estimate_output_tokens(response_body: dict[str, Any]) -> int:
    """Estimate generated tokens from the text, when ``usageMetadata`` was absent."""
    candidates = response_body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return 0
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return 0
    return len(_candidate_text(candidate)) // CHARS_PER_TOKEN


def _strip_models_prefix(name: str) -> str:
    """``"models/gemini-3.6-flash"`` → ``"gemini-3.6-flash"``.

    Gemini's catalogue returns fully-qualified resource names. Leaving the prefix
    on makes every comparison against a ``ModelSpec.model`` silently false, which
    Phase 6's per-provider capability merge depends on.
    """
    return name.removeprefix("models/")


def _is_int(value: object) -> TypeGuard[int]:
    """``bool`` is an ``int`` subclass; for a token count it never means one."""
    return isinstance(value, int) and not isinstance(value, bool)
