"""The perception lane — render step 1, for real.

Everything Phases 1 to 3 left as a seam meets here. :class:`PerceptionResolver`
is the first real implementation of ``memory/render.py``'s
``AttachmentResolver`` protocol, and it answers exactly one question, once per
file per candidate: *here is a stored ``file_ref`` and here is the model that
is about to answer — how does this file reach it?*

Four answers, in the order D25 fixes, under the rule that every tier but the
last logs and falls through (trap 12):

======  ==========  ========================================================
Tier    Name        Condition
======  ==========  ========================================================
0       ``cache``   a reading a *model* already produced for these bytes
1       ``native``  ``spec.supports_mime(mime)`` and it fits ``max_file_bytes``
2       ``llm``     a ``perception`` candidate with budget and a closed breaker
3       ``local``   PyMuPDF, and Tesseract if this host has it
======  ==========  ========================================================

**Tier 0 beats tier 1, with one qualification the phase plan implies rather
than states.** D25's reason for putting the cache first is that "the cached
text came out of the same model that would have read the bytes" — true of a
reading tier 2 produced, false of one tier 3 produced. So a stored ``llm`` row
wins outright, and a stored ``local`` row is held back as a *fallback*: the
lane still tries tier 2, and only replays the OCR when tier 2 has nothing to
offer. Without that qualification one reading taken during a Gemini outage
would serve every future turn about that document, and
``repo/extractions.py``'s "upgrade a ``local`` row, never downgrade an ``llm``
one" clause — the one place invariant 6's retroactive improvement is actually
implemented — could never fire.

**Memoized per request, keyed on ``(file_hash, native_wanted)`` (D22, trap 6).**
Render runs once per candidate and up to three times per turn under failover.
Without a memo, a spill from Groq to Gemini re-extracts a document that was
extracted forty milliseconds ago — and the second extraction is the one that
finds the daily budget gone. The key carries the native flag because the same
stored ``file_ref`` genuinely resolves differently for two candidates: Gemini
may take the bytes, Groq never can. Everything else about the decision is a
function of the hash alone.

**Bytes are fetched only when a tier needs them.** Tier 0 needs none, so the
common case — the second turn about a document — touches object storage not at
all. They are never persisted by this module, never logged, and never attached
to a :class:`~app.memory.render.RenderReport`.

**Only the bottom of the chain surfaces.** Any exception a tier raises that is
not :class:`~app.core.errors.FileUnreadable` is caught, logged with the file
hash and the tier that raised it, and the chain moves on. When the chain is
exhausted the turn stops with a 422 naming the file, because answering a
question about a document nobody read is the worst behaviour in the design.

**One case the per-candidate shape cannot fix:** a turn whose first candidate
is text-only, and whose document defeats tiers 0, 2 and 3, raises before a
later natively-capable candidate is reached. Rendering happens per attempt, so
the lane cannot know Gemini is next in the chain. In practice tier 3 returning
nothing means an image on a host with no Tesseract, or a scan OCR could not
read — both worth failing loudly on. Recorded here rather than papered over.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, TypeVar

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache import keys
from app.config import Settings
from app.core.errors import FileUnreadable
from app.core.logging import get_logger
from app.memory.canonical import FileRefBlock
from app.perception import extractors, local
from app.perception.storage import ObjectStore, object_path
from app.providers.registry import ProviderRegistry
from app.providers.types import ExtractionTier, ModelSpec, ResolvedAttachment
from app.quota.tracker import QuotaTracker
from app.routing.circuit_breaker import CircuitBreaker

logger = get_logger("app.perception")

T = TypeVar("T")

TIER_CACHE: Final[ExtractionTier] = "cache"
TIER_NATIVE: Final[ExtractionTier] = "native"
TIER_LLM: Final[ExtractionTier] = "llm"
TIER_LOCAL: Final[ExtractionTier] = "local"


@dataclass(frozen=True, slots=True)
class _MemoKey:
    """``(file_hash, native_wanted)`` — D22's memo identity."""

    file_hash: str
    native_wanted: bool


class PerceptionResolver:
    """One instance per request; the memo is its whole reason for having state.

    Built by ``deps.get_resolver`` and handed to ``routing.route`` /
    ``routing.route_stream``, which pass it into every ``render`` they do. It
    holds a session *factory* rather than a session (D14): the lane runs in the
    middle of a turn that may take a minute, and a session held open across an
    extraction call pins a Postgres connection a free-tier pool cannot spare.

    ``quota=None`` disables the perception lane's reservations exactly as it
    disables the answer lane's (``QUOTA_ENFORCEMENT``, D15); ``redis=None``
    disables tier 0's fast path and tier 2's stampede lock and nothing else.
    """

    def __init__(
        self,
        *,
        store: ObjectStore,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        breaker: CircuitBreaker,
        quota: QuotaTracker | None,
        redis: Redis | None,
        settings: Settings,
        scope: keys.Scope = keys.SYSTEM_SCOPE,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._registry = registry
        self._breaker = breaker
        self._quota = quota
        self._redis = redis
        self._settings = settings
        self._scope = scope
        self._memo: dict[_MemoKey, ResolvedAttachment] = {}
        self._locks: dict[_MemoKey, asyncio.Lock] = {}

    async def resolve(self, refs: list[FileRefBlock], spec: ModelSpec) -> list[ResolvedAttachment]:
        """Render step 1. One :class:`ResolvedAttachment` per distinct hash.

        Deduplicated by hash before anything runs: a conversation that
        references the same document in three turns is one file, and both
        ``fitting`` and ``materialize`` look attachments up by hash anyway.
        """
        resolved: list[ResolvedAttachment] = []
        seen: set[str] = set()
        for ref in refs:
            if ref["file_hash"] in seen:
                continue
            seen.add(ref["file_hash"])
            resolved.append(await self._resolve_one(ref, spec))
        return resolved

    # ---- the memo (D22, trap 6) -------------------------------------------- #
    async def _resolve_one(self, ref: FileRefBlock, spec: ModelSpec) -> ResolvedAttachment:
        key = _MemoKey(file_hash=ref["file_hash"], native_wanted=_native_wanted(ref, spec))

        # A per-key lock rather than a bare dict check: two candidates never
        # render concurrently, but one turn resolving the same hash from two
        # places under `asyncio.gather` would, and "one extraction per hash per
        # request" is the entire point of the memo.
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            memoized = self._memo.get(key)
            if memoized is not None:
                logger.debug(
                    "perception.memo_hit",
                    file_hash=key.file_hash,
                    tier=memoized.tier,
                    mode=memoized.mode,
                )
                return memoized

            attachment = await self._run_chain(ref, spec, native_wanted=key.native_wanted)
            self._memo[key] = attachment
            return attachment

    # ---- the chain (D25) ---------------------------------------------------- #
    async def _run_chain(
        self, ref: FileRefBlock, spec: ModelSpec, *, native_wanted: bool
    ) -> ResolvedAttachment:
        file_hash = ref["file_hash"]
        local_only = self._settings.PERCEPTION_LOCAL_ONLY
        bytes_of = _Lazy(lambda: self._fetch(file_hash))

        # --- tier 0 -----------------------------------------------------------
        # Skipped wholesale under PERCEPTION_LOCAL_ONLY: the flag exists so the
        # phase's most persuasive demo does not require revoking a key (D30),
        # and a stored `llm` reading would answer that demo turn perfectly and
        # disclose nothing degraded, which is the opposite of the demo.
        cached = (
            None
            if local_only
            else await self._guarded(TIER_CACHE, file_hash, lambda: self._cached(file_hash))
        )
        if cached is not None and cached.tier == "llm":
            return _injected(ref, cached, tier=TIER_CACHE)

        # --- tier 1 -----------------------------------------------------------
        if native_wanted:
            native_data = await self._guarded(TIER_NATIVE, file_hash, bytes_of)
            if native_data is not None:
                # No perception reservation (trap 7). These bytes ride in the
                # answering model's own payload and are already counted by the
                # reservation the router makes for that attempt.
                return _native(ref, native_data)

        # --- tier 2 -----------------------------------------------------------
        if not local_only:
            llm_data = await self._guarded(TIER_LLM, file_hash, bytes_of)
            if llm_data is not None:
                extracted = await self._guarded(
                    TIER_LLM, file_hash, lambda: self._extract(ref, llm_data)
                )
                if extracted is not None:
                    return _injected(ref, extracted, tier=TIER_LLM)

        # --- tier 0, the fallback half ----------------------------------------
        # A stored `local` reading, held back above so tier 2 had its chance to
        # upgrade it. Replaying it here costs nothing and re-OCRs nothing.
        if cached is not None:
            return _injected(ref, cached, tier=TIER_CACHE)

        # --- tier 3 -----------------------------------------------------------
        local_data = await self._guarded(TIER_LOCAL, file_hash, bytes_of)
        if local_data is not None:
            reading = await self._guarded(
                TIER_LOCAL, file_hash, lambda: self._local(ref, local_data)
            )
            if reading is not None:
                return _injected(ref, reading, tier=TIER_LOCAL)

        # --- the bottom of the chain (D25) ------------------------------------
        logger.warning(
            "perception.file_unreadable",
            file_hash=file_hash,
            filename=ref["filename"],
            mime=ref["mime"],
            provider=spec.provider,
            model=spec.model,
        )
        raise FileUnreadable(
            f"No tier of the perception lane could read {ref['filename']!r}.",
            details={"file_hash": file_hash, "filename": ref["filename"]},
        )

    async def _guarded(
        self, tier: ExtractionTier, file_hash: str, step: Callable[[], Awaitable[T | None]]
    ) -> T | None:
        """Run one tier. Anything it raises becomes ``None`` and a log line.

        D25's rule and trap 12, in one place rather than four ``try`` blocks:
        every tier but the last falls through, so a bug in the extractor, an
        object store that is down, or a PDF PyMuPDF chokes on all degrade to
        the next tier instead of failing a turn the tier below could still have
        answered. :class:`~app.core.errors.FileUnreadable` is re-raised because
        it *is* the bottom of the chain — catching it here would turn the one
        honest failure into another silent fall-through.
        """
        try:
            return await step()
        except FileUnreadable:
            raise
        except Exception as exc:
            logger.warning(
                "perception.tier_failed",
                tier=tier,
                file_hash=file_hash,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    # ---- the tiers, one method each ----------------------------------------- #
    async def _cached(self, file_hash: str) -> extractors.Extraction | None:
        """Tier 0 — Redis, then ``file_extractions``. Written in Step 6."""
        return await extractors.load_cached(
            file_hash=file_hash,
            session_factory=self._session_factory,
            redis=self._redis,
        )

    async def _fetch(self, file_hash: str) -> bytes:
        """The bytes, from the private bucket, by content-addressed path (D23).

        The only reader of an uploaded object in the whole system. A
        :class:`~app.perception.storage.StorageUnavailable` from here is
        already normalized and carries neither the key nor the bytes; it
        reaches :meth:`_guarded` like any other failure and costs this tier
        rather than the turn.
        """
        return await self._store.get(object_path(file_hash))

    async def _extract(self, ref: FileRefBlock, data: bytes) -> extractors.Extraction | None:
        """Tier 2 — the internal ``perception`` slot, paid for out of D8's half."""
        return await extractors.extract_with_llm(
            file_hash=ref["file_hash"],
            filename=ref["filename"],
            mime=ref["mime"],
            data=data,
            registry=self._registry,
            breaker=self._breaker,
            session_factory=self._session_factory,
            redis=self._redis,
            quota=self._quota,
            scope=self._scope,
        )

    async def _local(self, ref: FileRefBlock, data: bytes) -> extractors.Extraction | None:
        """Tier 3 — PyMuPDF and Tesseract, in a thread, spending no quota.

        Persisted like tier 2's reading, and for the same reason: an outage
        lasting a whole conversation should cost one OCR pass rather than one
        per turn. ``repo/extractions.py``'s upgrade clause is what makes that
        safe — the first healthy tier-2 call replaces the row — and the tier-0
        split at the top of this chain is what makes sure a tier-2 call still
        gets to happen.
        """
        reading = await local.extract_locally(
            file_hash=ref["file_hash"],
            mime=ref["mime"],
            data=data,
            ocr_enabled=self._settings.PERCEPTION_LOCAL_OCR_ENABLED,
            max_ocr_pages=self._settings.PERCEPTION_OCR_MAX_PAGES,
        )
        if reading is None:
            return None
        if not self._settings.PERCEPTION_LOCAL_ONLY:
            # Not under the demo flag: writing a deliberately-degraded reading
            # into a global, content-addressed table would outlive the demo and
            # reach every other user who holds those bytes.
            await extractors.persist(
                reading, session_factory=self._session_factory, redis=self._redis
            )
        return reading


class _Lazy:
    """Await something at most once, then hand back whatever it produced.

    The bytes are wanted by tiers 1, 2 and 3 and by none of them when tier 0
    hits, so the fetch has to be deferred — and a chain falling from tier 2 to
    tier 3 must not download the same object twice. A failed fetch is not
    retried either: it already cost this chain a round trip and a log line.
    """

    def __init__(self, fetch: Callable[[], Awaitable[bytes]]) -> None:
        self._fetch = fetch
        self._done = False
        self._value: bytes | None = None

    async def __call__(self) -> bytes | None:
        if self._done:
            return self._value
        self._done = True
        self._value = await self._fetch()
        return self._value


def _native_wanted(ref: FileRefBlock, spec: ModelSpec) -> bool:
    """Tier 1's two questions (D25), asked before any I/O.

    Both are pure reads off the spec and the stored block, which is what lets
    the answer be part of the memo key rather than something discovered
    halfway down the chain.
    """
    if not spec.supports_mime(ref["mime"]):
        return False
    return spec.max_file_bytes is None or ref["bytes"] <= spec.max_file_bytes


def _native(ref: FileRefBlock, data: bytes) -> ResolvedAttachment:
    return ResolvedAttachment(
        file_hash=ref["file_hash"],
        filename=ref["filename"],
        mime=ref["mime"],
        size_bytes=ref["bytes"],
        mode="native",
        data=data,
        tier=TIER_NATIVE,
    )


def _injected(
    ref: FileRefBlock, extraction: extractors.Extraction, *, tier: ExtractionTier
) -> ResolvedAttachment:
    """A reading, wrapped for the prompt.

    ``text`` reaches a model through ``document_envelope`` at materialize time
    and nowhere else — this module never formats a prompt, because every
    provider must wrap extracted text identically and ``memory/render.py`` owns
    that envelope (trap 11).
    """
    return ResolvedAttachment(
        file_hash=ref["file_hash"],
        filename=ref["filename"],
        mime=ref["mime"],
        size_bytes=ref["bytes"],
        mode="injected",
        text=extraction.text,
        confidence=extraction.confidence,
        tier=tier,
    )


__all__ = [
    "TIER_CACHE",
    "TIER_LLM",
    "TIER_LOCAL",
    "TIER_NATIVE",
    "PerceptionResolver",
]
