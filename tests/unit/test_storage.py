"""D23's three ``ObjectStore`` backends.

``SupabaseStore`` is exercised entirely over ``httpx.MockTransport`` — no
network, per the hard rule in ``CLAUDE.md``. The three backends share one
behavioural contract (round-trip, ``exists``, a miss on ``get``); the
Supabase-specific wire shape (headers, the 409-is-success rule) gets its own
tests below that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.perception.storage import (
    LocalStore,
    MemoryStore,
    ObjectStore,
    StorageUnavailable,
    SupabaseStore,
    build_store,
    object_path,
)

BUCKET = "uploads"
BASE_URL = "https://test-project.supabase.co"
SERVICE_ROLE_KEY = "service_role_test_key_not_real"
PATH = "ab/abcd1234"
DATA = b"%PDF-1.4 not a real pdf, just bytes"

Handler = Callable[[httpx.Request], httpx.Response]


@asynccontextmanager
async def _store(handler: Handler) -> AsyncIterator[SupabaseStore]:
    """A ``SupabaseStore`` over a mock transport, closed on exit like every
    other adapter test in this repo closes its client (see
    ``tests/provider_fixtures.py``)."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield SupabaseStore(
            client=client, base_url=BASE_URL, bucket=BUCKET, service_role_key=SERVICE_ROLE_KEY
        )
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# object_path — D23's content-addressed sharding
# --------------------------------------------------------------------------- #
def test_object_path_shards_on_the_first_two_hex_characters() -> None:
    assert object_path("abcd1234") == "ab/abcd1234"


def test_object_path_refuses_a_hash_too_short_to_shard() -> None:
    with pytest.raises(ValueError, match="too short"):
        object_path("a")


# --------------------------------------------------------------------------- #
# SupabaseStore — the wire shape
# --------------------------------------------------------------------------- #
async def test_put_posts_to_the_object_endpoint_with_upsert_and_bearer_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    async with _store(handler) as store:
        await store.put(PATH, DATA, mime="application/pdf")

    assert len(requests) == 1
    sent = requests[0]
    assert sent.method == "POST"
    assert sent.url.path == f"/storage/v1/object/{BUCKET}/{PATH}"
    assert sent.headers["Authorization"] == f"Bearer {SERVICE_ROLE_KEY}"
    assert sent.headers["x-upsert"] == "true"
    assert sent.headers["Content-Type"] == "application/pdf"
    assert sent.content == DATA


async def test_a_409_on_put_is_success_not_an_error() -> None:
    """The path is the content hash, so an object already there is identical
    bytes — a race with another upload of the same file, not a conflict."""
    async with _store(lambda _r: httpx.Response(409)) as store:
        await store.put(PATH, DATA, mime="application/pdf")  # must not raise


async def test_a_wrapped_409_on_put_is_also_success() -> None:
    """The real API's shape, observed against a live bucket: a duplicate
    without upsert arrives as a literal HTTP 400 with the true status as a
    *string* inside the body — never a bare 409 in practice."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"statusCode": "409", "error": "Duplicate", "code": "KeyAlreadyExists"}
        )

    async with _store(handler) as store:
        await store.put(PATH, DATA, mime="application/pdf")  # must not raise


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
async def test_put_raises_storage_unavailable_on_any_other_error_status(status: int) -> None:
    async with _store(lambda _r: httpx.Response(status)) as store:
        with pytest.raises(StorageUnavailable):
            await store.put(PATH, DATA, mime="application/pdf")


async def test_put_normalizes_a_transport_failure_never_a_raw_httpx_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _store(handler) as store:
        with pytest.raises(StorageUnavailable) as excinfo:
            await store.put(PATH, DATA, mime="application/pdf")
        assert not isinstance(excinfo.value, httpx.HTTPError)


async def test_get_downloads_from_the_object_endpoint_and_returns_the_bytes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=DATA)

    async with _store(handler) as store:
        result = await store.get(PATH)

    assert result == DATA
    assert requests[0].method == "GET"
    assert requests[0].url.path == f"/storage/v1/object/{BUCKET}/{PATH}"
    assert requests[0].headers["Authorization"] == f"Bearer {SERVICE_ROLE_KEY}"


async def test_get_raises_storage_unavailable_on_a_miss() -> None:
    async with _store(lambda _r: httpx.Response(404)) as store:
        with pytest.raises(StorageUnavailable):
            await store.get(PATH)


async def test_get_raises_storage_unavailable_on_a_wrapped_miss() -> None:
    """``get`` never treats a missing object as anything other than an error —
    unlike ``exists``, it has no ``False`` to return — so the wrapped 404 shape
    must still raise, not silently return empty bytes."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"statusCode": "404", "error": "not_found", "code": "NoSuchKey"}
        )

    async with _store(handler) as store:
        with pytest.raises(StorageUnavailable):
            await store.get(PATH)


async def test_exists_reads_the_info_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"name": PATH})

    async with _store(handler) as store:
        result = await store.exists(PATH)

    assert result is True
    assert requests[0].url.path == f"/storage/v1/object/info/{BUCKET}/{PATH}"


async def test_exists_is_false_on_a_404_and_not_an_error() -> None:
    async with _store(lambda _r: httpx.Response(404)) as store:
        assert await store.exists(PATH) is False


async def test_exists_is_false_on_a_wrapped_404() -> None:
    """The real API's shape, observed against a live bucket: a missing object
    arrives as a literal HTTP 400 with the true status as a *string* inside
    the body — never a bare 404 in practice."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"statusCode": "404", "error": "not_found", "code": "NoSuchKey"}
        )

    async with _store(handler) as store:
        assert await store.exists(PATH) is False


async def test_exists_raises_storage_unavailable_on_a_real_failure() -> None:
    async with _store(lambda _r: httpx.Response(500)) as store:
        with pytest.raises(StorageUnavailable):
            await store.exists(PATH)


async def test_a_missing_bucket_also_reads_as_a_wrapped_404() -> None:
    """Also observed live: the API reports the same ``statusCode: "404"`` for
    a missing *bucket* as for a missing object (only ``error``/``code``
    differ — ``"Bucket not found"``/``"NoSuchBucket"`` vs. ``"not_found"``/
    ``"NoSuchKey"``), so ``exists`` cannot and does not tell the two apart.
    A misconfigured bucket therefore reads as "every file is absent" rather
    than as a startup-style hard failure — ``put``/``get`` still surface it,
    because a missing bucket is never a 409 and always ``status >= 400``
    there."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"statusCode": "404", "error": "Bucket not found", "code": "NoSuchBucket"},
        )

    async with _store(handler) as store:
        assert await store.exists(PATH) is False


async def test_put_into_a_missing_bucket_raises_storage_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"statusCode": "404", "error": "Bucket not found", "code": "NoSuchBucket"},
        )

    async with _store(handler) as store:
        with pytest.raises(StorageUnavailable):
            await store.put(PATH, DATA, mime="application/pdf")


async def test_supabase_store_round_trips_through_a_mock_transport() -> None:
    """The end-to-end claim Step 2's 'done when' names: put, then exists, then
    get, all through one in-memory fake bucket behind the mock transport."""
    fake_bucket: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        object_prefix = f"/storage/v1/object/{BUCKET}/"
        info_prefix = f"/storage/v1/object/info/{BUCKET}/"
        if request.method == "POST" and request.url.path.startswith(object_prefix):
            key = request.url.path.removeprefix(object_prefix)
            fake_bucket[key] = request.content
            return httpx.Response(200)
        if request.method == "GET" and request.url.path.startswith(info_prefix):
            key = request.url.path.removeprefix(info_prefix)
            return httpx.Response(200) if key in fake_bucket else httpx.Response(404)
        if request.method == "GET" and request.url.path.startswith(object_prefix):
            key = request.url.path.removeprefix(object_prefix)
            if key in fake_bucket:
                return httpx.Response(200, content=fake_bucket[key])
            return httpx.Response(404)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    async with _store(handler) as store:
        assert await store.exists(PATH) is False
        await store.put(PATH, DATA, mime="application/pdf")
        assert await store.exists(PATH) is True
        assert await store.get(PATH) == DATA


# --------------------------------------------------------------------------- #
# LocalStore
# --------------------------------------------------------------------------- #
async def test_local_store_round_trips(tmp_path: Path) -> None:
    store = LocalStore(root=tmp_path)
    assert await store.exists(PATH) is False

    await store.put(PATH, DATA, mime="application/pdf")

    assert await store.exists(PATH) is True
    assert await store.get(PATH) == DATA
    assert (tmp_path / PATH).read_bytes() == DATA


async def test_local_store_get_on_a_miss_raises_storage_unavailable(tmp_path: Path) -> None:
    store = LocalStore(root=tmp_path)
    with pytest.raises(StorageUnavailable):
        await store.get("missing/nothing-here")


@pytest.mark.parametrize(
    "escaping_path",
    ["../outside", "../../etc/passwd", "a/../../b", "/etc/passwd"],
)
async def test_local_store_refuses_a_path_that_escapes_its_root(
    tmp_path: Path, escaping_path: str
) -> None:
    store = LocalStore(root=tmp_path)
    with pytest.raises(StorageUnavailable, match="escapes"):
        await store.put(escaping_path, DATA, mime="application/pdf")


async def test_local_store_creates_its_root_directory(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist" / "yet"
    LocalStore(root=root)
    assert root.is_dir()


# --------------------------------------------------------------------------- #
# MemoryStore
# --------------------------------------------------------------------------- #
async def test_memory_store_round_trips() -> None:
    store = MemoryStore()
    assert await store.exists(PATH) is False

    await store.put(PATH, DATA, mime="application/pdf")

    assert await store.exists(PATH) is True
    assert await store.get(PATH) == DATA


async def test_memory_store_get_on_a_miss_raises_storage_unavailable() -> None:
    store = MemoryStore()
    with pytest.raises(StorageUnavailable):
        await store.get(PATH)


# --------------------------------------------------------------------------- #
# The Protocol
# --------------------------------------------------------------------------- #
def test_all_three_backends_satisfy_the_protocol(tmp_path: Path) -> None:
    supabase = SupabaseStore(
        client=httpx.AsyncClient(),
        base_url=BASE_URL,
        bucket=BUCKET,
        service_role_key=SERVICE_ROLE_KEY,
    )
    assert isinstance(supabase, ObjectStore)
    assert isinstance(LocalStore(root=tmp_path), ObjectStore)
    assert isinstance(MemoryStore(), ObjectStore)


# --------------------------------------------------------------------------- #
# build_store — picking a backend from Settings
# --------------------------------------------------------------------------- #
def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://gateway:gateway@127.0.0.1:5432/gateway",
        "REDIS_URL": "redis://localhost:6379/0",
        "SUPABASE_URL": BASE_URL,
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "GROQ_API_KEY": "gsk_test",
        "GEMINI_API_KEY": "gemini_test",
        "OPENROUTER_API_KEY": "sk-or-v1-test",
        "ENCRYPTION_KEY": "dGVzdC1mZXJuZXQta2V5LW5vdC1hLXJlYWwtb25lLTAwMD0=",
        "SUPABASE_SERVICE_ROLE_KEY": SERVICE_ROLE_KEY,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_build_store_picks_supabase_by_default() -> None:
    store = build_store(client=httpx.AsyncClient(), settings=_settings())
    assert isinstance(store, SupabaseStore)


def test_build_store_picks_local(tmp_path: Path) -> None:
    settings = _settings(FILES_STORAGE_BACKEND="local", FILES_LOCAL_DIR=str(tmp_path))
    store = build_store(client=httpx.AsyncClient(), settings=settings)
    assert isinstance(store, LocalStore)


def test_build_store_picks_memory() -> None:
    settings = _settings(FILES_STORAGE_BACKEND="memory")
    store = build_store(client=httpx.AsyncClient(), settings=settings)
    assert isinstance(store, MemoryStore)
