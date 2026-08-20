"""Wire models for ``/v1/files`` — upload a document, read back what we kept.

**The hash is the identifier.** Not the row id: a ``file_ref`` on a chat turn
names a ``file_hash``, the object store's path is derived from it, and
``file_extractions`` is keyed on it alone (D24). Exposing a second identifier
would invite a client to reference the wrong one, and there is nothing the row
id would let anyone do that the hash does not.

**No URL, no bytes, no storage path.** The bucket is private and stays private
(D23) — there is no download endpoint in this phase and no signed URL is ever
generated, so the only reader of the bytes is the gateway itself, resolving an
attachment for a model. ``storage_path`` is an internal detail of that and is
deliberately absent from every response here.

Request/response models only; nothing here imports from the rest of the app.
The sniffing, the cap and the ownership query all live in
``app/api/v1/files.py``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

FILE_HASH_PATTERN = r"^[0-9a-f]{64}$"
"""SHA-256, lowercase hex. Used by the path parameter here and — from Step 4 —
by ``InputMessage.file_refs``, so a malformed hash is a 422 from pydantic
rather than a database round trip."""


class FileOut(BaseModel):
    """One uploaded file's metadata. What ``GET /v1/files/{file_hash}`` returns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_hash: str = Field(pattern=FILE_HASH_PATTERN)
    """SHA-256 of the bytes, computed while streaming the upload. The name a
    chat turn references the file by."""

    filename: str
    """What the client called it. Kept for display only — nothing in the
    perception lane routes on it, because the extension is a claim and the
    magic bytes are not."""

    mime: str
    """The *sniffed* type, which is not necessarily the ``Content-Type`` the
    upload declared (trap 2). A PNG named ``report.pdf`` is stored, and
    reported here, as ``image/png``."""

    bytes: int
    created_at: datetime
    """When *this user* claimed the hash. Two users uploading identical bytes
    get two rows with two timestamps and one object."""


class FileUploadResponse(FileOut):
    """What ``POST /v1/files`` returns. 201 either way — see ``deduplicated``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deduplicated: bool
    """True when this user already owned this hash, so nothing was stored and
    no row was written — the response describes the row that was already
    there. The status is 201 regardless, because the endpoint's promise is
    "this hash is now yours to reference" and that is equally true both times;
    this field is what says whether any bytes moved."""
