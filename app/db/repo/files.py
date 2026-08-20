"""Reads and writes against ``files`` — who may reference which bytes.

The table this module owns is not the bytes. The bytes are content-addressed
and global, one object per distinct SHA-256 in a private bucket; a ``files``
row is one *user's right to reference* one hash (D24). Two people uploading
the same PDF write one object and two rows, and that asymmetry is the whole
design: re-storing identical bytes per user is waste, but "not guessable" is
not an authorization model, so the right to name a hash has to be recorded
somewhere.

**Ownership is scoped in the SQL, always** — the same rule ``conversations``
follows, and for the same reason. Every read here takes a ``user_id`` and puts
it in the WHERE clause; there is no unscoped ``get`` for a caller to reach for
by mistake, so a hash belonging to someone else and a hash that was never
uploaded produce the same ``None`` and the same 404. A 403 would confirm the
hash names something real, which is exactly what an enumeration attempt wants.

There is deliberately no "does anybody own this hash" query. Whether the
*object* already exists is a question for the object store
(``ObjectStore.exists``), not for this table — routing it through here would
turn one user's upload history into an oracle another user could probe.

Repositories take a session and never commit. The caller owns the transaction
boundary.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File


async def get_owned(session: AsyncSession, *, user_id: UUID, file_hash: str) -> File | None:
    """Load this user's claim on a hash, or ``None`` when it is not theirs.

    The dedup check ``POST /v1/files`` runs *before* it touches the object
    store: a hash the caller already owns needs no upload, no row and no
    second copy of anything.
    """
    result = await session.execute(
        select(File).where(File.user_id == user_id, File.file_hash == file_hash)
    )
    return result.scalar_one_or_none()


async def create_if_absent(
    session: AsyncSession,
    *,
    user_id: UUID,
    file_hash: str,
    filename: str,
    mime: str,
    size_bytes: int,
    storage_path: str,
) -> tuple[File, bool]:
    """Record this user's claim on a hash. Returns ``(row, created)``.

    ``INSERT ... ON CONFLICT DO NOTHING`` against ``uq_files_user_id_file_hash``
    rather than a SELECT and a conditional INSERT. The endpoint has already run
    :func:`get_owned`, so reaching a conflict here means two uploads of the
    same bytes by the same user arrived together — rare, but a plain INSERT
    would answer one of them with a 500 for doing something entirely
    reasonable.

    ``created=False`` on that path, so the response says ``deduplicated: true``
    and stays honest: this request wrote nothing.
    """
    statement = (
        pg_insert(File)
        .values(
            user_id=user_id,
            file_hash=file_hash,
            filename=filename,
            mime=mime,
            bytes=size_bytes,
            storage_path=storage_path,
        )
        .on_conflict_do_nothing(constraint="uq_files_user_id_file_hash")
        .returning(File)
    )
    result = await session.execute(statement)
    created = result.scalar_one_or_none()
    if created is not None:
        return created, True

    # The conflict fired: the row is already there, written by a request that
    # got here first. Re-read it rather than reconstructing one from the
    # values we tried to insert — `created_at` and the winning request's
    # `filename` belong to that row, not to this one.
    existing = await get_owned(session, user_id=user_id, file_hash=file_hash)
    if existing is None:  # pragma: no cover — the conflict proves the row exists
        raise RuntimeError(f"files row for {file_hash!r} vanished between insert and read")
    return existing, False
