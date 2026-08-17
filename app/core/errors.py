"""Application errors and the exception handlers that render them.

Every failure leaves the gateway through one of the four handlers registered by
:func:`register_exception_handlers`, and every one of them produces the same
envelope (``app/schemas/errors.py``). There is no second shape and no bare
``{"detail": ...}`` anywhere — FastAPI's own ``HTTPException`` is caught and
re-rendered too.

Two rules the handlers exist to enforce:

**Never leak internals.** An unhandled exception is logged with its traceback and
answered with a generic 500. The client gets a ``request_id`` and nothing else;
the log line has the rest. Anything a client is meant to act on has to be raised
deliberately as an :class:`AppError` with a message written for a human.

**Always return the request id.** It is the only thing that turns "it said
something went wrong" into a log query.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import REQUEST_ID_HEADER, get_logger, request_id_for
from app.schemas.errors import ErrorBody, ErrorResponse

logger = get_logger("app.errors")

GENERIC_500_MESSAGE = "An unexpected error occurred. Quote the request_id if you report this."


# --------------------------------------------------------------------------- #
# The exception hierarchy
# --------------------------------------------------------------------------- #
class AppError(Exception):
    """Base class for every deliberate, client-visible failure.

    Subclasses set ``status_code``, ``code`` and a default ``message`` as class
    attributes; call sites override the message when they can say something more
    useful. Raising one of these is a statement that the message is safe to show
    to whoever made the request.
    """

    status_code: int = 500
    code: str = "internal_error"
    message: str = GENERIC_500_MESSAGE

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.code = code or type(self).code
        self.details: dict[str, Any] = details or {}
        self.headers: dict[str, str] = headers or {}
        super().__init__(self.message)


class InvalidRequest(AppError):
    """The request was understood but is not something we can act on."""

    status_code = 400
    code = "invalid_request"
    message = "The request could not be processed."


class Unauthenticated(AppError):
    """No credentials, or credentials that did not check out.

    ``code`` is almost always overridden at the raise site — ``invalid_token``,
    ``token_expired``, ``email_not_verified``, ``invalid_api_key``,
    ``missing_credentials`` — because the frontend's response differs for each
    (refresh the session, resend the confirmation mail, or bounce to login).
    """

    status_code = 401
    code = "unauthenticated"
    message = "Authentication is required."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        # RFC 6750: a 401 without this header is not a well-formed challenge.
        super().__init__(
            message,
            code=code,
            details=details,
            headers={"WWW-Authenticate": "Bearer", **(headers or {})},
        )


class Forbidden(AppError):
    """Authenticated, but not allowed to do this.

    Deliberately rare here. Asking for a resource owned by someone else is a
    :class:`NotFound`, not a 403 — a 403 confirms the thing exists.
    """

    status_code = 403
    code = "forbidden"
    message = "You do not have access to this resource."


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    message = "The requested resource does not exist."


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class TooManyRequests(AppError):
    """D20: *our* limit on *our* user, not a provider's limit on us.

    Named ``TooManyRequests`` rather than ``RateLimited`` precisely because
    :class:`app.providers.errors.RateLimited` exists and means the opposite
    direction — a provider refusing the gateway. Two identically-named classes in
    one traceback is a debugging tax with no upside.

    ``code`` is ``rate_limited``, which is what :data:`_CODE_BY_STATUS` already
    reserves for a 429, and the frontend already reads as a wait rather than a
    failure. ``Retry-After`` is delta-seconds — never an HTTP-date, which
    ``frontend/lib/api.ts`` says out loud and parses on that assumption.
    """

    status_code = 429
    code = "rate_limited"
    message = "You are sending requests faster than your tier allows. Try again shortly."

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after_s: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        merged_headers = dict(headers or {})
        merged_details = dict(details or {})
        if retry_after_s is not None:
            merged_headers.setdefault("Retry-After", str(retry_after_s))
            merged_details.setdefault("retry_after_s", retry_after_s)
        super().__init__(message, code=code, details=merged_details, headers=merged_headers)


class UpstreamUnavailable(AppError):
    """An upstream provider failed in a way we could not route around.

    Phase 1 raises this when Groq is unreachable or the key is dead; from Phase 2
    it means every candidate in the failover chain was exhausted.
    """

    status_code = 502
    code = "upstream_unavailable"
    message = "The upstream provider could not be reached."


class ServiceUnavailable(AppError):
    """A dependency of ours is down — the database, or later Redis."""

    status_code = 503
    code = "service_unavailable"
    message = "The service is temporarily unavailable."


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def error_response(
    request: Request | None,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build an envelope response. The single place a JSON error body is made."""
    request_id = request_id_for(request)
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=request_id,
            details=details or {},
        )
    )

    # The id goes on the header here, not only in `RequestContextMiddleware`.
    # A 500 is rendered by Starlette's `ServerErrorMiddleware`, which sits
    # *outside* ours, so that response never reaches the middleware's `send`
    # wrapper — the one failure class where a caller most wants an id to quote
    # would otherwise be the one that omits the header. Responses that do pass
    # through the middleware are unaffected: it assigns the same value with
    # `MutableHeaders.__setitem__`, which replaces rather than appends.
    response_headers = dict(headers or {})
    if request_id is not None:
        response_headers.setdefault(REQUEST_ID_HEADER, request_id)

    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=response_headers,
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Deliberate failures. Logged at warning — expected, but worth counting."""
    if not isinstance(exc, AppError):  # pragma: no cover — registered only for AppError
        return await unhandled_exception_handler(request, exc)
    logger.warning(
        "request.failed",
        code=exc.code,
        status_code=exc.status_code,
        path=request.url.path,
    )
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Pydantic rejected the request body, query, or path.

    422, not 400: the request was well-formed enough to parse and specifically
    failed validation, and the per-field errors go in ``details`` so a client can
    point at the offending field rather than re-read the docs.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        return await unhandled_exception_handler(request, exc)
    return error_response(
        request,
        status_code=422,
        code="invalid_request",
        message="The request failed validation.",
        # jsonable_encoder-safe: pydantic v2 errors can carry non-serializable
        # `ctx` values (exception instances), which would 500 the error handler.
        details={"errors": _serializable_errors(exc)},
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's and Starlette's own ``HTTPException``.

    Routing raises these before any of our code runs — an unknown path, a bad
    method. Without this handler those responses would be the one place in the
    API that answers with ``{"detail": ...}``.
    """
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        return await unhandled_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else "Request could not be completed."
    return error_response(
        request,
        status_code=exc.status_code,
        code=_CODE_BY_STATUS.get(exc.status_code, "http_error"),
        message=detail,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """The catch-all: a bug. Log everything, return nothing.

    Starlette re-raises after this returns, so uvicorn still records the
    traceback and a test client can opt into seeing it
    (``ASGITransport(raise_app_exceptions=False)`` to assert on the envelope).
    """
    logger.error(
        "request.unhandled_exception",
        path=request.url.path,
        method=request.method,
        exc_info=exc,
    )
    return error_response(
        request,
        status_code=500,
        code="internal_error",
        message=GENERIC_500_MESSAGE,
    )


_CODE_BY_STATUS: dict[int, str] = {
    400: "invalid_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_unavailable",
    503: "service_unavailable",
}


def _serializable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Reduce pydantic's error list to primitives that always serialize."""
    return [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()
    ]


def register_exception_handlers(app: FastAPI) -> None:
    """Install all four handlers. Called once, from ``create_app``."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
