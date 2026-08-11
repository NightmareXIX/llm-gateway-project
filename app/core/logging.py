"""Structured logging: JSON everywhere, with ``request_id`` and ``user_id`` bound
as contextvars so every line emitted while handling a request carries both.

Deliberately JSON in every environment, including dev. A pretty renderer locally
and JSON in prod means the thing you debug is not the thing you ship.

The middleware here is raw ASGI rather than Starlette's ``BaseHTTPMiddleware``:
``BaseHTTPMiddleware`` buffers through an anyio stream and interferes with
long-lived streaming responses, which is exactly what Phase 3's SSE lane is.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Protocol

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID_SCOPE_KEY = "request_id"
"""Key under ``scope["state"]``, which is what ``request.state`` reads."""


class HasScope(Protocol):
    """Structural type for ``starlette.requests.Request``.

    Typed structurally rather than imported so this module stays free of a
    Starlette request dependency — it is imported by ``app/core/errors.py``,
    which is imported by everything.
    """

    @property
    def scope(self) -> Scope: ...


request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON pipeline. Call once, before the app starts serving."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_request_id(request_id: str) -> None:
    """Bind a request id to both the contextvar and the log context."""
    request_id_var.set(request_id)
    structlog.contextvars.bind_contextvars(request_id=request_id)


def bind_user_id(user_id: str) -> None:
    """Bind the authenticated principal's user id. Called from the auth dependency."""
    user_id_var.set(user_id)
    structlog.contextvars.bind_contextvars(user_id=user_id)


def get_request_id() -> str | None:
    return request_id_var.get()


def request_id_for(request: HasScope | None) -> str | None:
    """The request id for an exception handler, contextvar or not.

    Handlers registered on the app run *outside* :class:`RequestContextMiddleware`
    — Starlette's ``ServerErrorMiddleware`` and ``ExceptionMiddleware`` sit above
    it — so by the time a 500 is rendered the middleware's ``finally`` has already
    cleared the contextvar. The middleware therefore also stashes the id in the
    ASGI scope, which outlives it; this reads that first and falls back to the
    contextvar for callers that have no request at all.
    """
    if request is not None:
        scope_state = request.scope.get("state")
        if isinstance(scope_state, dict):
            stashed = scope_state.get(REQUEST_ID_SCOPE_KEY)
            if isinstance(stashed, str):
                return stashed
    return request_id_var.get()


def get_user_id() -> str | None:
    return user_id_var.get()


def clear_log_context() -> None:
    request_id_var.set(None)
    user_id_var.set(None)
    structlog.contextvars.clear_contextvars()


class RequestContextMiddleware:
    """Per-request: assign a request id, bind it, echo it, log the outcome.

    An inbound ``X-Request-ID`` is honoured so a request can be traced across a
    proxy or the frontend; otherwise one is generated.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._logger = get_logger("app.request")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        clear_log_context()
        request_id = Headers(scope=scope).get(REQUEST_ID_HEADER) or new_request_id()
        bind_request_id(request_id)

        # Outlives this middleware's contextvars, so an exception handler running
        # above us can still name the request. See `request_id_for`.
        scope.setdefault("state", {})[REQUEST_ID_SCOPE_KEY] = request_id

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self._logger.info(
                "request.completed",
                method=scope.get("method"),
                path=scope.get("path"),
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            clear_log_context()
