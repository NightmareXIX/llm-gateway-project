"""``/v1/files`` — upload a document, read back what we kept about it.

The endpoint that puts bytes into the system, and the only one that ever does.
It stores bytes and metadata and **nothing else**: no extraction runs here (D22
— extraction is resolved at render time, because tier 1 cannot exist at upload
time and invariant 6's payoff is that a better extractor retroactively improves
old conversations), so this route either succeeds or does not. There is no
partial-success state for anyone to have to decide about.

Three things it is careful about, in the order they bite:

**The size cap is enforced mid-stream** (trap 3). A ``Content-Length`` is a
claim. The body is parsed incrementally out of ``request.stream()``, hashed and
counted as it arrives, and abandoned the instant the running count passes
``FILE_MAX_BYTES`` — so an 11MB upload against a 10MB cap costs 10MB of memory
and a 413, not 11MB and a 413. This is why the route parses multipart itself
rather than declaring an ``UploadFile`` parameter: FastAPI would have finished
parsing the whole body, spooling it to a temporary file of unbounded size,
before the handler ever ran.

**The declared type is not trusted** (trap 2). A browser will call anything
``application/pdf``. The type is sniffed from the leading bytes, the sniffed
type is what gets stored and reported, and a mismatch is logged rather than
rejected — a PNG named ``report.pdf`` is a perfectly good PNG.

**Dedup is content-addressed and ownership is not** (D24). The object path is
derived from the hash, so identical bytes are one object; the ``files`` row is
per user, so the right to reference that hash is not. A hash this user already
owns skips the store entirely; a hash somebody *else* already uploaded skips
the upload but still writes the row.

``GET /v1/files/{file_hash}`` returns metadata and only metadata — there is no
download endpoint and no signed URL in this phase (D23). The bucket is private
and the only reader of the bytes is the gateway itself, resolving an attachment
for a model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Path, Request, status
from python_multipart.multipart import MultipartParser, parse_options_header

from app.auth.dependency import PrincipalDep, RateLimitDep
from app.config import get_settings
from app.core.errors import (
    InvalidRequest,
    NotFound,
    PayloadTooLarge,
    ServiceUnavailable,
    UnsupportedMediaType,
)
from app.core.logging import get_logger
from app.db.models import File
from app.db.repo import files as files_repo
from app.deps import SessionDep, StoreDep
from app.perception.storage import StorageUnavailable, object_path
from app.schemas.errors import AUTHENTICATED_ERROR_RESPONSES, NOT_FOUND_RESPONSE, ErrorResponse
from app.schemas.files import FILE_HASH_PATTERN, FileOut, FileUploadResponse

if TYPE_CHECKING:  # pragma: no cover — the callback table's type, erased at runtime
    from python_multipart.multipart import MultipartCallbacks

logger = get_logger("app.api.files")

router = APIRouter(prefix="/v1/files", tags=["files"], responses=AUTHENTICATED_ERROR_RESPONSES)

UPLOAD_FIELD = "file"
"""The multipart field the bytes arrive in. One file per request — a batch
upload would have to decide what a partial failure means, and the frontend's
composer uploads one at a time anyway."""

MAX_FILENAME_LENGTH = 255
"""Longer than any real filename and shorter than a payload. The name is
display-only, but it is stored, so it gets a bound."""


# --------------------------------------------------------------------------- #
# Type sniffing (trap 2)
# --------------------------------------------------------------------------- #
def sniff_mime(head: bytes) -> str | None:
    """The type the leading bytes actually are, or ``None`` if unrecognized.

    **The sniffer's range is the allowlist.** There is no second list to keep
    in step: a format this function cannot identify from its magic bytes is a
    format no tier of the perception lane can read, and it is a 415. That also
    makes the failure mode the safe one — an unknown format is refused rather
    than passed to PyMuPDF to find out.

    PDF, PNG, JPEG, WebP, per §1's allowlist. No audio, no video, no office
    formats: a format nobody has a tier-3 fallback for is a format that fails
    at 3am rather than degrading.
    """
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


SNIFF_PREFIX_BYTES: Final = 12
"""How many leading bytes :func:`sniff_mime` needs — ``RIFF????WEBP`` is the
longest signature it checks."""


# --------------------------------------------------------------------------- #
# Streaming multipart read (trap 3)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _StreamedUpload:
    """One file part, hashed and counted as it arrived.

    The bytes are held in memory on purpose: ``ObjectStore.put`` takes
    ``bytes``, and the cap that bounds this buffer is the same cap the endpoint
    advertises. "Do not buffer the rest" means the accumulation *stops* at the
    limit, not that it never happens — a 10MB ceiling is what makes holding it
    acceptable.
    """

    filename: str | None = None
    declared_mime: str | None = None
    data: bytearray = field(default_factory=bytearray)
    digest: hashlib._Hash = field(default_factory=hashlib.sha256)
    size: int = 0
    complete: bool = False


class _FilePartReader:
    """``python_multipart`` callbacks that keep exactly one file part.

    Deliberately not Starlette's ``MultiPartParser``: that one hands every file
    part to a ``SpooledTemporaryFile`` with no total-size ceiling, which is the
    behaviour trap 3 is about. This keeps the one field we asked for, hashes it
    on the way past, and raises as soon as it is too big.

    Every other part is parsed and discarded. A stray form field is not an
    error worth failing an upload over, and silently ignoring it is what
    FastAPI would do too.
    """

    def __init__(self, *, field_name: str, max_bytes: int) -> None:
        self._field_name = field_name
        self._max_bytes = max_bytes
        self._header_name = b""
        self._header_value = b""
        self._headers: dict[bytes, bytes] = {}
        self._keeping = False
        self.upload = _StreamedUpload()

    # -- headers ------------------------------------------------------------ #
    def on_part_begin(self) -> None:
        self._headers = {}
        self._header_name = b""
        self._header_value = b""
        self._keeping = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._header_name.lower()] = self._header_value
        self._header_name = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        _disposition, options = parse_options_header(self._headers.get(b"content-disposition"))
        name = options.get(b"name", b"").decode("latin-1")
        if name != self._field_name or self.upload.complete:
            # Not ours, or a second file part after we already have one. The
            # first wins; this endpoint takes one file.
            return

        self._keeping = True
        raw_filename = options.get(b"filename")
        if raw_filename is not None:
            self.upload.filename = raw_filename.decode("utf-8", errors="replace")
        declared = self._headers.get(b"content-type")
        if declared is not None:
            self.upload.declared_mime = declared.decode("latin-1").strip()

    # -- body --------------------------------------------------------------- #
    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if not self._keeping:
            return
        chunk = data[start:end]
        if self.upload.size + len(chunk) > self._max_bytes:
            # Raised from inside `parser.write()`, so the caller's `async for`
            # over the request stream is abandoned right here and the rest of
            # the body is never read.
            raise PayloadTooLarge(
                f"The file is larger than the {self._max_bytes:,}-byte limit this gateway accepts."
            )
        self.upload.data.extend(chunk)
        self.upload.digest.update(chunk)
        self.upload.size += len(chunk)

    def on_part_end(self) -> None:
        if self._keeping:
            self.upload.complete = True
            self._keeping = False

    def callbacks(self) -> MultipartCallbacks:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
        }


async def _read_upload(request: Request, *, max_bytes: int) -> _StreamedUpload:
    """Parse the multipart body incrementally, keeping one file part.

    The whole reason this function exists rather than an ``UploadFile``
    parameter: the loop below can stop. FastAPI's form handling cannot — by the
    time a handler with an ``UploadFile`` parameter runs, the entire body has
    been read and spooled, and a cap checked afterwards has already lost.
    """
    media_type, params = parse_options_header(request.headers.get("content-type"))
    if media_type != b"multipart/form-data":
        raise InvalidRequest(
            "Upload the file as multipart/form-data.",
            code="invalid_content_type",
        )
    boundary = params.get(b"boundary")
    if not boundary:
        raise InvalidRequest(
            "The multipart/form-data body is missing its boundary parameter.",
            code="invalid_multipart",
        )

    reader = _FilePartReader(field_name=UPLOAD_FIELD, max_bytes=max_bytes)
    parser = MultipartParser(boundary, reader.callbacks())
    async for chunk in request.stream():
        parser.write(chunk)
    parser.finalize()

    if not reader.upload.complete or reader.upload.filename is None:
        raise InvalidRequest(
            f"The request has no '{UPLOAD_FIELD}' file part.",
            code="missing_file",
        )
    if reader.upload.size == 0:
        raise InvalidRequest("The uploaded file is empty.", code="empty_file")
    return reader.upload


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _to_out(row: File) -> FileOut:
    return FileOut(
        file_hash=row.file_hash,
        filename=row.filename,
        mime=row.mime,
        bytes=row.bytes,
        created_at=row.created_at,
    )


@router.post(
    "",
    response_model=FileUploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "The body is not a usable upload."},
        413: {"model": ErrorResponse, "description": "The file is over the size cap."},
        415: {"model": ErrorResponse, "description": "The file is not a type we can read."},
        429: {"model": ErrorResponse, "description": "You are over your own tier's limit."},
        503: {"model": ErrorResponse, "description": "Object storage is unreachable."},
    },
    # Declared by hand because the route parses the body itself — see
    # `_read_upload`. Without this the schema would say the endpoint takes no
    # body at all, which is the one thing it definitely does take.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {UPLOAD_FIELD: {"type": "string", "format": "binary"}},
                        "required": [UPLOAD_FIELD],
                    }
                }
            },
        }
    },
)
async def upload_file(
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    store: StoreDep,
    _rate_limit: RateLimitDep = None,
) -> FileUploadResponse:
    """Store one file and record that this user may reference it.

    201 whether or not anything was written — ``deduplicated`` says which. The
    promise the status makes is "this hash is now yours to reference", and that
    is equally true when the row was already there.
    """
    settings = get_settings()
    upload = await _read_upload(request, max_bytes=settings.FILE_MAX_BYTES)

    mime = sniff_mime(bytes(upload.data[:SNIFF_PREFIX_BYTES]))
    if mime is None:
        raise UnsupportedMediaType(
            "That file is not a PDF, PNG, JPEG or WebP. Those are the formats "
            "the gateway can read.",
        )
    if upload.declared_mime and upload.declared_mime != mime:
        # Info, not a rejection: a PNG named `report.pdf` is a good PNG, and the
        # sniffed type is what everything downstream uses either way. Worth a
        # line because a systematic mismatch is a client bug worth finding.
        logger.info(
            "file.mime_mismatch", declared=upload.declared_mime, sniffed=mime, bytes=upload.size
        )

    file_hash = upload.digest.hexdigest()
    filename = upload.filename or "upload"
    if len(filename) > MAX_FILENAME_LENGTH:
        filename = filename[:MAX_FILENAME_LENGTH]

    # Dedup, before the store is touched at all: a hash this user already owns
    # needs no upload, no row, and no second copy of anything.
    owned = await files_repo.get_owned(session, user_id=principal.user_id, file_hash=file_hash)
    if owned is not None:
        logger.info("file.deduplicated", file_hash=file_hash, mime=owned.mime, bytes=owned.bytes)
        return FileUploadResponse(**_to_out(owned).model_dump(), deduplicated=True)

    path = object_path(file_hash)
    try:
        # The object may already be there from somebody else's upload — same
        # bytes, same path, nothing to write. The row below is what makes it
        # this user's to reference (D24).
        if not await store.exists(path):
            await store.put(path, bytes(upload.data), mime=mime)
    except StorageUnavailable as exc:
        # Normalized in `perception/storage.py` so nothing here ever sees an
        # httpx error, a URL, or the service-role key (trap 17).
        logger.warning("file.storage_unavailable", file_hash=file_hash, error=str(exc))
        raise ServiceUnavailable(
            "File storage is temporarily unavailable. Try the upload again.",
            code="storage_unavailable",
        ) from exc

    row, created = await files_repo.create_if_absent(
        session,
        user_id=principal.user_id,
        file_hash=file_hash,
        filename=filename,
        mime=mime,
        size_bytes=upload.size,
        storage_path=path,
    )
    await session.commit()

    # The filename is deliberately absent: it is user-supplied text that can
    # carry a person's name or a case number, and nothing operational needs it.
    logger.info("file.uploaded", file_hash=file_hash, mime=mime, bytes=upload.size, stored=created)
    return FileUploadResponse(**_to_out(row).model_dump(), deduplicated=not created)


@router.get("/{file_hash}", response_model=FileOut, responses=NOT_FOUND_RESPONSE)
async def read_file(
    principal: PrincipalDep,
    session: SessionDep,
    file_hash: Annotated[str, Path(pattern=FILE_HASH_PATTERN)],
) -> FileOut:
    """Metadata for one file this user owns. Never the bytes (D23).

    Ownership is scoped in the query, so somebody else's hash and a hash that
    was never uploaded are the same 404 — a 403 would confirm the hash names
    real bytes, which is exactly what makes a 64-character identifier worth
    probing.
    """
    row = await files_repo.get_owned(session, user_id=principal.user_id, file_hash=file_hash)
    if row is None:
        raise NotFound("That file does not exist.", code="file_not_found")
    return _to_out(row)
