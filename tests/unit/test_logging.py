"""Request context: the ``request_id`` that makes a user report traceable.

The error envelope returns a ``request_id`` on every failure and promises it names
a log line. That promise has two halves — the id has to reach the response, and it
has to reach the log — and this module is where both are checked.

**Why the real pipeline and not** ``structlog.testing.capture_logs``. That helper
replaces the configured processor chain, which drops ``merge_contextvars`` — the
one processor whose job is putting ``request_id`` and ``user_id`` on every line.
Testing through it would assert the fields are absent and call it a pass. So the
tests below attach a handler to the root logger and parse the JSON that actually
comes out, which is the same bytes production ships.

[test_errors.py](tests/unit/test_errors.py) covers the envelope's side of this
against a synthetic app. Here the subject is the middleware itself, including the
paths an endpoint test cannot reach: a handler that raises, a non-HTTP scope, and
``request_id_for`` running *after* the contextvar has been cleared — which is the
ordering every 500 goes through.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from app.core.logging import (
    REQUEST_ID_HEADER,
    REQUEST_ID_SCOPE_KEY,
    RequestContextMiddleware,
    bind_request_id,
    bind_user_id,
    clear_log_context,
    configure_logging,
    get_logger,
    get_request_id,
    get_user_id,
    new_request_id,
    request_id_for,
)


@pytest.fixture(autouse=True)
def clean_context() -> Iterator[None]:
    """No test inherits another's bound ids, and none leaks its own."""
    configure_logging()
    clear_log_context()
    yield
    clear_log_context()


@contextmanager
def json_logs() -> Iterator[list[dict[str, Any]]]:
    """Collect the JSON lines emitted inside the block.

    Populated on exit, so assertions belong after the ``with``. Reading the
    rendered line rather than the event dict is the point — it is what proves the
    contextvars survived all the way through ``JSONRenderer``.
    """
    stream = logging.StreamHandler()
    records: list[str] = []
    stream.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]

    root = logging.getLogger()
    root.addHandler(stream)
    lines: list[dict[str, Any]] = []
    try:
        yield lines
    finally:
        root.removeHandler(stream)
        lines.extend(json.loads(line) for line in records if line.strip().startswith("{"))


# --------------------------------------------------------------------------- #
# The middleware, over a real ASGI app
# --------------------------------------------------------------------------- #
async def _ok(request: Any) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _boom(request: Any) -> PlainTextResponse:
    raise RuntimeError("deliberate")


def _app() -> Starlette:
    application = Starlette(routes=[Route("/ok", _ok), Route("/boom", _boom)])
    application.add_middleware(RequestContextMiddleware)
    return application


@pytest.fixture
def client() -> Iterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_a_request_without_an_id_is_given_one(client: httpx.AsyncClient) -> None:
    response = await client.get("/ok")

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]


async def test_an_inbound_id_is_honoured_verbatim(client: httpx.AsyncClient) -> None:
    """So a request can be traced across the frontend and a proxy, not just here."""
    response = await client.get("/ok", headers={REQUEST_ID_HEADER: "traced-from-upstream"})

    assert response.headers[REQUEST_ID_HEADER] == "traced-from-upstream"


async def test_two_requests_do_not_share_an_id(client: httpx.AsyncClient) -> None:
    first = await client.get("/ok")
    second = await client.get("/ok")

    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_the_completed_line_carries_the_request_and_its_id(
    client: httpx.AsyncClient,
) -> None:
    with json_logs() as lines:
        response = await client.get("/ok?q=1", headers={REQUEST_ID_HEADER: "rid-1"})

    assert response.status_code == 200
    completed = [line for line in lines if line["event"] == "request.completed"]
    assert len(completed) == 1

    line = completed[0]
    assert line["request_id"] == "rid-1"
    assert line["method"] == "GET"
    assert line["path"] == "/ok"
    assert line["status_code"] == 200
    assert isinstance(line["duration_ms"], int | float)


async def test_a_handler_that_raises_is_still_logged(client: httpx.AsyncClient) -> None:
    """The ``finally``. An exception is the case where you most want the line."""
    with json_logs() as lines:
        await client.get("/boom", headers={REQUEST_ID_HEADER: "rid-boom"})

    completed = [line for line in lines if line["event"] == "request.completed"]
    assert len(completed) == 1
    assert completed[0]["request_id"] == "rid-boom"
    # No response ever started, so the default stands rather than a stale 200.
    assert completed[0]["status_code"] == 500


async def test_the_context_is_cleared_when_the_request_ends(client: httpx.AsyncClient) -> None:
    """Otherwise a background task, or the next request on this worker, would log
    under an id belonging to somebody else's call."""
    await client.get("/ok")

    assert get_request_id() is None
    assert get_user_id() is None


async def test_a_bound_user_id_reaches_every_later_line() -> None:
    """What the auth dependency does, and why every line after it is attributable."""
    bind_request_id("rid-2")
    bind_user_id("user-7")

    with json_logs() as lines:
        get_logger("test").info("something.happened", extra_field="kept")

    assert len(lines) == 1
    assert lines[0]["request_id"] == "rid-2"
    assert lines[0]["user_id"] == "user-7"
    assert lines[0]["extra_field"] == "kept"


async def test_a_non_http_scope_passes_straight_through() -> None:
    """Lifespan and websocket traffic have no request to identify."""
    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    async def receive() -> Message:  # pragma: no cover - never awaited
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:  # pragma: no cover - never called
        raise AssertionError("nothing should be sent")

    scope: Scope = {"type": "lifespan"}
    await RequestContextMiddleware(inner)(scope, receive, send)

    assert seen == [scope]
    assert "state" not in scope
    assert get_request_id() is None


# --------------------------------------------------------------------------- #
# request_id_for — the scope stash
# --------------------------------------------------------------------------- #
class _FakeRequest:
    """The structural minimum ``HasScope`` asks for."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope


def test_the_id_survives_the_contextvar_being_cleared() -> None:
    """Exception handlers run *above* the middleware, so by the time a 500 is
    rendered the ``finally`` has already cleared the contextvar. The scope stash
    is the only thing left holding the id, and this is the ordering that proves
    the envelope on a 500 is not blank."""
    request = _FakeRequest({"type": "http", "state": {REQUEST_ID_SCOPE_KEY: "stashed"}})
    clear_log_context()

    assert get_request_id() is None
    assert request_id_for(request) == "stashed"


def test_the_stash_wins_over_a_contextvar_from_another_request() -> None:
    bind_request_id("some-other-request")
    request = _FakeRequest({"type": "http", "state": {REQUEST_ID_SCOPE_KEY: "mine"}})

    assert request_id_for(request) == "mine"


@pytest.mark.parametrize(
    "scope",
    [
        {"type": "http"},
        {"type": "http", "state": {}},
        {"type": "http", "state": "not-a-dict"},
        {"type": "http", "state": {REQUEST_ID_SCOPE_KEY: 12345}},
    ],
)
def test_an_unusable_stash_falls_back_rather_than_raising(scope: Scope) -> None:
    """Starlette does not guarantee ``state``, and a non-string there would be our
    bug. Either way the handler still has to render a response."""
    bind_request_id("from-the-contextvar")

    assert request_id_for(_FakeRequest(scope)) == "from-the-contextvar"


def test_a_caller_with_no_request_reads_the_contextvar() -> None:
    bind_request_id("rid-3")

    assert request_id_for(None) == "rid-3"


def test_nothing_bound_anywhere_is_none_not_an_invention() -> None:
    """A made-up id is worse than no id: it sends someone searching the logs for a
    line that was never written."""
    assert request_id_for(None) is None


# --------------------------------------------------------------------------- #
# Renderer and ids
# --------------------------------------------------------------------------- #
def test_every_line_is_json_with_a_level_and_a_timestamp() -> None:
    """JSON in every environment, dev included — the thing you debug locally is
    the thing you ship."""
    with json_logs() as lines:
        get_logger("test").warning("shape.check")

    assert len(lines) == 1
    assert lines[0]["event"] == "shape.check"
    assert lines[0]["level"] == "warning"
    assert lines[0]["timestamp"].endswith("Z")


def test_an_exception_is_rendered_into_the_line_not_dropped() -> None:
    with json_logs() as lines:
        try:
            raise ValueError("something specific")
        except ValueError:
            get_logger("test").error("it.failed", exc_info=True)

    assert "something specific" in lines[0]["exception"]


def test_request_ids_are_unique_and_url_safe() -> None:
    ids = {new_request_id() for _ in range(500)}

    assert len(ids) == 500
    assert all(len(value) == 32 and value.isalnum() for value in ids)
