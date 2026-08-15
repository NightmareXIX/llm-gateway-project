"""``app/streaming/sse.py`` — event framing, headers, heartbeat, disconnect.

Pure formatting plus one thin Starlette wrapper. Nothing here opens a network
connection, so unlike most of this repo there is no fixture to replay.
"""

from __future__ import annotations

import json

import pytest
from fastapi import Request
from pydantic import ValidationError

from app.schemas.chat import ServedBy
from app.streaming.sse import (
    HEARTBEAT_INTERVAL_S,
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    DeltaEvent,
    DoneEvent,
    MetaEvent,
    RestartEvent,
    StreamUsage,
    client_disconnected,
    format_event,
    format_heartbeat,
)


def _served_by() -> ServedBy:
    return ServedBy(slot="general", provider="gemini", model="gemini-flash")


# --------------------------------------------------------------------------- #
# format_event
# --------------------------------------------------------------------------- #
def test_meta_event_frames_as_a_named_sse_block() -> None:
    event = MetaEvent(
        attempt=1,
        slot="general",
        provider="groq",
        model="llama-3.3-70b-versatile",
        requested_slot="general",
        conversation_id="11111111-1111-4111-8111-111111111111",
        message_id="22222222-2222-4222-8222-222222222222",
    )

    frame = format_event(event)

    assert frame.startswith(b"event: meta\ndata: ")
    assert frame.endswith(b"\n\n")
    line = frame.decode().splitlines()[1]
    body = json.loads(line.removeprefix("data: "))
    assert body == {
        "attempt": 1,
        "slot": "general",
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "requested_slot": "general",
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "message_id": "22222222-2222-4222-8222-222222222222",
    }


def test_delta_event_is_openai_shaped() -> None:
    frame = format_event(DeltaEvent.of("Hel"))

    assert frame.startswith(b"event: delta\n")
    body = json.loads(frame.decode().splitlines()[1].removeprefix("data: "))
    assert body == {"choices": [{"delta": {"content": "Hel"}}]}


def test_restart_event_names_the_failed_and_next_candidate() -> None:
    # `model_validate` rather than the constructor: the nested candidate shapes
    # are module-private types, and a dict is exactly what Step 9's orchestrator
    # will actually have on hand when it builds one of these.
    event = RestartEvent.model_validate(
        {
            "reason": "provider_unavailable",
            "failed": {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            "next": {"slot": "general", "provider": "gemini", "model": "gemini-flash"},
            "attempt": 2,
            "discarded_chars": 412,
        }
    )

    frame = format_event(event)

    assert frame.startswith(b"event: restart\n")
    body = json.loads(frame.decode().splitlines()[1].removeprefix("data: "))
    assert body["reason"] == "provider_unavailable"
    assert body["failed"] == {"provider": "groq", "model": "llama-3.3-70b-versatile"}
    assert body["next"] == {"slot": "general", "provider": "gemini", "model": "gemini-flash"}
    assert body["attempt"] == 2
    assert body["discarded_chars"] == 412


def test_done_event_usage_is_the_non_streaming_shape_plus_wasted_tokens() -> None:
    """`DoneEvent.usage` is deliberately `UsageOut` plus one field — the frontend
    is built to render one provenance shape for both a streamed and a
    non-streamed answer, and a second usage shape would break that."""
    event = DoneEvent(
        served_by=_served_by(),
        requested_slot="general",
        substituted=True,
        attempts=2,
        usage=StreamUsage(
            prompt_tokens=812, completion_tokens=340, total_tokens=1152, wasted_tokens_out=96
        ),
        degraded=False,
        status="ok",
    )

    frame = format_event(event)

    assert frame.startswith(b"event: done\n")
    body = json.loads(frame.decode().splitlines()[1].removeprefix("data: "))
    assert body["served_by"] == {"slot": "general", "provider": "gemini", "model": "gemini-flash"}
    assert body["substituted"] is True
    assert body["attempts"] == 2
    assert body["usage"] == {
        "prompt_tokens": 812,
        "completion_tokens": 340,
        "total_tokens": 1152,
        "estimated": False,
        "wasted_tokens_out": 96,
    }
    assert body["degraded"] is False
    assert body["status"] == "ok"
    assert "partial_content" in body
    assert body["partial_content"] is None


def test_done_event_carries_partial_content_only_when_set() -> None:
    event = DoneEvent(
        served_by=_served_by(),
        requested_slot="general",
        substituted=False,
        attempts=3,
        usage=StreamUsage(prompt_tokens=10, completion_tokens=0, total_tokens=10),
        degraded=False,
        status="failed",
        partial_content="the answer got this f",
    )

    body = json.loads(format_event(event).decode().splitlines()[1].removeprefix("data: "))

    assert body["status"] == "failed"
    assert body["partial_content"] == "the answer got this f"


def test_every_frame_ends_with_the_blank_line_that_terminates_an_sse_event() -> None:
    for event in (
        DeltaEvent.of("x"),
        DoneEvent(
            served_by=_served_by(),
            requested_slot="general",
            substituted=False,
            attempts=1,
            usage=StreamUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            degraded=False,
            status="ok",
        ),
    ):
        assert format_event(event).endswith(b"\n\n")


def test_events_reject_unknown_fields() -> None:
    """`extra="forbid"` — a typo'd key in whatever constructs these (Step 9's
    orchestrator) should fail loudly at construction, not silently ship an
    extra wire field."""
    with pytest.raises(ValidationError):
        MetaEvent(
            attempt=1,
            slot="general",
            provider="groq",
            model="llama-3.3-70b-versatile",
            requested_slot="general",
            conversation_id="11111111-1111-4111-8111-111111111111",
            message_id="22222222-2222-4222-8222-222222222222",
            bogus=1,  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# format_heartbeat
# --------------------------------------------------------------------------- #
def test_heartbeat_is_a_comment_line_not_a_named_event() -> None:
    """A comment (no `event:`/`data:` fields) is valid SSE that every client's
    `onmessage`/named-listener machinery silently ignores."""
    heartbeat = format_heartbeat()

    assert heartbeat.startswith(b":")
    assert heartbeat.endswith(b"\n\n")
    assert b"event:" not in heartbeat
    assert b"data:" not in heartbeat


def test_heartbeat_interval_is_comfortably_under_a_minute() -> None:
    """Guards the constant against creeping up past what a proxy's or client's
    own idle-connection timeout would tolerate."""
    assert 0 < HEARTBEAT_INTERVAL_S < 60.0


# --------------------------------------------------------------------------- #
# Response headers
# --------------------------------------------------------------------------- #
def test_the_media_type_is_the_sse_content_type() -> None:
    assert SSE_MEDIA_TYPE == "text/event-stream"


def test_headers_disable_proxy_buffering() -> None:
    """The header that actually bites: without it an nginx-family proxy
    buffers the whole body and the stream never reaches the client
    incrementally, in production only — every test still passes."""
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"
    assert SSE_HEADERS["Cache-Control"] == "no-cache"
    assert SSE_HEADERS["Connection"] == "keep-alive"
    # Content-Type is carried by `StreamingResponse(media_type=SSE_MEDIA_TYPE)`,
    # not duplicated into this dict, so there is exactly one place it is set.
    assert "Content-Type" not in SSE_HEADERS


# --------------------------------------------------------------------------- #
# client_disconnected
# --------------------------------------------------------------------------- #
def _request(*, disconnected: bool) -> Request:
    message: dict[str, object] = (
        {"type": "http.disconnect"}
        if disconnected
        else {"type": "http.request", "body": b"", "more_body": False}
    )

    async def receive() -> dict[str, object]:
        return message

    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    return Request(scope, receive)


async def test_client_disconnected_is_true_once_the_socket_is_gone() -> None:
    assert await client_disconnected(_request(disconnected=True)) is True


async def test_client_disconnected_is_false_for_a_live_connection() -> None:
    assert await client_disconnected(_request(disconnected=False)) is False
