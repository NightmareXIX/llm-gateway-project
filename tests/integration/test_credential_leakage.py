"""Phase 6 Step 8 — the key never reaches a log.

`development-plan.md`'s own exit criterion for BYOK is "grep all logs for the
key string -> zero hits", written as a manual step. A manual grep passes once;
this file makes it pass forever by driving the real add/list/turn/turn/
AuthFailed/remove lifecycle from `phase6.md`'s own script through the real
app, capturing everything structlog emits along the way (the same technique
`tests/unit/test_logging.py` establishes: attach a handler to the root logger
and parse the JSON that actually comes out, since `structlog.testing
.capture_logs` drops `merge_contextvars` and would prove nothing), and
asserting a sentinel plaintext and its Fernet ciphertext appear in neither the
captured log lines, the response bodies, nor the `requests.attempts` JSONB
this suite writes.

**What this covers.** Every log line structlog renders during: adding a
private key (`POST /v1/provider-keys`, live `validate_key`), listing keys,
a non-streaming turn served by it, a streaming turn served by it, a live
`AuthFailed` on it (D40's disclosure path — the row really does flip to
`'invalid'`, proving the write itself never carries the key either), and
removing it. Also covers the realistic leak Step 8's own plan calls out by
name: an unhandled exception raised while the plaintext key is a local
variable in scope, confirming `unhandled_exception_handler`'s output (both
the response and the log line) carries neither the plaintext nor a `repr` of
anything holding it — which only holds because `structlog.processors
.format_exc_info` renders a plain Python traceback (file, line, source line)
and never a variable dump; a switch to a "rich" traceback renderer with
`capture_locals=True` would break this test, which is the point of having it.

**What this cannot cover.** A provider's own logs (Groq/Gemini never see the
gateway's log stream) and anything written before this test existed — a log
aggregator's retained history from an earlier, leakier build is outside any
test's reach. This is a regression test for exactly the scenarios it drives,
not a formal proof that no log line anywhere can ever carry a credential.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ProvidersConfig, get_providers_config
from app.db.models import Request
from app.db.repo import provider_keys as provider_keys_repo
from app.providers.registry import build_registry
from tests import provider_fixtures
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"
PROVIDER_KEYS = "/v1/provider-keys"

SENTINEL_KEY = "gsk_sentinel_leak_test_9f8e7d6c5b4a1230"
"""Distinctive enough to grep for and to never collide with a real fixture
string (Groq keys look nothing like this, on purpose)."""


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


def _groq_only() -> ProvidersConfig:
    """Copied from `test_chat_endpoint.py`/`test_provider_keys_endpoints.py`
    rather than imported — a one-file helper duplicated a third time is
    cheaper than a cross-module test dependency."""
    config = get_providers_config()
    providers = {
        name: entry if name == "groq" else entry.model_copy(update={"enabled": False})
        for name, entry in config.providers.items()
    }
    return config.model_copy(update={"providers": providers})


class _FailoverHandler:
    """Groq answers every request with ``AuthFailed``; Gemini answers with a
    real success — the transport D40's scenario needs: a private Groq key
    that fails live, and a failover that lands on Gemini's shared key.
    OpenRouter is never expected to be reached and raises loudly if it is.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if "groq" in host:
            return provider_fixtures.load("groq", "auth_failed").to_response()
        if "generativelanguage" in host:
            return provider_fixtures.load("gemini", "success").to_response()
        raise AssertionError(f"unexpected request to {request.url} — openrouter should not be hit")

    def client(self) -> httpx.AsyncClient:
        return provider_fixtures.client_from(self)


@contextmanager
def _captured_logs() -> Iterator[list[dict[str, Any]]]:
    """Every JSON line structlog renders inside the block, decoded.

    Attaches a handler directly to the root logger and reads
    ``record.getMessage()`` — the exact bytes production ships, since
    ``logging.basicConfig(format="%(message)s", ...)`` means structlog's
    ``JSONRenderer`` output *is* the message. See ``tests/unit/test_logging
    .py``'s module docstring for why ``structlog.testing.capture_logs`` would
    not do: it replaces the processor chain and drops ``merge_contextvars``.
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
        for line in records:
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    lines.append(json.loads(stripped))
                except ValueError:
                    continue


def _assert_absent_everywhere(
    *,
    logs: list[dict[str, Any]],
    responses: list[httpx.Response],
    request_rows: list[Request],
    sentinel: str,
    ciphertext: str,
) -> None:
    for line in logs:
        rendered = json.dumps(line)
        assert sentinel not in rendered, f"plaintext leaked into a log line: {line}"
        assert ciphertext not in rendered, f"ciphertext leaked into a log line: {line}"

    for response in responses:
        assert sentinel not in response.text, f"plaintext leaked into a response: {response.text}"
        assert ciphertext not in response.text, (
            f"ciphertext leaked into a response: {response.text}"
        )

    for row in request_rows:
        rendered = json.dumps(row.attempts)
        assert sentinel not in rendered, f"plaintext leaked into requests.attempts: {row.attempts}"
        assert ciphertext not in rendered, (
            f"ciphertext leaked into requests.attempts: {row.attempts}"
        )


async def _all_requests(session: AsyncSession) -> list[Request]:
    result = await session.execute(select(Request).order_by(Request.created_at))
    return list(result.scalars().all())


async def test_the_full_byok_lifecycle_never_logs_the_key(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    app: FastAPI,
) -> None:
    """`phase6.md` Step 8's own script: add -> list -> a non-streaming turn ->
    a streaming turn -> a live ``AuthFailed`` on the private key (D40) ->
    remove. The sentinel and its ciphertext must appear in no captured log
    line, no response body, and no stored ``requests.attempts`` row."""
    user_id = uuid4()
    headers = _headers(make_jwt, sub=user_id)
    responses: list[httpx.Response] = []

    with _captured_logs() as logs:
        # 1. Add the key. `validate_key` hits Groq's GET /models.
        validate_transport = provider_fixtures.client_returning(
            provider_fixtures.load("groq", "models_list")
        )
        app.state.provider_registry = build_registry(client=validate_transport, config=_groq_only())
        added = await client.post(
            PROVIDER_KEYS,
            json={"provider": "groq", "key": SENTINEL_KEY, "nickname": "leak test key"},
            headers=headers,
        )
        await validate_transport.aclose()
        responses.append(added)
        assert added.status_code == 201, added.text

        # 2. List keys.
        listed = await client.get(PROVIDER_KEYS, headers=headers)
        responses.append(listed)
        assert listed.status_code == 200

        # 3. A non-streaming turn, served by the private key.
        success_fixture = provider_fixtures.load("groq", "success")
        success_handler = provider_fixtures.RecordingHandler(success_fixture)
        success_transport = success_handler.client()
        app.state.provider_registry = build_registry(client=success_transport, config=_groq_only())
        turn1 = await client.post(
            COMPLETIONS,
            json={"messages": [{"role": "user", "content": "what is a gateway?"}]},
            headers=headers,
        )
        await success_transport.aclose()
        responses.append(turn1)
        assert turn1.status_code == 200, turn1.text
        assert turn1.json()["key_pool"] == "private"
        assert success_handler.last.headers["authorization"] == f"Bearer {SENTINEL_KEY}"

        # 4. A streaming turn, also served by the private key.
        stream_transport = provider_fixtures.client_streaming(
            [provider_fixtures.read_sse("groq", "stream_success").encode("utf-8")]
        )
        app.state.provider_registry = build_registry(client=stream_transport, config=_groq_only())
        turn2 = await client.post(
            COMPLETIONS,
            json={
                "conversation_id": turn1.json()["conversation_id"],
                "messages": [{"role": "user", "content": "and again, streamed"}],
                "stream": True,
            },
            headers=headers,
        )
        await stream_transport.aclose()
        responses.append(turn2)
        assert turn2.status_code == 200, turn2.text
        assert "event: done" in turn2.text

        # 5. Force a live `AuthFailed` on the private key. D40: the chain
        # fails over to Gemini's shared key rather than laundering the
        # traffic through it, and the row is flagged rather than silently
        # retried on a different credential.
        failover_handler = _FailoverHandler()
        app.state.provider_registry = build_registry(
            client=failover_handler.client(), config=get_providers_config()
        )
        turn3 = await client.post(
            COMPLETIONS,
            json={
                "conversation_id": turn1.json()["conversation_id"],
                "model": "general",
                "messages": [{"role": "user", "content": "third turn, over a dead key"}],
            },
            headers=headers,
        )
        responses.append(turn3)
        assert turn3.status_code == 200, turn3.text
        assert turn3.json()["key_pool"] == "shared"
        assert turn3.json()["served_by"]["provider"] == "gemini"

        # 6. Remove the key.
        removed = await client.delete(f"{PROVIDER_KEYS}/groq", headers=headers)
        responses.append(removed)
        assert removed.status_code == 204

    # D40's disclosure actually happened — proving the write path this test
    # exercises is real, not merely silent.
    rows = await provider_keys_repo.list_for_user(db_session, user_id)
    assert len(rows) == 1
    assert rows[0].validation_status == "invalid"
    ciphertext = rows[0].encrypted_key
    assert ciphertext != SENTINEL_KEY

    request_rows = await _all_requests(db_session)
    assert len(request_rows) >= 3  # the two private turns and the failover

    _assert_absent_everywhere(
        logs=logs,
        responses=responses,
        request_rows=request_rows,
        sentinel=SENTINEL_KEY,
        ciphertext=ciphertext,
    )


class _RaisingHandler:
    """Groq's transport raises a bare, non-``httpx.HTTPError`` exception —
    the shape a genuine bug in the transport layer would take, and one
    ``ProviderAdapter._request``'s ``except httpx.HTTPError`` does not catch.
    It propagates through the router, uncaught, to FastAPI's catch-all —
    exactly the realistic leak Step 8's own plan names: a traceback rendered
    while the plaintext key was a local variable in an enclosing frame.
    """

    def __call__(self, request: httpx.Request) -> httpx.Response:
        raise RuntimeError("deliberate transport bug for the leak test")


async def test_an_unhandled_exception_with_a_private_key_in_scope_leaks_nothing(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
    app: FastAPI,
) -> None:
    """The key is resolved (``resolved.key``, a local in ``router.route``'s
    frame) before the transport ever raises, so it is genuinely in scope when
    the unhandled-exception handler runs. The assertion is that neither the
    500 envelope nor the log line it writes carries the plaintext — true only
    because ``structlog.processors.format_exc_info`` renders a plain
    traceback (file/line/source line) and never a local-variable dump.
    """
    user_id = uuid4()
    headers = _headers(make_jwt, sub=user_id)

    validate_transport = provider_fixtures.client_returning(
        provider_fixtures.load("groq", "models_list")
    )
    app.state.provider_registry = build_registry(client=validate_transport, config=_groq_only())
    added = await client.post(
        PROVIDER_KEYS,
        json={"provider": "groq", "key": SENTINEL_KEY},
        headers=headers,
    )
    await validate_transport.aclose()
    assert added.status_code == 201, added.text

    row = await provider_keys_repo.get_active(db_session, user_id=user_id, provider="groq")
    assert row is not None
    ciphertext = row.encrypted_key

    with _captured_logs() as logs:
        crashing_transport = provider_fixtures.client_from(_RaisingHandler())
        app.state.provider_registry = build_registry(client=crashing_transport, config=_groq_only())
        response = await client.post(
            COMPLETIONS,
            json={"messages": [{"role": "user", "content": "trigger the bug"}]},
            headers=headers,
        )
        await crashing_transport.aclose()

    assert response.status_code == 500

    _assert_absent_everywhere(
        logs=logs,
        responses=[response],
        request_rows=[],
        sentinel=SENTINEL_KEY,
        ciphertext=ciphertext,
    )
