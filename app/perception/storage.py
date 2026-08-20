"""D23 — where uploaded bytes live: one ``ObjectStore`` Protocol, three backends.

Render's free plan gives an ephemeral filesystem and a service that sleeps;
anything written to disk is gone on the next deploy. ``SupabaseStore`` is what
every deployed environment uses — Supabase Storage over the app's existing
shared ``httpx.AsyncClient``, no new SDK, no second connection pool.
``LocalStore`` is for a developer without Storage configured; ``MemoryStore``
is what the test suite uses, so a byte never touches a disk or a network in CI.

**The bucket is private and stays private.** There is no signed-URL and no
download endpoint anywhere in this phase — the only reader of the bytes is the
gateway itself, resolving an attachment for a model.

**Path is content-addressed** (``{hash[:2]}/{hash}``, :func:`object_path`).
The two-character shard is for the eventual object listing rather than for
lookup; dedup falls out of it for free, because two uploads of the same bytes
write the same path.

Every failure normalizes to :class:`StorageUnavailable` — never a raw
``httpx`` error, and never one carrying the key or the bytes (trap 17: the
service-role key bypasses row-level security on the whole Supabase project
and gets the same handling as a provider key)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from app.config import ConfigError, Settings
from app.core.logging import get_logger

logger = get_logger("app.perception.storage")


class StorageUnavailable(Exception):
    """An object store failed in a way the caller cannot recover from.

    The one exception every backend raises. Never carries the object's bytes
    or the credential used to reach it — only enough to say what operation
    failed and, for ``SupabaseStore``, the HTTP status that explains why.
    """


@runtime_checkable
class ObjectStore(Protocol):
    """D23's Protocol. Three implementations, chosen by ``FILES_STORAGE_BACKEND``."""

    async def put(self, path: str, data: bytes, *, mime: str) -> None:
        """Write ``data`` at ``path``. Idempotent: writing the same content-addressed
        path twice succeeds both times."""
        ...

    async def get(self, path: str) -> bytes:
        """Read the bytes at ``path``. Raises :class:`StorageUnavailable` on a miss —
        callers that need to distinguish "absent" from "unreachable" use :meth:`exists`
        first."""
        ...

    async def exists(self, path: str) -> bool:
        """Whether an object is already at ``path``, without fetching its bytes."""
        ...


def object_path(file_hash: str) -> str:
    """``{hash[:2]}/{hash}`` — the content-addressed path every backend stores under."""
    if len(file_hash) < 2:
        raise ValueError(f"file_hash too short to shard into a path: {file_hash!r}")
    return f"{file_hash[:2]}/{file_hash}"


def _logical_status(response: httpx.Response) -> int:
    """The status Supabase Storage actually means, not always the one on the wire.

    Verified against a live bucket while building this module, not guessed
    from documentation: a missing object and a non-upserted duplicate both
    arrive as a literal HTTP **400**, with the real status as a *string* in
    the JSON body — ``{"statusCode": "404", "error": "not_found", "code":
    "NoSuchKey"}`` and ``{"statusCode": "409", "error": "Duplicate", ...}``
    respectively. Reading ``response.status_code`` alone would make
    :meth:`SupabaseStore.exists` raise :class:`StorageUnavailable` on every
    ordinary miss instead of returning ``False`` — the dedup check Step 3
    builds on depends on this being right.

    Falls back to the literal HTTP status for everything else, which is what
    a real 5xx, a genuine 401, and every ``httpx.MockTransport`` fixture in
    this module's own tests send.
    """
    if response.status_code != 400:
        return response.status_code
    try:
        body = response.json()
    except ValueError:
        return response.status_code
    if not isinstance(body, dict):
        return response.status_code
    try:
        return int(body["statusCode"])
    except (KeyError, TypeError, ValueError):
        return response.status_code


# --------------------------------------------------------------------------- #
# Supabase Storage — every deployed environment
# --------------------------------------------------------------------------- #
class SupabaseStore:
    """Speaks the Storage REST API over the process's shared ``httpx.AsyncClient``.

    Constructed once in the lifespan with the same client every provider
    adapter shares, so it inherits the timeouts and the connection pool
    rather than opening a second one.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        bucket: str,
        service_role_key: str,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._bucket = bucket
        self._service_role_key = service_role_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._service_role_key}"}

    def _object_url(self, path: str) -> str:
        return f"{self._base_url}/storage/v1/object/{self._bucket}/{path}"

    def _info_url(self, path: str) -> str:
        return f"{self._base_url}/storage/v1/object/info/{self._bucket}/{path}"

    async def put(self, path: str, data: bytes, *, mime: str) -> None:
        try:
            response = await self._client.post(
                self._object_url(path),
                content=data,
                headers={**self._headers(), "Content-Type": mime, "x-upsert": "true"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "perception.storage_unreachable", operation="put", error_type=type(exc).__name__
            )
            raise StorageUnavailable("could not reach object storage") from exc

        status = _logical_status(response)
        if status == 409:
            # The path is the content hash, so an object already there is
            # identical bytes — a race with another upload of the same file,
            # not a conflict worth reporting.
            return
        if status >= 400:
            logger.warning(
                "perception.storage_error",
                operation="put",
                status_code=response.status_code,
                logical_status=status,
            )
            raise StorageUnavailable(f"object storage rejected the upload (status {status})")

    async def get(self, path: str) -> bytes:
        try:
            response = await self._client.get(self._object_url(path), headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning(
                "perception.storage_unreachable", operation="get", error_type=type(exc).__name__
            )
            raise StorageUnavailable("could not reach object storage") from exc

        status = _logical_status(response)
        if status >= 400:
            logger.warning(
                "perception.storage_error",
                operation="get",
                status_code=response.status_code,
                logical_status=status,
            )
            raise StorageUnavailable(f"object storage could not serve the object (status {status})")
        return response.content

    async def exists(self, path: str) -> bool:
        try:
            response = await self._client.get(self._info_url(path), headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning(
                "perception.storage_unreachable", operation="exists", error_type=type(exc).__name__
            )
            raise StorageUnavailable("could not reach object storage") from exc

        status = _logical_status(response)
        if status == 404:
            return False
        if status >= 400:
            logger.warning(
                "perception.storage_error",
                operation="exists",
                status_code=response.status_code,
                logical_status=status,
            )
            raise StorageUnavailable(f"object storage could not check the object (status {status})")
        return True


# --------------------------------------------------------------------------- #
# Local filesystem — a dev box without Storage configured
# --------------------------------------------------------------------------- #
class LocalStore:
    """Writes under a configured directory. Gone on the next deploy of a
    free-tier host, which is exactly why this is not the production backend."""

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        """``path`` is content-addressed and built by us, never taken verbatim
        from a client — but a store that trusted that assumption silently would
        be one refactor away from a path-traversal bug, so it is checked anyway."""
        candidate = (self._root / path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            raise StorageUnavailable(f"path escapes the store root: {path!r}") from None
        return candidate

    async def put(self, path: str, data: bytes, *, mime: str) -> None:
        del mime  # the local filesystem carries no content-type of its own
        target = self._resolve(path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        await asyncio.to_thread(_write)

    async def get(self, path: str) -> bytes:
        target = self._resolve(path)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except OSError as exc:
            raise StorageUnavailable(f"could not read local object: {path!r}") from exc

    async def exists(self, path: str) -> bool:
        target = self._resolve(path)
        return await asyncio.to_thread(target.exists)


# --------------------------------------------------------------------------- #
# In-memory — the test suite
# --------------------------------------------------------------------------- #
class MemoryStore:
    """Holds a dict. Nothing here ever touches a disk or the network, so the
    hard rule ("never call a live provider from a test") holds for uploads too."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(self, path: str, data: bytes, *, mime: str) -> None:
        del mime
        self._objects[path] = data

    async def get(self, path: str) -> bytes:
        try:
            return self._objects[path]
        except KeyError:
            raise StorageUnavailable(f"no such object: {path!r}") from None

    async def exists(self, path: str) -> bool:
        return path in self._objects


# --------------------------------------------------------------------------- #
# Construction — one call from the lifespan
# --------------------------------------------------------------------------- #
def build_store(*, client: httpx.AsyncClient, settings: Settings) -> ObjectStore:
    """Pick the backend ``FILES_STORAGE_BACKEND`` names. Called once, from the
    lifespan, mirroring ``providers.registry.build_registry``."""
    if settings.FILES_STORAGE_BACKEND == "supabase":
        service_role_key = settings.SUPABASE_SERVICE_ROLE_KEY
        # config.py's model validator already enforces this pairing; this is a
        # defensive second check, not a reachable path — hence no cover below.
        if service_role_key is None:  # pragma: no cover
            raise ConfigError(
                "FILES_STORAGE_BACKEND is 'supabase' but SUPABASE_SERVICE_ROLE_KEY is unset"
            )
        return SupabaseStore(
            client=client,
            base_url=settings.SUPABASE_URL,
            bucket=settings.FILES_BUCKET,
            service_role_key=service_role_key.get_secret_value(),
        )
    if settings.FILES_STORAGE_BACKEND == "local":
        return LocalStore(root=settings.FILES_LOCAL_DIR)
    return MemoryStore()
