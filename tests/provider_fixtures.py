"""Replay recorded provider responses through ``httpx.MockTransport``.

The seam that makes "never call a live provider from a test" practical rather
than aspirational. A fixture file holds a status, headers and a body; this module
turns one into an ``httpx.Response`` and a client that serves it.

Lives outside ``conftest.py`` deliberately: these are plain functions the
contract suite and the unit suite both import directly, and pytest fixtures would
force every caller into the dependency-injection style whether it helps or not.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from app.config import REPO_ROOT
from app.memory.canonical import (
    CanonicalMessage,
    ContentBlock,
    FileRefBlock,
    MessageMeta,
    Role,
    file_ref_block,
    omission_marker,
    text_block,
)
from app.providers.types import GenParams, ModelSpec, ResolvedAttachment

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "provider_responses"
GOLDEN_ROOT = REPO_ROOT / "tests" / "fixtures" / "golden_payloads"
FILES_ROOT = REPO_ROOT / "tests" / "fixtures" / "files"


@dataclass(frozen=True)
class RecordedResponse:
    """One captured provider response, ready to be replayed."""

    name: str
    source: str
    status: int
    headers: dict[str, str]
    body: dict[str, Any] | None
    text: str | None
    request_body: dict[str, Any] | None = None
    """The payload that produced this response, when the fixture recorded one.

    What makes the conformance suite's estimate check mean anything: without it,
    ``estimate_tokens`` gets measured against usage reported for a completely
    different prompt.
    """

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
        request_body=raw.get("request", {}).get("body"),
    )


def load_all(provider: str) -> dict[str, RecordedResponse]:
    """Every JSON fixture for a provider, keyed by case name."""
    directory = FIXTURE_ROOT / provider
    return {path.stem: load(provider, path.stem) for path in sorted(directory.glob("*.json"))}


def read_sse(provider: str, name: str) -> str:
    """Raw SSE text for the Phase 2 streaming cases."""
    return (FIXTURE_ROOT / provider / f"{name}.sse").read_text(encoding="utf-8")


class ScriptedByteStream(httpx.AsyncByteStream):
    """A response body that trickles out on a script, for streaming tests.

    A bare async generator does not satisfy ``httpx.Response(stream=...)`` —
    it checks ``isinstance(..., AsyncByteStream)`` rather than duck-typing —
    so genuinely delayed or mid-stream-failing bodies need a real subclass.
    Each scripted item is either a chunk of bytes to yield, a delay in seconds
    to await first (what makes an idle-timeout test deterministic instead of
    `sleep`-based guessing), or an exception to raise in place of a chunk
    (what makes a mid-stream transport fault reproducible).
    """

    def __init__(self, script: Sequence[bytes | float | BaseException]) -> None:
        self._script = script

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for item in self._script:
            if isinstance(item, float):
                await asyncio.sleep(item)
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item

    async def aclose(self) -> None:
        return None


def client_streaming(script: Sequence[bytes | float | BaseException]) -> httpx.AsyncClient:
    """A client whose response body plays out exactly as scripted."""
    return client_from(lambda _request: httpx.Response(200, stream=ScriptedByteStream(script)))


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


class ScriptedHandler:
    """Serves a *sequence* of fixtures — one per request, in order.

    What :class:`RecordingHandler` is for asserting on the payload we sent, this
    is for asserting on what the router does when the second answer differs from
    the first: a 429 then a 200 is a failover, a 503 then a 200 is a retry, and
    the difference between those two is the whole of Step 4.

    Faking at the transport layer rather than behind a fake ``ProviderAdapter``
    keeps every test in the repo mocking at one seam, and means the router is
    exercised through the real adapter's ``parse_error`` — which is where the
    normalized error it branches on actually comes from.

    An unscripted request is an assertion failure, not a repeat of the last
    response. "The loop tried a fourth time" is exactly the bug the attempt cap
    exists to prevent, and silently answering it would hide that.
    """

    def __init__(self, *script: RecordedResponse) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self.script):
            raise AssertionError(
                f"unscripted request #{index + 1} to {request.url}; "
                f"the script holds {len(self.script)}"
            )
        return self.script[index].to_response()

    @property
    def calls(self) -> int:
        """How many requests actually left the process."""
        return len(self.requests)

    def models(self) -> list[str]:
        """The model each request targeted, in order.

        The candidate chain, as observed from the wire — which is a stronger
        assertion than the router's own report of what it did.

        Reads the body first, then falls back to the URL: Gemini names the model in
        the path (``/models/{model}:generateContent``) and rejects it in the body,
        so a body-only reading returns ``""`` for every Gemini attempt and a
        cross-provider chain assertion goes quiet exactly where it matters most.
        """
        return [_target_model(request) for request in self.requests]

    def client(self) -> httpx.AsyncClient:
        return client_from(self)


def _target_model(request: httpx.Request) -> str:
    """The model a recorded request was aimed at, whichever half of it carries one."""
    try:
        body = json.loads(request.content)
    except ValueError:
        body = None
    if isinstance(body, dict):
        model = body.get("model")
        if isinstance(model, str) and model:
            return model

    # `/v1beta/models/gemini-3.6-flash:generateContent` -> `gemini-3.6-flash`.
    segment = request.url.path.rsplit("/", 1)[-1]
    if ":" in segment:
        return segment.split(":", 1)[0]
    return ""


# --------------------------------------------------------------------------- #
# Model specs and gen params (§2.2.6 — shared by the per-adapter payload tests
# and the cross-provider matrix, so the two suites compare apples to apples)
# --------------------------------------------------------------------------- #
def gemini_spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="gemini",
        model="gemini-3.6-flash",
        context_window=1048576,
        max_output_tokens=65536,
        supports_streaming=True,
        supports_vision=True,
        supports_pdf=True,
        supports_system_field=True,
        max_file_bytes=20000000,
        priority=1,
    )


def groq_spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="groq",
        model="openai/gpt-oss-120b",
        context_window=131072,
        max_output_tokens=32768,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=0,
    )


def openrouter_spec() -> ModelSpec:
    return ModelSpec(
        slot="general",
        provider="openrouter",
        model="nvidia/nemotron-3-super-120b-a12b:free",
        context_window=262144,
        max_output_tokens=262144,
        supports_streaming=True,
        supports_vision=False,
        supports_pdf=False,
        supports_system_field=False,
        max_file_bytes=None,
        priority=2,
    )


def general_params() -> GenParams:
    """The knobs every §2.1.5 case 1 payload test renders with."""
    return GenParams(temperature=0.2, max_tokens=512, top_p=0.9, stop=["</done>"])


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


ATTACHMENT_HASH = "c0161dcdfde3e5b1b1898d993d0860533f590a2a6ba12104d795b30b50ade9a0"
"""The sha256 of :func:`attachment_bytes` — the hash a real upload would give
it, written out rather than computed so a fixture that changes underneath the
golden file changes the golden file too."""

ATTACHMENT_NAME = "tile.png"


def attachment_bytes() -> bytes:
    """The 200-byte fixture image every attachment payload test attaches.

    An image rather than a PDF, and 200 bytes rather than a realistic
    document, for one reason: the golden file carries its base64 inline, and a
    golden nobody can read in a diff is a golden everybody re-blesses without
    reading. 96x96 also happens to be one tile at Gemini's published rate,
    which keeps the token arithmetic in the test checkable by hand.
    """
    return (FILES_ROOT / ATTACHMENT_NAME).read_bytes()


def canonical_history_with_attachment() -> list[CanonicalMessage]:
    """:func:`canonical_history` with a file attached to its final question.

    The same fixed six messages, so an attachment golden diffs against the
    text-only one as *one added part* rather than as a whole new conversation.
    The ``file_ref`` block sits after the text, which is the order
    ``api/v1/chat.py`` writes a turn in: what I asked, then what I attached.
    """
    history = canonical_history()
    last = history[-1]
    history[-1] = CanonicalMessage(
        id=last.id,
        conversation_id=last.conversation_id,
        role=last.role,
        content=[
            *last.content,
            file_ref_block(
                file_hash=ATTACHMENT_HASH,
                filename=ATTACHMENT_NAME,
                mime="image/png",
                bytes=len(attachment_bytes()),
            ),
        ],
        meta=last.meta,
        created_at=last.created_at,
        seq=last.seq,
    )
    return history


SCRIPTED_EXTRACTION_TEXT = (
    "Summary: a single solid-color test tile, used across the payload fixtures.\n"
    "Verbatim text: none — this is a raster image with no text layer."
)
"""The fixed extraction the scripted resolver returns for an injected attachment.

Multi-line and short on purpose (D31, phase5.md Step 1): it appears verbatim
inside :func:`app.memory.render.document_envelope` in two committed goldens
(``groq_attachment``, ``openrouter_attachment``), and a golden nobody can read
in a diff is a golden everybody re-blesses without reading.
"""


class ScriptedResolver:
    """A :class:`~app.memory.render.AttachmentResolver` double for §2.2.6's matrix.

    Answers tier 1's question — native or injected? — and nothing else: no
    cache, no database, no Redis, no ``PerceptionResolver`` import. That
    restriction is deliberate (trap 9, phase5.md): a golden that needs Postgres
    and object storage to produce a diff is a golden nobody runs on a red
    build, and ``PerceptionResolver``'s own tier logic already has its own
    integration suite.

    ``mode="native"`` with the real fixture bytes and D27's published tile rate
    when the target spec can read this mime within its file-size limit;
    otherwise ``mode="injected"`` with :data:`SCRIPTED_EXTRACTION_TEXT`. No
    clock, no randomness — a fixed history in, a fixed resolution out.
    """

    async def resolve(self, refs: list[FileRefBlock], spec: ModelSpec) -> list[ResolvedAttachment]:
        resolved: dict[str, ResolvedAttachment] = {}
        for ref in refs:
            if ref["file_hash"] not in resolved:
                resolved[ref["file_hash"]] = self._resolve_one(ref, spec)
        return list(resolved.values())

    @staticmethod
    def _resolve_one(ref: FileRefBlock, spec: ModelSpec) -> ResolvedAttachment:
        within_limit = spec.max_file_bytes is not None and ref["bytes"] <= spec.max_file_bytes
        if spec.supports_mime(ref["mime"]) and within_limit:
            return ResolvedAttachment(
                file_hash=ref["file_hash"],
                filename=ref["filename"],
                mime=ref["mime"],
                size_bytes=ref["bytes"],
                mode="native",
                data=attachment_bytes(),
                tier="native",
                token_cost=258,
            )
        return ResolvedAttachment(
            file_hash=ref["file_hash"],
            filename=ref["filename"],
            mime=ref["mime"],
            size_bytes=ref["bytes"],
            mode="injected",
            text=SCRIPTED_EXTRACTION_TEXT,
            confidence="high",
            tier="llm",
        )


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


def write_golden(name: str, payload: dict[str, Any]) -> None:
    """Bless one golden file. Every ``--bless`` entrypoint writes through this.

    ``newline="\\n"`` is load-bearing, not a style choice: ``tests/fixtures/**``
    is ``-text`` in ``.gitattributes`` (byte-exact, checked out as committed),
    but ``Path.write_text``'s default ``newline=None`` still translates ``\\n``
    to the *host's* line ending on write — ``\\r\\n`` on Windows. Skipping this
    turns "regenerate a golden" into an all-lines-changed diff that is really a
    line-ending flip, on a repo whose own ``.gitattributes`` explains at length
    why CRLF must not reach a file a Linux container or CI runner will read.
    """
    path = GOLDEN_ROOT / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_golden(payload), encoding="utf-8", newline="\n")
    print(f"wrote {path}")
