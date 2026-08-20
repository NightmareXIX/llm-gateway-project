"""``POST /v1/files`` and ``GET /v1/files/{file_hash}`` — Phase 4 Step 3.

What is worth asserting here is not that a row round-trips — it is the four
places this endpoint is deliberately paranoid, because each of them is a trap
the plan names by number:

* the cap is enforced on bytes counted, not on ``Content-Length`` (trap 3);
* the stored type comes from the magic bytes, not from what the client claimed
  (trap 2);
* identical bytes are one object and per-user rows (D24);
* somebody else's hash is a 404, never a 403.

No network and no disk: the object store is a :class:`MemoryStore` for every
test in this module, so an upload is a dict write.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import File
from app.perception.storage import MemoryStore, StorageUnavailable, object_path
from tests.conftest import TokenFactory, assert_envelope

pytestmark = pytest.mark.integration

FILES = "/v1/files"

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP_BYTES = b"RIFF" + (72).to_bytes(4, "little") + b"WEBP" + b"\x00" * 64
EXE_BYTES = b"MZ\x90\x00" + b"\x00" * 64
"""A Windows PE header — the canonical "renamed to .pdf" payload. The sniffer
does not recognize it, and the sniffer's range *is* the allowlist."""


def _headers(make_jwt: TokenFactory, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(**kwargs)}"}


def _upload(
    content: bytes,
    *,
    filename: str = "report.pdf",
    content_type: str = "application/pdf",
) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (filename, content, content_type)}


async def _row_count(session: AsyncSession, *, file_hash: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(File).where(File.file_hash == file_hash)
    )
    return int(result.scalar_one())


@pytest.fixture
def store(app: FastAPI) -> MemoryStore:
    """Swap the lifespan's store for a dict.

    The ``app`` fixture builds whatever ``FILES_STORAGE_BACKEND`` names — a
    ``SupabaseStore`` over the JWKS mock transport by default, which would 404
    on a storage path. Every test in this module wants the in-memory one, and
    wants to be able to look inside it.
    """
    memory = MemoryStore()
    app.state.object_store = memory
    return memory


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
async def test_upload_stores_bytes_and_records_ownership(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    db_session: AsyncSession,
) -> None:
    """One upload: 201, the hash is the SHA-256 of what we sent, one object."""
    response = await client.post(FILES, files=_upload(PDF_BYTES), headers=_headers(make_jwt))

    assert response.status_code == 201
    body = response.json()
    expected_hash = hashlib.sha256(PDF_BYTES).hexdigest()
    assert body == {
        "file_hash": expected_hash,
        "filename": "report.pdf",
        "mime": "application/pdf",
        "bytes": len(PDF_BYTES),
        "created_at": body["created_at"],
        "deduplicated": False,
    }

    # Content-addressed and sharded, per D23.
    assert await store.exists(object_path(expected_hash))
    assert await store.get(object_path(expected_hash)) == PDF_BYTES
    assert await _row_count(db_session, file_hash=expected_hash) == 1


@pytest.mark.parametrize(
    ("content", "expected_mime"),
    [
        (PDF_BYTES, "application/pdf"),
        (PNG_BYTES, "image/png"),
        (JPEG_BYTES, "image/jpeg"),
        (WEBP_BYTES, "image/webp"),
    ],
)
async def test_every_allowlisted_format_is_recognized(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    content: bytes,
    expected_mime: str,
) -> None:
    """The four formats §1 allows, each identified from its magic bytes alone —
    the declared type below is wrong on purpose for three of the four."""
    response = await client.post(
        FILES,
        files=_upload(content, filename="thing.bin", content_type="application/octet-stream"),
        headers=_headers(make_jwt),
    )
    assert response.status_code == 201
    assert response.json()["mime"] == expected_mime


# --------------------------------------------------------------------------- #
# Trap 2 — the declared type is a claim
# --------------------------------------------------------------------------- #
async def test_a_png_named_pdf_is_stored_as_a_png(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    """The sniffed type wins and is what gets stored. Not an error: a PNG called
    ``report.pdf`` is a perfectly good PNG, and every tier can read it."""
    response = await client.post(
        FILES,
        files=_upload(PNG_BYTES, filename="report.pdf", content_type="application/pdf"),
        headers=_headers(make_jwt),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mime"] == "image/png"
    assert body["filename"] == "report.pdf"


async def test_an_executable_named_pdf_is_415(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    db_session: AsyncSession,
) -> None:
    """A format the sniffer does not recognize is a format no tier can read.
    Refused before anything touches PyMuPDF, and nothing is stored."""
    response = await client.post(
        FILES,
        files=_upload(EXE_BYTES, filename="invoice.pdf"),
        headers=_headers(make_jwt),
    )

    assert response.status_code == 415
    error = assert_envelope(response.json())
    assert error["code"] == "unsupported_media_type"
    assert await _row_count(db_session, file_hash=hashlib.sha256(EXE_BYTES).hexdigest()) == 0
    assert store._objects == {}


# --------------------------------------------------------------------------- #
# Trap 3 — the cap is counted, not claimed
# --------------------------------------------------------------------------- #
async def test_oversized_upload_is_413_and_stores_nothing(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    db_session: AsyncSession,
) -> None:
    """11MB against the 10MB default. The 413 arrives, and neither the object
    store nor ``files`` has anything in it afterwards."""
    limit = get_settings().FILE_MAX_BYTES
    oversized = PDF_BYTES + b"\x00" * (limit + 1_000_000 - len(PDF_BYTES))

    response = await client.post(
        FILES, files=_upload(oversized, filename="huge.pdf"), headers=_headers(make_jwt)
    )

    assert response.status_code == 413
    error = assert_envelope(response.json())
    assert error["code"] == "payload_too_large"
    assert store._objects == {}
    assert await _row_count(db_session, file_hash=hashlib.sha256(oversized).hexdigest()) == 0


async def test_a_lying_content_length_does_not_help(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    """The cap is enforced on bytes counted while parsing, so understating the
    body's size in the header changes nothing — which is the entire point of
    parsing the multipart body ourselves rather than declaring an
    ``UploadFile``."""
    limit = get_settings().FILE_MAX_BYTES
    oversized = PDF_BYTES + b"\x00" * (limit + 1000 - len(PDF_BYTES))
    request = client.build_request(
        "POST", FILES, files=_upload(oversized, filename="huge.pdf"), headers=_headers(make_jwt)
    )
    request.headers["content-length"] = "42"

    response = await client.send(request)

    assert response.status_code == 413
    assert store._objects == {}


# --------------------------------------------------------------------------- #
# D24 — one object, per-user rows
# --------------------------------------------------------------------------- #
async def test_the_same_file_twice_is_one_object_and_one_row(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    db_session: AsyncSession,
) -> None:
    """Two responses, one ``files`` row, one object — and the second says so."""
    headers = _headers(make_jwt)

    first = await client.post(FILES, files=_upload(PDF_BYTES), headers=headers)
    second = await client.post(FILES, files=_upload(PDF_BYTES), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is True
    # Byte-identical apart from the flag: the second response describes the row
    # the first one wrote, `created_at` included.
    assert first.json()["created_at"] == second.json()["created_at"]

    file_hash = hashlib.sha256(PDF_BYTES).hexdigest()
    assert await _row_count(db_session, file_hash=file_hash) == 1
    assert list(store._objects) == [object_path(file_hash)]


async def test_two_users_uploading_the_same_bytes_get_two_rows_and_one_object(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
    db_session: AsyncSession,
) -> None:
    """Bytes are global, the right to reference them is not (D24). The second
    upload is *not* a dedup hit — this user has never claimed the hash — but it
    writes no second copy of the object either."""
    alice = _headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _headers(make_jwt, sub=uuid4(), email="bob@example.com")

    first = await client.post(FILES, files=_upload(PDF_BYTES), headers=alice)
    second = await client.post(FILES, files=_upload(PDF_BYTES), headers=bob)

    assert first.status_code == second.status_code == 201
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is False

    file_hash = hashlib.sha256(PDF_BYTES).hexdigest()
    assert await _row_count(db_session, file_hash=file_hash) == 2
    assert list(store._objects) == [object_path(file_hash)]


async def test_an_existing_object_is_not_rewritten_for_a_new_owner(
    app: FastAPI,
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    """The object is already there from somebody else's upload, so ``put`` is
    never called — only ``exists``. Asserted by counting, because "skip the
    upload" is a quota and bandwidth claim, not a cosmetic one."""
    puts: list[str] = []

    class _CountingStore(MemoryStore):
        async def put(self, path: str, data: bytes, *, mime: str) -> None:
            puts.append(path)
            await super().put(path, data, mime=mime)

    counting = _CountingStore()
    app.state.object_store = counting

    await client.post(FILES, files=_upload(PDF_BYTES), headers=_headers(make_jwt, sub=uuid4()))
    await client.post(FILES, files=_upload(PDF_BYTES), headers=_headers(make_jwt, sub=uuid4()))

    assert puts == [object_path(hashlib.sha256(PDF_BYTES).hexdigest())]


# --------------------------------------------------------------------------- #
# GET — metadata only, ownership-scoped
# --------------------------------------------------------------------------- #
async def test_get_returns_metadata_for_the_owner(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    headers = _headers(make_jwt)
    uploaded = (await client.post(FILES, files=_upload(PDF_BYTES), headers=headers)).json()

    response = await client.get(f"{FILES}/{uploaded['file_hash']}", headers=headers)

    assert response.status_code == 200
    # No bytes, no URL, no storage path — D23's private bucket has no reader
    # but the gateway itself.
    assert response.json() == {
        key: value for key, value in uploaded.items() if key != "deduplicated"
    }


async def test_another_users_hash_is_a_404(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    """Not a 403: a 403 would confirm the hash names real bytes, which is
    exactly what makes a 64-character identifier worth probing."""
    alice = _headers(make_jwt, sub=uuid4(), email="alice@example.com")
    bob = _headers(make_jwt, sub=uuid4(), email="bob@example.com")
    uploaded = (await client.post(FILES, files=_upload(PDF_BYTES), headers=alice)).json()

    response = await client.get(f"{FILES}/{uploaded['file_hash']}", headers=bob)

    assert response.status_code == 404
    assert assert_envelope(response.json())["code"] == "file_not_found"


async def test_an_unknown_hash_is_the_same_404(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
) -> None:
    response = await client.get(f"{FILES}/{'a' * 64}", headers=_headers(make_jwt))
    assert response.status_code == 404
    assert assert_envelope(response.json())["code"] == "file_not_found"


async def test_a_malformed_hash_never_reaches_the_database(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
) -> None:
    """The path pattern is SHA-256's, so a typo is a 422 from pydantic."""
    response = await client.get(f"{FILES}/not-a-hash", headers=_headers(make_jwt))
    assert response.status_code == 422
    assert assert_envelope(response.json())["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# Malformed and unhappy bodies
# --------------------------------------------------------------------------- #
async def test_a_body_without_a_file_part_is_a_400(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    """A well-formed multipart body whose file part is under a different field
    name. Every other part is parsed and discarded, so what is left is nothing."""
    response = await client.post(
        FILES,
        files={"attachment": ("report.pdf", PDF_BYTES, "application/pdf")},
        data={"notes": "no file under the name we asked for"},
        headers=_headers(make_jwt),
    )
    assert response.status_code == 400
    assert assert_envelope(response.json())["code"] == "missing_file"
    assert store._objects == {}


async def test_an_empty_file_is_a_400(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    """Zero bytes have a valid SHA-256 and no readable content. Refused here
    rather than at tier 3, where it would look like an OCR failure."""
    response = await client.post(
        FILES, files=_upload(b"", filename="empty.pdf"), headers=_headers(make_jwt)
    )
    assert response.status_code == 400
    assert assert_envelope(response.json())["code"] == "empty_file"


async def test_a_non_multipart_body_is_a_400(
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    store: MemoryStore,
) -> None:
    response = await client.post(FILES, json={"file": "hello"}, headers=_headers(make_jwt))
    assert response.status_code == 400
    assert assert_envelope(response.json())["code"] == "invalid_content_type"


async def test_storage_failure_is_a_503_not_a_500(
    app: FastAPI,
    client: httpx.AsyncClient,
    make_jwt: TokenFactory,
    db_session: AsyncSession,
) -> None:
    """``StorageUnavailable`` is the one exception every backend raises, and it
    never carries the key or the bytes. The endpoint turns it into a 503 with a
    code a client can retry on — and writes no row."""

    class _BrokenStore(MemoryStore):
        async def exists(self, path: str) -> bool:
            raise StorageUnavailable("supabase storage returned 500")

    app.state.object_store = _BrokenStore()

    response = await client.post(FILES, files=_upload(PDF_BYTES), headers=_headers(make_jwt))

    assert response.status_code == 503
    assert assert_envelope(response.json())["code"] == "storage_unavailable"
    assert await _row_count(db_session, file_hash=hashlib.sha256(PDF_BYTES).hexdigest()) == 0


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
async def test_upload_requires_credentials(
    client: httpx.AsyncClient,
    store: MemoryStore,
) -> None:
    response = await client.post(FILES, files=_upload(PDF_BYTES))
    assert response.status_code == 401
    assert store._objects == {}


async def test_an_api_key_can_upload_too(
    client: httpx.AsyncClient,
    store: MemoryStore,
    user_factory: Callable[..., Any],
    api_key_factory: Callable[..., Any],
) -> None:
    """D7: both credential types collapse to one ``Principal``, and ownership
    keys on ``user_id`` regardless of which was presented."""
    user = await user_factory()
    plaintext, _api_key = await api_key_factory(user=user)

    response = await client.post(FILES, files=_upload(PDF_BYTES), headers={"X-API-Key": plaintext})

    assert response.status_code == 201
    # The row belongs to the key's owner, not to the key.
    read = await client.get(
        f"{FILES}/{response.json()['file_hash']}", headers={"X-API-Key": plaintext}
    )
    assert read.status_code == 200
    assert UUID(str(user.id)) == user.id
