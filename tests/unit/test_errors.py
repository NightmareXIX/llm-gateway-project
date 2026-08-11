"""The error envelope: one shape for every failure, and no internals in any of them."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.errors import NotFound, Unauthenticated, register_exception_handlers
from app.core.logging import REQUEST_ID_HEADER, RequestContextMiddleware
from tests.conftest import assert_envelope

SECRET_IN_A_TRACEBACK = "postgresql://gateway:hunter2@db.internal:5432/gateway"


class Body(BaseModel):
    count: int


@pytest.fixture
def error_app() -> FastAPI:
    """A minimal app wired exactly like the real one, with routes that fail."""
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/app-error")
    async def app_error() -> None:
        raise NotFound("No such conversation.", code="conversation_not_found")

    @app.get("/unauthenticated")
    async def unauthenticated() -> None:
        raise Unauthenticated("Nope.", code="invalid_token")

    @app.get("/with-details")
    async def with_details() -> None:
        raise NotFound("Gone.", code="gone", details={"slot": "llm2"})

    @app.post("/validated")
    async def validated(body: Body) -> Body:
        return body

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"connection to {SECRET_IN_A_TRACEBACK} failed")

    return app


@pytest.fixture
async def error_client(error_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=error_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_app_error_renders_its_code_and_status(error_client: httpx.AsyncClient) -> None:
    response = await error_client.get("/app-error")

    assert response.status_code == 404
    error = assert_envelope(response.json())
    assert error["code"] == "conversation_not_found"
    assert error["message"] == "No such conversation."


async def test_request_id_is_returned_and_matches_the_header(
    error_client: httpx.AsyncClient,
) -> None:
    """The whole point: a user quoting this can be found in the logs."""
    response = await error_client.get("/app-error")

    error = assert_envelope(response.json())
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_inbound_request_id_is_honoured(error_client: httpx.AsyncClient) -> None:
    """So a trace started at the proxy or the frontend survives into our logs."""
    response = await error_client.get("/app-error", headers={REQUEST_ID_HEADER: "trace-123"})

    error = assert_envelope(response.json())
    assert error["request_id"] == "trace-123"


async def test_401_carries_the_challenge_header(error_client: httpx.AsyncClient) -> None:
    response = await error_client.get("/unauthenticated")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_details_pass_through(error_client: httpx.AsyncClient) -> None:
    response = await error_client.get("/with-details")

    error = assert_envelope(response.json())
    assert error["details"] == {"slot": "llm2"}


async def test_validation_failure_is_a_422_in_the_envelope(
    error_client: httpx.AsyncClient,
) -> None:
    response = await error_client.post("/validated", json={"count": "not a number"})

    assert response.status_code == 422
    error = assert_envelope(response.json())
    assert error["code"] == "invalid_request"
    details = error["details"]
    assert isinstance(details, dict)
    assert details["errors"], "the offending field should be named"


async def test_unknown_route_is_not_fastapis_own_shape(error_client: httpx.AsyncClient) -> None:
    """Routing 404s go through the same handler as everything else."""
    response = await error_client.get("/no-such-route")

    assert response.status_code == 404
    error = assert_envelope(response.json())
    assert error["code"] == "not_found"


async def test_wrong_method_is_a_405_in_the_envelope(error_client: httpx.AsyncClient) -> None:
    """``/validated`` is POST-only. Starlette raises the 405 before our code runs."""
    response = await error_client.get("/validated")

    assert response.status_code == 405
    error = assert_envelope(response.json())
    assert error["code"] == "method_not_allowed"


async def test_unhandled_exception_leaks_nothing(error_client: httpx.AsyncClient) -> None:
    """A bug returns a request id and a generic message — never the exception."""
    response = await error_client.get("/boom")

    assert response.status_code == 500
    error = assert_envelope(response.json())
    assert error["code"] == "internal_error"

    body = response.text
    assert SECRET_IN_A_TRACEBACK not in body
    assert "hunter2" not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body
    assert error["request_id"]


async def test_500_still_carries_the_request_id_header(error_client: httpx.AsyncClient) -> None:
    """The header has to survive the one path that bypasses the middleware.

    ``ServerErrorMiddleware`` — which renders the catch-all's response — sits
    *outside* :class:`RequestContextMiddleware`, so the ``send`` wrapper that
    normally sets this header never sees a 500. ``error_response`` sets it itself
    for exactly this case; without that, the header is missing on the one failure
    class a user is most likely to be quoting an id from.
    """
    response = await error_client.get("/boom")

    error = assert_envelope(response.json())
    assert response.headers[REQUEST_ID_HEADER] == error["request_id"]
