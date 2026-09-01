"""The error envelope — one shape for every failure the gateway returns.

Defined before the first endpoint, on purpose. A client that learns to read

.. code-block:: json

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {}}}

on day one never has to learn a second shape, and ``request_id`` means a user
report ("it said something went wrong") is always traceable to a log line.

These are wire models only. The exceptions that produce them live in
``app/core/errors.py``; nothing here imports from the rest of the app.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """The contents of the ``error`` key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    """Stable, machine-readable, snake_case. Clients branch on this, never on
    ``message`` — the message is free to be reworded, the code is not."""

    message: str
    """Human-readable and safe to display. Never contains an exception string,
    a stack frame, a SQL fragment, or a credential."""

    request_id: str | None = None
    """The id echoed in the ``X-Request-ID`` header and bound to every log line
    emitted while handling this request. ``None`` only outside a request context."""

    details: dict[str, Any] = Field(default_factory=dict)
    """Structured extras a client can act on — validation failures, ``retry_after_s``
    on a rate limit, the ``slot`` that was unavailable. Empty by default."""


class ErrorResponse(BaseModel):
    """The full response body. Every non-2xx response the gateway emits is one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorBody


# --------------------------------------------------------------------------- #
# OpenAPI documentation
# --------------------------------------------------------------------------- #
# Left undeclared, FastAPI documents its own ``HTTPValidationError`` — a
# ``{"detail": [...]}`` body — on every route that takes a body or a path
# parameter. Nothing in this app has ever returned that shape, so a client
# generated from the schema would branch on a field that does not exist.
# Declaring a 422 ourselves suppresses it: FastAPI only injects its default when
# the operation does not already describe one.
#
# The sets are deliberately small. Documenting 404 or 502 on every route would be
# a different flavour of the same lie — the schema should say what a route can
# actually return, so anything beyond these goes on the route that raises it.


def _error_doc(description: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": description}


DEFAULT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: _error_doc("The request failed validation."),
    500: _error_doc("An unexpected error occurred."),
}
"""Applies to every route, including the health checks. Set on the app itself."""

AUTHENTICATED_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **DEFAULT_ERROR_RESPONSES,
    401: _error_doc("Missing or invalid credentials."),
    403: _error_doc("Authenticated, but not permitted to do this."),
}
"""For routers behind ``get_principal``. Set on the router, so the fact that its
routes need credentials is declared by the thing that requires them."""

NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: _error_doc("No such resource, or it belongs to someone else."),
}
"""Per-route. Ownership misses are 404s, so this covers both cases."""

CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: _error_doc(
        "The request conflicts with one already made — a reused `Idempotency-Key`, "
        "or one whose original request is still in flight."
    ),
}
"""Per-route, like :data:`NOT_FOUND_RESPONSE`. D6's two 409s
(``idempotency_key_reuse`` and ``idempotency_in_flight``) are the only ones the
gateway emits, so this is declared on the one route that can raise them."""
