"""The published schema has to describe the envelope the app actually returns.

The schema is a contract in its own right — clients get generated from it. Left
alone, FastAPI documents its own ``HTTPValidationError`` (``{"detail": [...]}``)
on every route taking a body or a path parameter, a shape nothing in this app has
ever produced.

``create_app()`` is called directly rather than through the ``app`` fixture: that
fixture pulls in ``test_database_url``, and asserting on a static document should
not require a live Postgres. ``conftest.py`` has already set the environment by
import time, and the engine connects lazily.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.main import create_app

REF_ERROR_RESPONSE = "#/components/schemas/ErrorResponse"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    openapi: dict[str, Any] = create_app().openapi()
    return openapi


def operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Every (path, method, operation) in the document."""
    methods = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
    return [
        (path, method, operation)
        for path, item in schema["paths"].items()
        for method, operation in item.items()
        if method in methods
    ]


def response_ref(operation: dict[str, Any], status_code: int) -> str | None:
    """The ``$ref`` of a documented response's JSON schema, if it has one."""
    response = operation["responses"].get(str(status_code))
    if response is None:
        return None
    content = response.get("content", {}).get("application/json", {})
    ref: str | None = content.get("schema", {}).get("$ref")
    return ref


def test_fastapis_own_error_shape_is_not_published(schema: dict[str, Any]) -> None:
    """The headline assertion: no route documents ``{"detail": ...}``.

    Declaring a 422 ourselves is what suppresses it — FastAPI only injects its
    default when the operation does not already describe one — so this failing
    means some route lost its declaration.
    """
    components = schema["components"]["schemas"]

    assert "HTTPValidationError" not in components
    assert "ValidationError" not in components
    assert "detail" not in str(schema["paths"])


def test_the_envelope_is_published(schema: dict[str, Any]) -> None:
    components = schema["components"]["schemas"]

    assert "ErrorResponse" in components
    assert "ErrorBody" in components
    assert set(components["ErrorBody"]["properties"]) == {
        "code",
        "message",
        "request_id",
        "details",
    }


@pytest.mark.parametrize("status_code", [422, 500])
def test_every_route_documents_the_universal_failures(
    schema: dict[str, Any], status_code: int
) -> None:
    """Any route can fail validation or hit a bug, so every route says so."""
    for path, method, operation in operations(schema):
        assert response_ref(operation, status_code) == REF_ERROR_RESPONSE, (
            f"{method.upper()} {path} does not document {status_code} as the envelope"
        )


def test_authenticated_routes_document_401(schema: dict[str, Any]) -> None:
    """Everything under /v1 is behind get_principal."""
    v1_operations = [
        (path, method, operation)
        for path, method, operation in operations(schema)
        if path.startswith("/v1")
    ]
    assert v1_operations, "no /v1 routes found — the routers are not mounted"

    for path, method, operation in v1_operations:
        assert response_ref(operation, 401) == REF_ERROR_RESPONSE, (
            f"{method.upper()} {path} does not document a 401"
        )


def test_health_checks_do_not_claim_to_need_credentials(schema: dict[str, Any]) -> None:
    """The counterpart: the small sets stay honest in the other direction too."""
    for path in ("/healthz", "/readyz"):
        assert "401" not in schema["paths"][path]["get"]["responses"]


def test_readyz_documents_its_503(schema: dict[str, Any]) -> None:
    assert response_ref(schema["paths"]["/readyz"]["get"], 503) == REF_ERROR_RESPONSE
