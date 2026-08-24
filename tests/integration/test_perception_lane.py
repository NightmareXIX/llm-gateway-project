"""The perception lane, end to end — Phase 4 Step 8.

This module drives the path the whole phase exists for: a file is uploaded
through the real endpoint, a turn references it, and the answer comes back
disclosing *how* the document reached the model. Every test asserts three
things at once, because a wrong tier is invisible in the answer and expensive
in the budget:

* **which tier was chosen** — ``extraction_tier`` on the response;
* **whether it was honest about it** — ``degraded``;
* **what it cost** — how many requests actually left the process, read off the
  transport rather than off anything the code says about itself.

**Two providers, one transport.** :class:`_Fleet` dispatches on the request's
host: Groq gets an answer fixture, Gemini gets whichever extraction fixture the
test scripted. That is what lets one test express a turn where the *answering*
model cannot read a PDF and the *perception* lane can — the feature itself, and
something a flat one-response handler cannot say.

**Nothing here reaches a network, a disk, or a Tesseract binary.** The object
store is a :class:`MemoryStore`, the fixture PDFs are the ones Step 7 committed,
and a test that needs the local tier to fail asks for that by turning OCR off
rather than by relying on this machine not having the binary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Annotated, Any

import httpx
import pymupdf
import pytesseract
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.chat import _get_resolver as chat_get_resolver
from app.cache import keys
from app.config import ProvidersConfig, Settings, get_providers_config, get_settings
from app.core.crypto import encrypt_provider_key
from app.db.models import FileExtraction, Message
from app.db.repo import provider_keys as provider_keys_repo
from app.deps import (
    get_breaker,
    get_quota,
    get_redis,
    get_registry,
    get_session_factory,
    get_store,
)
from app.keys_resolution.resolver import SystemCredentials
from app.memory.render import AttachmentResolver
from app.perception import local as local_tier
from app.perception.lane import TOKENS_PER_TILE, PerceptionResolver
from app.perception.storage import MemoryStore
from app.providers.registry import ProviderRegistry, build_registry
from app.quota.tracker import QuotaTracker
from app.routing.circuit_breaker import CircuitBreaker
from tests import provider_fixtures as fx
from tests.conftest import TokenFactory

pytestmark = pytest.mark.integration

COMPLETIONS = "/v1/chat/completions"
FILES = "/v1/files"

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "files"
TEXT_LAYER_PDF = (FIXTURE_DIR / "text_layer.pdf").read_bytes()
SCANNED_PDF = (FIXTURE_DIR / "scanned.pdf").read_bytes()
CHART_PNG = (FIXTURE_DIR / "chart.png").read_bytes()

EXTRACTION_MARKER = "document reader for another model"
"""A phrase from ``EXTRACTION_PROMPT``, used to tell an extraction request from
an answer request on the wire. Counting requests that carry it is a stronger
statement than counting requests to a host, because both lanes reach Gemini."""


def _pdf_of(pages: int) -> bytes:
    """A synthetic PDF of a given length, for the cost arithmetic (D27).

    Built here rather than committed: the only thing the assertion cares about
    is the page count, and a forty-page fixture in the repo would be forty
    pages of nothing.
    """
    doc = pymupdf.open()
    for index in range(pages):
        doc.new_page().insert_text((72, 72), f"page {index + 1}", fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------------- #
# The fleet
# --------------------------------------------------------------------------- #
class _Fleet:
    """One transport for both lanes, dispatching on the provider's host.

    The answer lane and the perception lane can hit different providers within
    one request, so a handler serving a flat script cannot distinguish "Groq
    answered and Gemini extracted" from "two requests happened". This keeps a
    script and a request log per provider.

    A fixture name ending in ``.sse`` is served as a streamed body, so the same
    fleet covers ``stream: true``.
    """

    def __init__(
        self,
        *,
        groq: list[str] | None = None,
        gemini: list[str] | None = None,
        openrouter: list[str] | None = None,
    ) -> None:
        self._script: dict[str, list[str]] = {
            "groq": list(groq or []),
            "gemini": list(gemini or []),
            "openrouter": list(openrouter or []),
        }
        self.requests: dict[str, list[httpx.Request]] = {
            "groq": [],
            "gemini": [],
            "openrouter": [],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        provider = _provider_of(request.url.host or "")
        index = len(self.requests[provider])
        self.requests[provider].append(request)
        script = self._script[provider]
        if index >= len(script):
            raise AssertionError(
                f"unscripted {provider} request #{index + 1} to {request.url}; "
                f"the script holds {len(script)}"
            )
        name = script[index]
        if name.endswith(".sse"):
            body = fx.read_sse(provider, name.removesuffix(".sse")).encode("utf-8")
            return httpx.Response(200, stream=fx.ScriptedByteStream([body]))
        return fx.load(provider, name).to_response()

    def calls(self, provider: str) -> int:
        return len(self.requests[provider])

    def extraction_calls(self) -> int:
        """Requests carrying ``EXTRACTION_PROMPT``, whoever they went to."""
        return sum(
            1
            for requests in self.requests.values()
            for request in requests
            if EXTRACTION_MARKER in request.content.decode("utf-8", "replace")
        )

    def client(self) -> httpx.AsyncClient:
        return fx.client_from(self)


def _provider_of(host: str) -> str:
    """Which provider a request was aimed at, by host.

    Cheap and exact: the three base URLs in ``config/providers.yaml`` share no
    domain, and the alternative — matching on path shape — would have to know
    that Gemini names the model in the path and the other two do not.
    """
    if "groq.com" in host:
        return "groq"
    if "openrouter.ai" in host:
        return "openrouter"
    return "gemini"


def _fleet_config(
    *, slot: str, general: tuple[str, ...], general_max_file_bytes: int | None = None
) -> ProvidersConfig:
    """The committed table with one answering slot narrowed to named providers.

    ``perception`` is left exactly as ``config/providers.yaml`` declares it. The
    point of most of these tests is that the answering slot and the extraction
    slot are two independent decisions, and rewriting the internal slot here
    would make that true by construction rather than by test.
    """
    config = get_providers_config()
    declared = config.slots[slot]
    candidates = tuple(c for c in declared.candidates if c.provider in general)
    if general_max_file_bytes is not None:
        candidates = tuple(
            c.model_copy(update={"max_file_bytes": general_max_file_bytes}) for c in candidates
        )
    narrowed = declared.model_copy(update={"candidates": candidates})
    return config.model_copy(update={"slots": {**config.slots, slot: narrowed}})


@pytest.fixture
def store(app: FastAPI) -> MemoryStore:
    """The lifespan's object store, as a dict."""
    memory = MemoryStore()
    app.state.object_store = memory
    return memory


@pytest.fixture
async def fleet(app: FastAPI) -> AsyncIterator[Callable[..., _Fleet]]:
    """Install a :class:`_Fleet` and narrow ``general`` to the named providers."""
    installed: list[httpx.AsyncClient] = []

    def install(
        *,
        slot: str = "general",
        general: tuple[str, ...] = ("groq",),
        groq: list[str] | None = None,
        gemini: list[str] | None = None,
        openrouter: list[str] | None = None,
        general_max_file_bytes: int | None = None,
    ) -> _Fleet:
        handler = _Fleet(groq=groq, gemini=gemini, openrouter=openrouter)
        client = handler.client()
        installed.append(client)
        app.state.provider_registry = build_registry(
            client=client,
            config=_fleet_config(
                slot=slot,
                general=general,
                general_max_file_bytes=general_max_file_bytes,
            ),
        )
        return handler

    try:
        yield install
    finally:
        for client in installed:
            await client.aclose()


@pytest.fixture
def perception(app: FastAPI) -> Callable[..., None]:
    """Rebuild the request's resolver with one or more settings overridden.

    Overriding the dependency rather than the environment: the ``PERCEPTION_*``
    flags are read through an ``lru_cache``d ``get_settings()``, so a test that
    cleared that cache would be changing configuration for every test that runs
    after it in the same session.
    """

    def configure(**overrides: Any) -> None:
        app.dependency_overrides[chat_get_resolver] = _resolver_override(
            get_settings().model_copy(update=overrides)
        )

    return configure


def _resolver_override(
    settings: Settings, sink: list[PerceptionResolver] | None = None
) -> Callable[..., AttachmentResolver | None]:
    """``chat.py``'s ``_get_resolver`` with a substituted ``Settings``.

    The same six sub-dependencies as the real one — in particular
    ``get_session_factory``, which ``conftest``'s ``app`` fixture overrides to
    hand back the test's transactional session — so only the settings differ.
    ``credentials`` is not one of them: these tests are about the perception
    lane's tiers, not BYOK (that is ``test_key_resolver.py``'s and
    ``test_router.py``'s subject), so a plain :class:`SystemCredentials`
    stands in for the real per-request resolver Phase 6 Step 6 threads here.

    ``sink`` collects the resolvers actually built, which is the only way to
    read a per-request object after the request that owned it has returned —
    and the resolver's memo is where D27's ``token_cost`` can be observed
    without asserting on a reservation that has already been reconciled away.
    """

    def _get_resolver(
        store: Annotated[MemoryStore, Depends(get_store)],
        session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
        registry: Annotated[ProviderRegistry, Depends(get_registry)],
        breaker: Annotated[CircuitBreaker, Depends(get_breaker)],
        quota: Annotated[QuotaTracker | None, Depends(get_quota)],
        redis: Annotated[Any, Depends(get_redis)],
    ) -> AttachmentResolver | None:
        if not settings.PERCEPTION_ENABLED:
            return None
        resolver = PerceptionResolver(
            store=store,
            session_factory=session_factory,
            registry=registry,
            breaker=breaker,
            quota=quota,
            redis=redis,
            settings=settings,
            credentials=SystemCredentials(registry),
        )
        if sink is not None:
            sink.append(resolver)
        return resolver

    return _get_resolver


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    content: bytes = TEXT_LAYER_PDF,
    filename: str = "report.pdf",
    content_type: str = "application/pdf",
) -> str:
    response = await client.post(
        FILES, files={"file": (filename, content, content_type)}, headers=headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["file_hash"])


async def _ask(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    file_hash: str | None = None,
    conversation_id: str | None = None,
    stream: bool = False,
    slot: str = "general",
    question: str = "what does this document say?",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "user", "content": question}
    if file_hash is not None:
        message["file_refs"] = [file_hash]
    body: dict[str, Any] = {
        "model": slot,
        "messages": [message],
        "temperature": 0.0,
        "stream": stream,
    }
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return await client.post(COMPLETIONS, json=body, headers=headers)


# --------------------------------------------------------------------------- #
# Tier 2 — a text-only model, answered from an extraction
# --------------------------------------------------------------------------- #
async def test_a_text_only_model_is_answered_from_a_gemini_extraction(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    db_session: AsyncSession,
) -> None:
    """§1's definition of done, first half: ask about a PDF on a model that
    cannot open one, and the answer arrives anyway — because a model that can
    read it was asked to describe it first.

    ``degraded`` is ``False``: a real model read the document. That distinction
    is the whole reason ``grade`` never returns ``low`` (trap 13).
    """
    handler = fleet(general=("groq",), groq=["success"], gemini=["extraction_complete"])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extraction_tier"] == "llm"
    assert body["degraded"] is False
    assert body["served_by"]["provider"] == "groq"
    assert handler.extraction_calls() == 1
    assert handler.calls("groq") == 1

    row = (await db_session.execute(select(FileExtraction))).scalar_one()
    assert row.file_hash == file_hash
    assert row.tier == "llm"
    assert row.extracted_by_provider == "gemini"


async def test_the_extraction_is_stored_on_the_assistant_message(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    db_session: AsyncSession,
) -> None:
    """Trap 13's chain, persisted rather than only returned: re-opening the
    thread tomorrow still says how the document reached the model."""
    fleet(general=("groq",), groq=["success"], gemini=["extraction_complete"])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    await _ask(client, headers, file_hash=file_hash)

    result = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assistant = result.scalar_one()
    assert assistant.meta["extraction_tier"] == "llm"
    assert assistant.meta["degraded"] is False


async def test_a_second_turn_reads_the_cache_and_calls_nothing_for_it(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """Tier 0. The second question about a document costs one answer call and
    nothing else — no extraction, no object read, no quota."""
    handler = fleet(general=("groq",), groq=["success", "success"], gemini=["extraction_complete"])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    first = await _ask(client, headers, file_hash=file_hash)
    assert first.json()["extraction_tier"] == "llm"

    second = await _ask(
        client, headers, file_hash=file_hash, conversation_id=first.json()["conversation_id"]
    )

    assert second.status_code == 200, second.text
    assert second.json()["extraction_tier"] == "cache"
    assert handler.extraction_calls() == 1


# --------------------------------------------------------------------------- #
# Tier 1 — the model reads it itself
# --------------------------------------------------------------------------- #
async def test_a_model_that_reads_pdfs_is_handed_the_bytes(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """§1's definition of done, second half: on Gemini nothing is extracted at
    all, because Gemini can open the file itself. One request leaves the
    process, and it is the answer."""
    handler = fleet(general=("gemini",), gemini=["success"])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    assert response.json()["extraction_tier"] == "native"
    assert response.json()["degraded"] is False
    assert handler.extraction_calls() == 0
    assert handler.calls("gemini") == 1

    # The bytes really rode in the payload: Step 6's `_render_parts` put an
    # `inline_data` part beside the message's text part, which is the only
    # thing that makes "native" mean anything.
    import json

    sent = json.loads(handler.requests["gemini"][0].content)
    parts = sent["contents"][-1]["parts"]
    assert any("inline_data" in part for part in parts)
    assert any("text" in part for part in parts)


async def test_a_native_forty_page_pdf_carries_a_five_figure_token_cost(
    app: FastAPI,
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """D27, end to end through real storage (Step 9).

    Forty pages of base64 are invisible to ``estimate_tokens`` (trap 9) and
    thirty characters to ``materialize``'s placeholder, so before this step a
    document billed in five figures measured as two. The resolved attachment is
    where the honest number is attached, and it is read off the lane's own memo
    rather than off a counter — the reservation this number feeds has already
    been reconciled against reported usage by the time the response arrives.
    """
    fleet(general=("gemini",), gemini=["success"])
    resolvers: list[PerceptionResolver] = []
    app.dependency_overrides[chat_get_resolver] = _resolver_override(get_settings(), resolvers)
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers, content=_pdf_of(40))

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    assert response.json()["extraction_tier"] == "native"

    resolved = list(resolvers[0]._memo.values())
    assert [attachment.mode for attachment in resolved] == ["native"]
    assert resolved[0].file_hash == file_hash
    assert resolved[0].pages == 40
    assert resolved[0].token_cost == 40 * TOKENS_PER_TILE
    assert resolved[0].token_cost >= 10_000


async def test_a_file_over_the_models_cap_never_reaches_tier_one(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """``max_file_bytes`` is tier 1's second question (D25). A file over it
    falls through to an extraction rather than being sent as bytes the provider
    would reject — and the extraction chain has its own, larger cap to check.

    Forced by shrinking the *answering* candidate's cap rather than by
    uploading twenty megabytes: the decision under test is the comparison, and
    ``FILE_MAX_BYTES`` refuses a genuinely oversized upload long before the lane
    ever sees it.
    """
    handler = fleet(
        general=("gemini",),
        gemini=["extraction_complete", "success"],
        general_max_file_bytes=16,
    )
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    assert response.json()["extraction_tier"] == "llm"
    assert handler.extraction_calls() == 1


# --------------------------------------------------------------------------- #
# Phase 6 Step 6 — tier 2 resolves credentials per candidate too
# --------------------------------------------------------------------------- #
async def _add_private_key(
    session: AsyncSession, *, user_id: Any, provider: str, plaintext: str
) -> None:
    await provider_keys_repo.upsert(
        session,
        user_id=user_id,
        provider=provider,
        encrypted_key=encrypt_provider_key(plaintext),
        last_4=plaintext[-4:],
        nickname=None,
        validation_status="valid",
        last_validated_at=None,
    )
    await session.flush()


async def test_an_extraction_under_a_private_key_spends_the_users_own_perception_budget(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    user_factory: Callable[..., Any],
    db_session: AsyncSession,
    redis_client: FakeRedis,
) -> None:
    """The perception lane's own candidate walk resolves credentials
    independently of the answering model's (D36). Groq answers off the shared
    pool; Gemini reads the document under this user's own key, on this user's
    own budget — proving the router asking ``credentials.for_provider`` and
    the lane asking it are the same fact reached twice, not once.
    """
    user = await user_factory()
    await _add_private_key(
        db_session, user_id=user.id, provider="gemini", plaintext="private-extraction-key-abcd"
    )

    handler = fleet(general=("groq",), groq=["success"], gemini=["extraction_complete"])
    headers = _headers(make_jwt, sub=user.id)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    assert response.json()["extraction_tier"] == "llm"
    # The extraction itself carried the user's own key, not the shared pool's.
    assert handler.requests["gemini"][0].headers["x-goog-api-key"] == "private-extraction-key-abcd"

    private_lane = keys.quota_perception_lane(str(user.id), "gemini", "gemini-3.6-flash")
    shared_lane = keys.quota_perception_lane(keys.SYSTEM_SCOPE, "gemini", "gemini-3.6-flash")
    assert await redis_client.get(private_lane) == "1"
    assert await redis_client.get(shared_lane) is None


async def test_a_shared_pool_extraction_still_answers_a_private_key_users_next_question(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    user_factory: Callable[..., Any],
    db_session: AsyncSession,
    redis_client: FakeRedis,
) -> None:
    """``file_extractions`` and ``extract:{hash}`` stay unscoped by design
    (D22/D24): the text is a fact about the document, not about who paid to
    learn it. A reading the shared pool paid for is served to this user's next
    question exactly as it would be to anyone else's, even after they have
    since added their own key — no re-extraction, private or shared.
    """
    user = await user_factory()
    headers = _headers(make_jwt, sub=user.id)
    handler = fleet(general=("groq",), groq=["success", "success"], gemini=["extraction_complete"])
    file_hash = await _upload(client, headers)

    first = await _ask(client, headers, file_hash=file_hash)
    assert first.json()["extraction_tier"] == "llm"

    # Only now does this user acquire a private Gemini key — after the shared
    # pool has already paid for the one reading that exists.
    await _add_private_key(
        db_session, user_id=user.id, provider="gemini", plaintext="added-after-the-fact-wxyz"
    )

    # A different question, deliberately: D29's exact-match cache would
    # otherwise answer the identical question before the lane ever ran, which
    # would prove nothing about tier 0.
    second = await _ask(
        client,
        headers,
        file_hash=file_hash,
        conversation_id=first.json()["conversation_id"],
        question="summarize this document",
    )

    assert second.status_code == 200, second.text
    assert second.json()["extraction_tier"] == "cache"
    assert handler.extraction_calls() == 1

    # A cache hit costs no quota at all, under either scope — the private key
    # this user now holds was never asked to pay for a reading that already
    # existed.
    private_lane = keys.quota_perception_lane(str(user.id), "gemini", "gemini-3.6-flash")
    assert await redis_client.get(private_lane) is None


# --------------------------------------------------------------------------- #
# D25's ordering: tier 0 beats tier 1
# --------------------------------------------------------------------------- #
async def test_a_stored_model_reading_beats_a_native_passthrough(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """D25, stated outright: "when both are available, the free one wins".

    A cached extraction costs nothing; handing Gemini the bytes costs tokens in
    a 1M-token window. So the second turn — this time on Gemini, which *could*
    read the file itself — is served from the row Groq's turn wrote. The exit
    checklist in ``phase4.md`` sketches this step as ``native``; D25 decides it
    explicitly, with reasoning, and D25 is what this asserts.
    """
    handler = fleet(general=("groq",), groq=["success"], gemini=["extraction_complete"])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)
    assert (await _ask(client, headers, file_hash=file_hash)).json()["extraction_tier"] == "llm"

    gemini_fleet = fleet(general=("gemini",), gemini=["success"])
    # A *different* question about the same document, deliberately. Since D29 an
    # attachment turn is cacheable, so re-asking the identical question in a
    # fresh conversation would be answered by `cache/exact.py` before the lane
    # ran at all — a real behaviour, tested on its own below, and the wrong one
    # to assert a tier through.
    response = await _ask(client, headers, file_hash=file_hash, question="summarize this document")

    assert response.json()["extraction_tier"] == "cache"
    assert gemini_fleet.extraction_calls() == 0
    assert handler.extraction_calls() == 1


# --------------------------------------------------------------------------- #
# The memo (D22, trap 6)
# --------------------------------------------------------------------------- #
async def test_a_failover_inside_one_turn_extracts_exactly_once(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """Trap 6, which is the expensive one. Render runs once per candidate, so a
    spill from Groq to Gemini renders twice — and without the memo and tier 0
    the second render would extract the same document a second time, out of the
    scarcest budget in the fleet, forty milliseconds after the first.

    Gemini serves both roles here: the extraction during Groq's render, then
    the answer after Groq 429s.
    """
    handler = fleet(
        general=("groq", "gemini"),
        groq=["rate_limited"],
        gemini=["extraction_complete", "success"],
    )
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    assert response.json()["served_by"]["provider"] == "gemini"
    assert response.json()["attempts"] == 2
    assert handler.extraction_calls() == 1
    # Gemini's second render found the row the first one wrote, so the winning
    # candidate answered off tier 0 rather than re-reading the bytes natively.
    assert response.json()["extraction_tier"] == "cache"


async def test_a_mid_stream_restart_re_renders_but_does_not_re_extract(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    db_session: AsyncSession,
) -> None:
    """Trap 6, on the path the non-streaming version above cannot exercise.

    D1's restart is a genuinely different failure than a pre-stream 429: Groq
    gets far enough to emit real deltas before the connection dies mid-JSON, so
    the router has already committed to a stream by the time it restarts onto
    Gemini. Render still runs a second time for the replacement candidate — the
    ``meta`` frame does not move, but the resolver does — and the memo plus
    tier 0 are what stop that second render from spending a second extraction.
    """
    handler = fleet(
        general=("groq", "gemini"),
        groq=["stream_truncated.sse"],
        gemini=["extraction_complete", "stream_success.sse"],
    )
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash, stream=True)

    assert response.status_code == 200, response.text
    frames = response.text
    assert "event: restart" in frames
    done = _last_done(frames)
    assert done["attempts"] == 2
    assert handler.extraction_calls() == 1
    # Gemini's restart render found the row Groq's first render wrote, so it
    # answered off tier 0 rather than re-reading the bytes natively.
    assert done["extraction_tier"] == "cache"

    result = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assert result.scalar_one().meta["extraction_tier"] == "cache"


async def test_the_memo_is_what_stops_a_second_reading_when_nothing_is_stored(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    perception: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memo on its own, with tier 0 taken away.

    The failover test above proves the document is read once, but it does not
    prove the *memo* is why: tier 0 would have caught the second render anyway,
    because tier 2 wrote a row. Under ``PERCEPTION_LOCAL_ONLY`` there is no row
    and no tier 0 at all, so two text-only candidates in one turn means two
    renders and — without the memo — two OCR passes over the same bytes.

    Both candidates are text-only on purpose: the memo key carries the native
    flag (D22), so a spill onto a model that *could* read the file is genuinely
    a different question and genuinely re-resolves.
    """
    calls: list[str] = []
    real = local_tier.extract_locally

    async def counting(**kwargs: Any) -> Any:
        calls.append(str(kwargs["file_hash"]))
        return await real(**kwargs)

    monkeypatch.setattr(local_tier, "extract_locally", counting)
    perception(PERCEPTION_LOCAL_ONLY=True)
    # `fast`, not `general`: `general`'s OpenRouter candidate declares a
    # max_output equal to its whole window, so the fitting step refuses it
    # before an attempt — a real config quirk, and not this test's subject.
    fleet(
        slot="fast",
        general=("groq", "openrouter"),
        groq=["rate_limited"],
        openrouter=["success"],
        gemini=[],
    )
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash, slot="fast")

    assert response.status_code == 200, response.text
    assert response.json()["attempts"] == 2
    assert response.json()["served_by"]["provider"] == "openrouter"
    assert response.json()["extraction_tier"] == "local"
    assert calls == [file_hash]


# --------------------------------------------------------------------------- #
# Tier 3, and the bottom of the chain
# --------------------------------------------------------------------------- #
async def test_local_only_answers_from_the_text_layer_without_touching_gemini(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    perception: Callable[..., None],
) -> None:
    """``PERCEPTION_LOCAL_ONLY`` (D30): the demo that does not require revoking
    a key. Tier 0 and tier 2 are both skipped — a stored model reading would
    answer perfectly and disclose nothing, which is the opposite of the point —
    and the answer still arrives, out of PyMuPDF alone."""
    perception(PERCEPTION_LOCAL_ONLY=True)
    handler = fleet(general=("groq",), groq=["success"], gemini=[])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    assert response.json()["extraction_tier"] == "local"
    assert handler.calls("gemini") == 0


async def test_an_ocr_reading_arrives_and_is_marked_degraded(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    perception: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trap 13's whole chain, on the path that makes it true: ``confidence="low"``
    → ``RenderReport.degraded`` → the response's ``degraded`` and
    ``extraction_tier: "local"``.

    Tesseract is mocked at the ``image_to_string`` seam — the same seam
    ``tests/unit/test_local_extraction.py`` uses — because this asserts on what
    the *gateway* does with an OCR reading, not on what Tesseract produces, and
    the binary is legitimately absent on a developer's machine (D30).
    """
    monkeypatch.setattr(local_tier, "ocr_available", lambda: True)
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda _image: "Quarterly revenue was flat."
    )
    perception(PERCEPTION_LOCAL_ONLY=True)
    fleet(general=("groq",), groq=["success"], gemini=[])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers, content=SCANNED_PDF, filename="scan.pdf")

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 200, response.text
    assert response.json()["extraction_tier"] == "local"
    assert response.json()["degraded"] is True


async def test_a_document_no_tier_can_read_stops_the_turn(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    perception: Callable[..., None],
    db_session: AsyncSession,
) -> None:
    """D25's one surfacing failure. Tier 2 refuses (a ``ContentFiltered`` stops
    the chain rather than laundering a refusal), tier 3 has no OCR to fall back
    on, and answering a question about a document nobody read is the worst
    thing available — so the turn is a 422 naming the file, and no assistant
    message is written.
    """
    perception(PERCEPTION_LOCAL_OCR_ENABLED=False)
    handler = fleet(general=("groq",), groq=[], gemini=["blocked_prompt"])
    headers = _headers(make_jwt)
    file_hash = await _upload(
        client, headers, content=CHART_PNG, filename="chart.png", content_type="image/png"
    )

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "file_unreadable"
    assert error["details"]["file_hash"] == file_hash
    # The chain really was walked: tier 2 was tried, tier 3 had nothing, and no
    # answer request was ever made.
    assert handler.extraction_calls() == 1
    assert handler.calls("groq") == 0

    assistants = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assert assistants.scalars().all() == []


async def test_the_streaming_path_surfaces_an_unreadable_file_as_a_422(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    perception: Callable[..., None],
) -> None:
    """D13 and D25 meeting, on the path where getting it wrong is invisible.

    The lane runs inside the first render, which happens while the endpoint is
    still priming the generator by hand — before a single byte has been written
    and therefore while the status line is still ours. So the same 422 the
    non-streaming path gives arrives as an ordinary JSON envelope rather than
    as a 200 with a failure buried inside it.
    """
    perception(PERCEPTION_LOCAL_ONLY=True, PERCEPTION_LOCAL_OCR_ENABLED=False)
    handler = fleet(general=("groq",), groq=[], gemini=[])
    headers = _headers(make_jwt)
    file_hash = await _upload(
        client, headers, content=CHART_PNG, filename="chart.png", content_type="image/png"
    )

    response = await _ask(client, headers, file_hash=file_hash, stream=True)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "file_unreadable"
    assert handler.calls("groq") == 0


# --------------------------------------------------------------------------- #
# D29 — the exact-match cache over an attachment turn (Step 10)
# --------------------------------------------------------------------------- #
async def test_an_identical_question_over_the_same_file_is_a_cache_hit(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """D29 reversed Phase 3's blanket exclusion, and this is what it bought.

    Three things have to be true at once, because a cache that hits while still
    calling a provider is a cache that costs more than it saves: the header says
    ``HIT``, no answer request left the process, and — the half specific to this
    phase — **no lane call either**. The response cache sits in front of render,
    so a hit skips the extraction chain as completely as it skips the router.
    """
    handler = fleet(general=("groq",), groq=["success"], gemini=["extraction_complete"])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    first = await _ask(client, headers, file_hash=file_hash)
    assert first.headers["X-Cache"] == "MISS"
    assert first.json()["extraction_tier"] == "llm"

    second = await _ask(client, headers, file_hash=file_hash)

    assert second.status_code == 200, second.text
    assert second.headers["X-Cache"] == "HIT"
    assert (
        second.json()["choices"][0]["message"]["content"]
        == (first.json()["choices"][0]["message"]["content"])
    )
    # One answer call and one extraction call in total — both from the first turn.
    assert handler.calls("groq") == 1
    assert handler.extraction_calls() == 1
    # A hit never ran the lane, so there is no tier to disclose. The tier that
    # produced the text is on the *first* answer's own row.
    assert second.json()["extraction_tier"] is None


async def test_the_same_question_over_different_bytes_misses(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
) -> None:
    """The hash is the identity of the content (D29). Two documents, one
    question, two keys — otherwise the cache would answer about the wrong
    document, which is the failure mode the blanket exclusion was protecting
    against in the first place."""
    handler = fleet(
        general=("groq",),
        groq=["success", "success"],
        gemini=["extraction_complete", "extraction_complete"],
    )
    headers = _headers(make_jwt)
    first_hash = await _upload(client, headers)
    second_hash = await _upload(client, headers, content=_pdf_of(2), filename="other.pdf")
    assert first_hash != second_hash

    assert (await _ask(client, headers, file_hash=first_hash)).headers["X-Cache"] == "MISS"
    second = await _ask(client, headers, file_hash=second_hash)

    assert second.headers["X-Cache"] == "MISS"
    assert handler.calls("groq") == 2


async def test_a_degraded_answer_is_never_written_to_the_cache(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    perception: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trap 14, which is the whole safety argument D29 rests on.

    An answer built on an OCR reading nobody trusts must not be replayed for an
    hour. The read side cannot know a tier — it runs before anything is
    resolved — so the guarantee lives entirely in the write side's ``degraded``
    gate, and the observable consequence is that the second identical request
    is a ``MISS`` that calls a provider again.
    """
    monkeypatch.setattr(local_tier, "ocr_available", lambda: True)
    monkeypatch.setattr(
        pytesseract, "image_to_string", lambda _image: "Quarterly revenue was flat."
    )
    perception(PERCEPTION_LOCAL_ONLY=True)
    handler = fleet(general=("groq",), groq=["success", "success"], gemini=[])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers, content=SCANNED_PDF, filename="scan.pdf")

    first = await _ask(client, headers, file_hash=file_hash)
    assert first.json()["degraded"] is True
    # `MISS`, not `BYPASS`: the request *was* eligible on the way in, and only
    # the answer turned out not to be. That distinction is the one `X-Cache`'s
    # third value exists for.
    assert first.headers["X-Cache"] == "MISS"

    second = await _ask(client, headers, file_hash=file_hash)

    assert second.headers["X-Cache"] == "MISS"
    assert handler.calls("groq") == 2


# --------------------------------------------------------------------------- #
# The kill switch
# --------------------------------------------------------------------------- #
async def test_the_lane_switched_off_fails_loudly_rather_than_silently(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    perception: Callable[..., None],
) -> None:
    """``PERCEPTION_ENABLED=False`` puts ``NoAttachments`` back in step 1, and
    ``NoAttachments`` raises rather than resolving a ``file_ref`` to nothing.

    A generic 500 is the *correct* outcome: silently dropping the reference
    would answer a question about a document the model never saw, and the user
    would have no way to tell that from a bad answer.
    """
    perception(PERCEPTION_ENABLED=False)
    handler = fleet(general=("groq",), groq=[], gemini=[])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert handler.calls("groq") == 0


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #
async def test_a_streamed_turn_discloses_the_tier_on_done(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    fleet: Callable[..., _Fleet],
    db_session: AsyncSession,
) -> None:
    """The disclosure has to hold on both paths (trap 13), and the streaming one
    carries it in a different field of a different shape — ``DoneEvent`` rather
    than the response body — so it gets its own assertion rather than an
    argument from symmetry."""
    fleet(general=("groq",), groq=["stream_success.sse"], gemini=["extraction_complete"])
    headers = _headers(make_jwt)
    file_hash = await _upload(client, headers)

    response = await _ask(client, headers, file_hash=file_hash, stream=True)

    assert response.status_code == 200, response.text
    frames = response.text
    assert "event: done" in frames
    done = _last_done(frames)
    assert done["extraction_tier"] == "llm"
    assert done["degraded"] is False

    result = await db_session.execute(select(Message).where(Message.role == "assistant"))
    assert result.scalar_one().meta["extraction_tier"] == "llm"


def _last_done(frames: str) -> dict[str, Any]:
    """The payload of the stream's ``done`` event."""
    import json

    for block in frames.split("\n\n"):
        if block.startswith("event: done"):
            return dict(json.loads(block.split("data: ", 1)[1]))
    raise AssertionError(f"no done event in:\n{frames}")
