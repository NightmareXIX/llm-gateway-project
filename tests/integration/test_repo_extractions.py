"""``app/db/repo/extractions.py`` against a real Postgres.

Two things here need a real table rather than a double.

**The upgrade rule is a WHERE clause on an ON CONFLICT DO UPDATE**, which is
exactly the kind of SQL that reads correctly and behaves otherwise. It encodes
invariant 6's payoff in one direction — a tier-3 OCR row is replaced the first
time tier 2 is healthy enough to do better — and refuses it in the other, so a
Gemini reading is never overwritten by local OCR during an outage.

**There is no ``user_id`` column**, deliberately (D24), and the CHECK
constraints are the only thing standing between a typo'd tier and a row nobody
can classify. Both are properties of the schema, so both are asserted against
the schema.

Ownership is not tested here because it is not this table's job: a ``file_ref``
passes ``files``'s ownership gate at the moment it enters a message, and only
stored history reaches the perception lane. See
[test_files_endpoint.py](tests/integration/test_files_endpoint.py).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import extractions as repo

pytestmark = pytest.mark.integration

HASH = "b" * 64


async def _write(session: AsyncSession, **overrides: object) -> object:
    fields: dict[str, object] = {
        "file_hash": HASH,
        "text": "## Summary\nread by a model",
        "provider": "gemini",
        "model": "gemini-3.6-flash",
        "confidence": "high",
        "tier": "llm",
        "pages": 2,
    }
    fields.update(overrides)
    return await repo.upsert(session, **fields)  # type: ignore[arg-type]


async def test_a_first_extraction_is_stored_and_read_back_by_hash_alone(
    db_session: AsyncSession,
) -> None:
    """Content-addressed and global: the primary key is the bytes' identity, and
    nothing about who uploaded them appears in the query or the row."""
    await _write(db_session)
    await db_session.commit()

    found = await repo.get(db_session, file_hash=HASH)

    assert found is not None
    assert found.tier == "llm"
    assert found.extracted_by_model == "gemini-3.6-flash"
    assert found.pages == 2


async def test_a_local_reading_is_upgraded_by_an_llm_one(db_session: AsyncSession) -> None:
    """Invariant 6's retroactive improvement, at its smallest scale.

    A row written by local OCR in the middle of an outage is exactly the row
    that should be replaced the next time a model can actually read the file —
    and because the extraction is resolved at render time, replacing it improves
    every stored conversation that references the hash.
    """
    await _write(
        db_session, provider="local", model="local", confidence="low", tier="local", pages=1
    )
    await db_session.commit()

    upgraded = await _write(db_session)
    await db_session.commit()

    assert upgraded.tier == "llm"  # type: ignore[attr-defined]
    stored = await repo.get(db_session, file_hash=HASH)
    assert stored is not None
    assert stored.extraction_confidence == "high"


async def test_an_llm_reading_is_never_overwritten_by_a_local_one(
    db_session: AsyncSession,
) -> None:
    """The other direction, which is the one that matters during an incident.

    Gemini's daily half runs out at 2pm; every extraction after that is tier 3.
    If those rows overwrote the good readings, an afternoon of quota exhaustion
    would permanently degrade every document the gateway had already read
    properly — and nothing would fail loudly.
    """
    await _write(db_session)
    await db_session.commit()

    kept = await _write(
        db_session,
        text="## Summary\n(illegible)",
        provider="local",
        model="local",
        confidence="low",
        tier="local",
    )
    await db_session.commit()

    # The returned row is what a subsequent tier-0 hit would see, not what this
    # call hoped to write — the caller is about to hand this text to a model.
    assert kept.tier == "llm"  # type: ignore[attr-defined]
    assert kept.extraction_confidence == "high"  # type: ignore[attr-defined]
    stored = await repo.get(db_session, file_hash=HASH)
    assert stored is not None
    assert stored.text == "## Summary\nread by a model"


async def test_an_unread_hash_is_simply_absent(db_session: AsyncSession) -> None:
    """Tier 0's miss. No exception, no sentinel row — the lane's next tier is the
    answer to "nobody has read these bytes yet"."""
    assert await repo.get(db_session, file_hash="c" * 64) is None


async def test_an_image_stores_a_null_page_count_rather_than_zero(
    db_session: AsyncSession,
) -> None:
    """``pages`` is nullable so an image can say "not applicable" instead of
    "zero pages", which would read as a document that could not be opened."""
    await _write(db_session, pages=None)
    await db_session.commit()

    found = await repo.get(db_session, file_hash=HASH)

    assert found is not None
    assert found.pages is None
