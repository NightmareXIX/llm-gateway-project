"""Reads and writes against ``file_extractions`` — what a document says.

The one table in the schema with **no ``user_id`` column and no ownership
scoping**, which is a deliberate exception to the rule every other repository
here follows rather than an oversight. D24 splits the two questions: the right
to *reference* a hash is per user and lives in ``files``; the extracted text of
a byte sequence is a property of those bytes, so re-extracting it per user
would spend the scarcest budget in the fleet to compute the same string twice.

Nothing reaches this table with a raw hash from a client. A ``file_ref``
passes ``files``'s ownership gate at the moment it enters a message
(``api/v1/chat.py::_resolve_file_refs``), and only stored history reaches the
perception lane — so by the time anything here runs, the caller's right to see
this text has already been established one layer up. Adding a ``user_id``
filter here would not add a check; it would add a *second copy* of the
extraction per user, which is exactly what D24 decided against.

Invariant 6's payoff — "improving your extractor retroactively improves old
conversations" — is why :func:`upsert` upgrades rather than skips: a row
written by tier 3 in the middle of an outage is replaced the first time tier 2
is healthy enough to do better. It never degrades in the other direction.

Repositories take a session and never commit. The caller owns the transaction
boundary.
"""

from __future__ import annotations

from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FileExtraction

ExtractionTier = Literal["llm", "local"]
"""The two tiers that *produce* a row. ``cache`` reads this table and ``native``
never touches it — the ``tier_known`` CHECK constraint says the same thing at
the database."""

_UPGRADABLE_FROM: Final = "local"
"""The only stored tier a new row may overwrite.

With two producing tiers, "rank the tiers and overwrite anything worse" and
"overwrite a ``local`` row" are the same rule, and this one is legible in SQL
without a CASE expression. A third tier would make it a rank; two does not."""


async def get(session: AsyncSession, *, file_hash: str) -> FileExtraction | None:
    """Tier 0's Postgres half — the extraction for these bytes, if one exists.

    Content-addressed and global: the primary key is the hash alone.
    """
    result = await session.execute(
        select(FileExtraction).where(FileExtraction.file_hash == file_hash)
    )
    return result.scalar_one_or_none()


async def upsert(
    session: AsyncSession,
    *,
    file_hash: str,
    text: str,
    provider: str,
    model: str,
    confidence: str,
    tier: ExtractionTier,
    pages: int | None = None,
) -> FileExtraction:
    """Store an extraction, upgrading a local reading but never downgrading one.

    ``INSERT ... ON CONFLICT DO UPDATE`` on the ``file_hash`` primary key,
    guarded so the update only fires against a stored ``local`` row. The three
    ways two writers collide here all end correctly:

    * both tier 2 — the second overwrites the first with an equivalent reading;
    * tier 3 then tier 2 — the OCR row is upgraded, which is invariant 6's
      retroactive improvement happening within a single request;
    * tier 2 then tier 3 — the guard refuses, and the good reading survives a
      later outage's degraded one.

    Returns the row that is actually stored, re-read when the guard refused, so
    the caller always sees what a subsequent tier-0 hit would see rather than
    what this call hoped to write.
    """
    statement = (
        pg_insert(FileExtraction)
        .values(
            file_hash=file_hash,
            text=text,
            extracted_by_provider=provider,
            extracted_by_model=model,
            extraction_confidence=confidence,
            tier=tier,
            pages=pages,
        )
        .on_conflict_do_update(
            index_elements=[FileExtraction.file_hash],
            set_={
                "text": text,
                "extracted_by_provider": provider,
                "extracted_by_model": model,
                "extraction_confidence": confidence,
                "tier": tier,
                "pages": pages,
            },
            where=FileExtraction.tier == _UPGRADABLE_FROM,
        )
        .returning(FileExtraction)
    )
    result = await session.execute(statement)
    written = result.scalar_one_or_none()
    if written is not None:
        return written

    # The guard refused: a better row is already there. Re-read it rather than
    # returning what we tried to write — the caller is about to hand this text
    # to a model, and it must be the text the database actually holds.
    existing = await get(session, file_hash=file_hash)
    if existing is None:  # pragma: no cover — the conflict proves the row exists
        raise RuntimeError(f"file_extractions row for {file_hash!r} vanished mid-upsert")
    return existing
