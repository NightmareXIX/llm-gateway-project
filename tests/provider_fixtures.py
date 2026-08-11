"""Replay recorded provider responses through ``httpx.MockTransport``.

The seam that makes "never call a live provider from a test" practical rather
than aspirational. A fixture file holds a status, headers and a body; this module
turns one into an ``httpx.Response`` and a client that serves it.

Lives outside ``conftest.py`` deliberately: these are plain functions the
contract suite and the unit suite both import directly, and pytest fixtures would
force every caller into the dependency-injection style whether it helps or not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from app.config import REPO_ROOT
from app.memory.canonical import (
    CanonicalMessage,
    ContentBlock,
    MessageMeta,
    Role,
    omission_marker,
    text_block,
)

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "provider_responses"
GOLDEN_ROOT = REPO_ROOT / "tests" / "fixtures" / "golden_payloads"


@dataclass(frozen=True)
class RecordedResponse:
    """One captured provider response, ready to be replayed."""

    name: str
    source: str
    status: int
    headers: dict[str, str]
    body: dict[str, Any] | None
    text: str | None

    @property
    def is_live(self) -> bool:
        """Whether this came from a real provider or was hand-authored.

        Not decoration: a suite that silently ran entirely on hand-written bodies
        would prove only that the adapter agrees with its author's guess about
        the wire format.
        """
        return self.source == "live"

    def to_response(self) -> httpx.Response:
        if self.text is not None:
            return httpx.Response(self.status, headers=self.headers, text=self.text)
        return httpx.Response(self.status, headers=self.headers, json=self.body)


def load(provider: str, name: str) -> RecordedResponse:
    """Read one fixture by name. Raises if it is missing or malformed."""
    path = FIXTURE_ROOT / provider / f"{name}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    response = raw["response"]

    return RecordedResponse(
        name=name,
        source=raw.get("source", "synthetic"),
        status=response["status"],
        headers=response.get("headers", {}),
        body=response.get("body"),
        text=response.get("text"),
    )


def load_all(provider: str) -> dict[str, RecordedResponse]:
    """Every JSON fixture for a provider, keyed by case name."""
    directory = FIXTURE_ROOT / provider
    return {path.stem: load(provider, path.stem) for path in sorted(directory.glob("*.json"))}


def read_sse(provider: str, name: str) -> str:
    """Raw SSE text for the Phase 2 streaming cases."""
    return (FIXTURE_ROOT / provider / f"{name}.sse").read_text(encoding="utf-8")


def client_returning(recorded: RecordedResponse) -> httpx.AsyncClient:
    """A client that answers every request with this fixture.

    The request is still built and still passes through httpx's own machinery, so
    a malformed payload or an unencodable header fails here exactly as it would
    against the real endpoint.
    """
    return client_from(lambda _request: recorded.to_response())


def client_from(handler: Any) -> httpx.AsyncClient:
    """A client backed by an arbitrary mock handler."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def client_raising(exc: Exception) -> httpx.AsyncClient:
    """A client whose transport always fails — the connection-error path."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return client_from(handler)


class RecordingHandler:
    """Serves a fixture and keeps the requests it was asked for.

    Used where the assertion is about what we *sent* — the payload Groq actually
    receives, the ``Authorization`` header's shape — rather than what came back.
    """

    def __init__(self, recorded: RecordedResponse) -> None:
        self.recorded = recorded
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.recorded.to_response()

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(self.last.content)
        return parsed

    def client(self) -> httpx.AsyncClient:
        return client_from(self)


def canonical_history() -> list[CanonicalMessage]:
    """The fixed six-message history every payload test renders (§2.1.5 case 1).

    Chosen to exercise the three things that differ between providers: a system
    message (in-array for OpenAI shapes, a top-level field for Gemini), an
    omission marker (D4's visible scar, which has no provider-native
    representation and must be rendered into the text), and strict user/assistant
    alternation.

    Every id and timestamp is fixed. A history built from ``uuid4()`` and
    ``now()`` would still produce a stable payload today — neither field is
    rendered — but the first adapter that includes a message id would make the
    golden file flap, and the cause would take an afternoon to find.
    """
    conversation_id = UUID("11111111-1111-4111-8111-111111111111")
    created = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

    def message(
        seq: int,
        role: Role,
        content: list[ContentBlock],
        *,
        meta: MessageMeta | None = None,
    ) -> CanonicalMessage:
        return CanonicalMessage(
            id=UUID(int=seq),
            conversation_id=conversation_id,
            role=role,
            content=content,
            meta=meta or MessageMeta(),
            created_at=created + timedelta(seconds=seq),
            seq=seq,
        )

    assistant_meta = MessageMeta(
        provider_used="groq",
        model_used="llama-3.3-70b-versatile",
        slot_used="general",
        requested_slot="general",
    )

    return [
        message(0, "system", [text_block("You are a terse assistant. Answer in one sentence.")]),
        message(
            1,
            "user",
            [
                omission_marker(4),
                text_block("Recap: what did we decide about mid-stream failover?"),
            ],
        ),
        message(
            2,
            "assistant",
            [text_block("Restart the stream on a new provider and discard the partial.")],
            meta=assistant_meta,
        ),
        message(3, "user", [text_block("And if the third attempt also fails?")]),
        message(
            4,
            "assistant",
            [text_block('The stream ends with status "failed" and the longest partial.')],
            meta=assistant_meta,
        ),
        message(5, "user", [text_block("Good. Now summarise that for the README.")]),
    ]


def read_golden(name: str) -> dict[str, Any]:
    """Load a committed golden payload."""
    parsed: dict[str, Any] = json.loads((GOLDEN_ROOT / f"{name}.json").read_text(encoding="utf-8"))
    return parsed


def dump_golden(payload: dict[str, Any]) -> str:
    """Serialize a payload the way golden files are stored.

    ``sort_keys`` on purpose: the assertion is about the payload's *content*, and
    a diff caused by two keys swapping insertion order is noise that trains
    people to re-bless golden files without reading them.
    """
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
