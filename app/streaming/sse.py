"""Server-side SSE framing (§1.1) — the mirror image of Step 7.

Step 7 taught the gateway to read a trickle *from* a provider. This module
teaches it to write a trickle *to* its own client, in the wire format browsers
understand natively: ``event: <name>`` followed by ``data: <json>``, one blank
line apart.

The four event shapes below are ``frontend/lib/sse.ts``'s types transcribed
field-for-field — a JSON diff between the two is the tell that one of them
drifted. ``DoneEvent.usage`` deliberately reuses :class:`~app.schemas.chat.UsageOut`
rather than the ad hoc ``tokens_in``/``tokens_out`` names in the contracts
doc's illustrative snippet: the frontend file's own docstring is explicit that
``DoneEvent`` is meant to be structurally the non-streaming response's
provenance block, so a client that already renders one already renders the
other, and a second usage shape would break that for no benefit.

**This module only formats.** It does not open a provider connection, does not
decide when a ``restart`` happens, and does not merge a heartbeat into a real
event stream — Step 9's orchestrator drives it. What matters most here is not
the framing (that part is easy) but the response headers: leave one out and an
nginx-family proxy will happily buffer the whole body and hand it over as one
blob, producing a gateway that passes every test and does not stream in
production.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from app.providers.types import ExtractionTier
from app.schemas.chat import ServedBy, UsageOut

SSE_MEDIA_TYPE: Final = "text/event-stream"

SSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Not optional. Without this an nginx-family proxy in front of the app
    # buffers the entire body and delivers it as one blob — a gateway that
    # passes every test and, on the deployed instance, visibly does not stream.
    "X-Accel-Buffering": "no",
}

HEARTBEAT_INTERVAL_S: Final = 15.0
"""How often to send a keepalive when nothing real is ready yet.

Comfortably under any proxy's or client's own idle-connection timeout. The
case that matters most is the pre-first-token wait (D13): a slow provider can
legitimately take several seconds to produce anything, and with nothing sent a
client has no way to tell the gateway apart from a dead socket.
"""


# --------------------------------------------------------------------------- #
# §1.1's four event payloads
# --------------------------------------------------------------------------- #
class MetaEvent(BaseModel):
    """The first event on every streamed message, sent before the first delta."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int
    slot: str
    provider: str
    model: str
    requested_slot: str
    conversation_id: str
    message_id: str


class _Delta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str


class _Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delta: _Delta


class DeltaEvent(BaseModel):
    """One incremental piece of text, OpenAI's ``choices[].delta.content`` shape.

    Kept OpenAI-shaped rather than a bare string so a client that already knows
    how to read a chat-completion chunk needs to learn nothing new to read this.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    choices: list[_Choice]

    @classmethod
    def of(cls, content: str) -> DeltaEvent:
        return cls(choices=[_Choice(delta=_Delta(content=content))])


class _FailedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str


class _NextCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: str
    provider: str
    model: str


class RestartEvent(BaseModel):
    """D1's restart: discard the partial, disclose why, name what comes next.

    The client-side contract this event exists to drive: clear the in-progress
    bubble entirely and swap the model indicator. Never splice or diff two
    attempts together.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    failed: _FailedCandidate
    next: _NextCandidate
    attempt: int
    discarded_chars: int

    @classmethod
    def of(
        cls,
        *,
        reason: str,
        failed_provider: str,
        failed_model: str,
        next_slot: str,
        next_provider: str,
        next_model: str,
        attempt: int,
        discarded_chars: int,
    ) -> RestartEvent:
        """Build one without reaching for the two private nested shapes.

        They are private because nothing outside this module should have an
        opinion about how the event nests; the orchestrator has the six flat
        facts and this turns them into the wire shape.
        """
        return cls(
            reason=reason,
            failed=_FailedCandidate(provider=failed_provider, model=failed_model),
            next=_NextCandidate(slot=next_slot, provider=next_provider, model=next_model),
            attempt=attempt,
            discarded_chars=discarded_chars,
        )


class StreamUsage(UsageOut):
    """``UsageOut`` plus the one number only a discarded attempt produces.

    Tokens a failed attempt actually generated were really spent — the free
    tier charged them against RPD/TPM even though the text is thrown away —
    so they are counted separately from the tokens that made it to the client.
    """

    wasted_tokens_out: int = 0


class DoneEvent(BaseModel):
    """The terminal event. Once this is sent, the message is final (D1) — no
    further ``restart`` may follow it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    served_by: ServedBy
    requested_slot: str
    substituted: bool
    attempts: int
    usage: StreamUsage
    degraded: bool
    extraction_tier: ExtractionTier | None = None
    """How this turn's attachments reached the model (Phase 4, D25) — ``cache``,
    ``native``, ``llm`` or ``local``, and the worst of them when there was more
    than one. ``None`` when the turn carried no attachment.

    ``degraded`` says *whether* the reading was a fallback; this says *what
    kind*, so the indicator can render "read by local OCR" rather than a bare
    warning — the same disclosure discipline ``served_by`` gets."""

    messages_dropped: int = 0
    """D34's streaming twin of :attr:`~app.schemas.chat.ChatCompletionResponse.messages_dropped`.
    Zero on a replayed cache hit (:func:`stream_cached_completion`) — nothing was
    rendered this turn."""

    warning: str | None = None
    """D32's streaming twin of :attr:`~app.schemas.chat.ChatCompletionResponse.warning`.
    ``None`` on a replayed cache hit — a replay never reruns pin resolution."""

    key_pool: Literal["shared", "private"] | None = None
    """D42's streaming twin of :attr:`~app.schemas.chat.ChatCompletionResponse.key_pool`
    (Phase 6 Step 7). ``None`` on a replayed cache hit — nothing was spent
    this turn — and only otherwise when every candidate was skipped on an
    open breaker, the one case with no resolved credential to name."""

    status: Literal["ok", "failed"]
    partial_content: str | None = None
    """Set only when ``status == "failed"`` and the longest discarded buffer is
    non-trivial, so the client can offer "keep partial answer"."""


StreamEventPayload = MetaEvent | DeltaEvent | RestartEvent | DoneEvent

_EVENT_NAMES: Final[dict[type[BaseModel], str]] = {
    MetaEvent: "meta",
    DeltaEvent: "delta",
    RestartEvent: "restart",
    DoneEvent: "done",
}


# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #
def format_event(data: StreamEventPayload) -> bytes:
    """One SSE frame: ``event: <name>\\ndata: <json>\\n\\n``.

    The event name is derived from the payload's own type rather than passed
    alongside it, so framing a :class:`DoneEvent` under ``event: delta`` by a
    typo is a type error caught by mypy, not a confused frontend discovering it
    at runtime.
    """
    name = _EVENT_NAMES[type(data)]
    body = data.model_dump_json()
    return f"event: {name}\ndata: {body}\n\n".encode()


def format_heartbeat() -> bytes:
    """A comment line: valid SSE, silently ignored by every client's ``onmessage``.

    Keeps the connection alive through a gap where nothing has happened yet to
    report, without the client mistaking it for a real event.
    """
    return b": heartbeat\n\n"


# --------------------------------------------------------------------------- #
# Client-disconnect detection
# --------------------------------------------------------------------------- #
async def client_disconnected(request: Request) -> bool:
    """Has the client gone away?

    A thin wrapper around Starlette's own check rather than a call spread
    across the streaming path — a disconnect is not a fault (§1.1: "detect it,
    abort upstream, commit tokens, persist nothing further, do not restart"),
    and every caller deciding that the same way starts with asking it the same
    way.
    """
    return await request.is_disconnected()
