"""The Redis connection pool, its readiness probe, and Lua script loading.

Builders, not module-level globals — the same rule ``app/db/session.py`` follows.
The lifespan owns the one client the process has; nothing here is a singleton, so
a test can hand the app a ``fakeredis`` instance and reach every code path.

**Created eagerly, connects lazily.** ``from_url`` opens no socket. A slow or
unreachable Upstash must not stop the process from starting and serving
``/healthz`` — the same reasoning that makes the SQLAlchemy engine lazy.

**Nothing here swallows an error except :func:`probe`.** Redis-down behaviour is
asymmetric by design (ADR-010): the breaker fails *open*, quota will fail
*closed*, and only the caller knows which it is. A wrapper that turned every
``ConnectionError`` into ``None`` would make that decision for all of them, once,
invisibly. :func:`probe` is the exception because "is Redis reachable?" is a
question whose answer is a boolean, not a failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger("app.cache.client")

LUA_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "quota" / "scripts"
"""Where checked-in Lua lives: ``reserve``, ``commit`` and ``release`` (Phase 3)."""

CONNECT_TIMEOUT_S = 5.0
"""Bound on opening a connection. Upstash is a network hop, not a local socket."""

SOCKET_TIMEOUT_S = 5.0
"""Bound on a single command. Every Redis call in this system is O(1) work on a
handful of small keys — if one takes five seconds, the server is not answering,
and waiting longer only converts a fast degraded path into a slow one."""

HEALTH_CHECK_INTERVAL_S = 30
"""How often an idle pooled connection is pinged before being handed out.
Upstash and every cloud NAT in front of it drop idle TCP connections silently;
without this the first command after a quiet minute fails instead of the
connection being replaced."""


def create_redis_client(settings: Settings) -> Redis:
    """The process-wide client. One per process, created in the lifespan.

    ``decode_responses=True`` because every value this system stores is text — a
    counter, a state name, a request id, a JSON blob. Decoding at the edge means
    no call site has to remember to ``.decode()``, and forgetting once produces a
    ``b"open" != "open"`` comparison that is false in a way that reads as true.
    """
    return Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_S,
        socket_timeout=SOCKET_TIMEOUT_S,
        health_check_interval=HEALTH_CHECK_INTERVAL_S,
    )


def redacted_target(url: str) -> str:
    """``host:port/db`` — what is safe to put in a log line.

    ``REDIS_URL`` carries a password in every deployment that is not a laptop.
    Logging the URL itself would write that password into structured logs, log
    aggregation, and anywhere those get shipped; §9.8's "never logged in
    plaintext" is about provider keys but the reasoning does not stop there.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable>"

    host = parts.hostname or "<unknown>"
    port = parts.port
    target = f"{host}:{port}" if port else host
    database = parts.path.lstrip("/")
    return f"{target}/{database}" if database else target


async def probe(client: Redis, *, timeout_s: float) -> bool:
    """Is Redis answering? Bounded, and never raises.

    The only place in the codebase that turns a Redis failure into a boolean.
    ``/readyz`` calls it, reports the answer, and stays 200 either way — an
    instance with a dead Redis serves every request correctly, just without the
    optimization the breaker provides (ADR-010).

    The bound is separate from, and shorter than, the database's: a hung Redis
    must not spend the probe's budget on a dependency that cannot fail it.
    """
    try:
        async with asyncio.timeout(timeout_s):
            await client.ping()
    except Exception as exc:
        # Warning, not debug. A permanently unreachable Redis is invisible
        # otherwise — every request still succeeds, just more slowly and with a
        # breaker that has forgotten everything it knew.
        logger.warning("redis.unreachable", error=str(exc), error_type=type(exc).__name__)
        return False
    return True


class LuaScriptRegistry:
    """The checked-in Lua scripts, registered against one client.

    Redis executes a script by SHA — ``EVALSHA`` — and answers ``NOSCRIPT`` if it
    has never seen that SHA, which happens after a restart, a ``SCRIPT FLUSH``,
    or a failover to a replica that was never sent the script. The recovery is
    always the same: send the source, cache the new SHA, retry.

    redis-py's :class:`AsyncScript` already implements exactly that loop, so this
    class does not reimplement it. What it adds is the part redis-py has no
    opinion about: discovering the ``.lua`` files, naming them, and loading them
    at startup so the first real request does not pay for the round trip and a
    syntax error surfaces at boot rather than under load.

    Phase 3's three scripts were the first to land in the directory, and needed
    no change here to be picked up: ``reserve.lua`` — a single atomic
    check-and-increment, because a check-then-increment across two round trips
    overshoots under concurrency — plus ``commit.lua`` and ``release.lua``.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client
        self._scripts: dict[str, AsyncScript] = {}

    def load_dir(self, directory: Path = LUA_SCRIPT_DIR) -> None:
        """Register every ``*.lua`` in *directory*, named by filename stem.

        A missing directory is not an error — the scripts are an implementation
        detail of a phase that may not have arrived yet. An *unreadable* one is,
        because that is a packaging mistake that would otherwise surface as a
        quota tracker that silently never reserves anything.
        """
        if not directory.is_dir():
            logger.debug("redis.scripts.no_directory", directory=str(directory))
            return

        for path in sorted(directory.glob("*.lua")):
            self._scripts[path.stem] = self._client.register_script(
                path.read_text(encoding="utf-8")
            )

    async def warm(self) -> None:
        """``SCRIPT LOAD`` every registered script. Never fatal.

        Startup does not fail on this. Redis is a fail-open dependency (ADR-010)
        and every script is loadable on demand by the ``NOSCRIPT`` path anyway —
        warming is an optimization plus an early syntax check, and neither is
        worth refusing to boot over.
        """
        if not self._scripts:
            logger.debug("redis.scripts.none_registered")
            return

        try:
            for name, script in self._scripts.items():
                # AsyncScript computes the SHA locally at registration, so this
                # round trip is not about learning it — it is about the server
                # having the source *before* the first EVALSHA, and about a
                # malformed script failing here instead of inside a request.
                sha = await self._client.script_load(script.script)
                logger.debug("redis.scripts.loaded", script=name, sha=sha)
        except Exception as exc:
            logger.warning(
                "redis.scripts.warm_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                registered=sorted(self._scripts),
            )
            return

        logger.info("redis.scripts.warmed", scripts=sorted(self._scripts))

    def __getitem__(self, name: str) -> AsyncScript:
        """The script by name. ``KeyError`` if it was never registered — a typo
        here is a bug, not a condition to degrade around."""
        return self._scripts[name]

    def __contains__(self, name: str) -> bool:
        return name in self._scripts

    def __len__(self) -> int:
        return len(self._scripts)
