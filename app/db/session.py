"""Async engine and session factory.

Builders, not module-level globals. The application creates exactly one engine in
the lifespan and hangs it off ``app.state``; tests build a throwaway one pointed at
a test database. A module-level singleton would make that second case awkward and
would tie engine creation to import time, which is precisely when configuration
errors are hardest to read.

Transaction boundaries are *not* set here. ``app/deps.py::get_session`` hands the
caller a session and closes it; committing is the repository's or the endpoint's
decision, because only they know what constitutes one unit of work.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def driver_connect_args(settings: Settings) -> dict[str, object]:
    """Driver-level arguments for asyncpg, derived from configuration.

    Split out from :func:`create_db_engine` because it is the one part of engine
    construction with a decision in it, and SQLAlchemy gives no way to read
    ``connect_args`` back off a built engine — it is merged into a pool-creator
    closure. A test can assert on this; it cannot assert on the engine.
    """
    if settings.DB_DISABLE_PREPARED_STATEMENTS:
        return {"statement_cache_size": 0}
    return {}


def create_db_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for this process.

    ``pool_pre_ping`` is not optional on free-tier Postgres: the provider drops
    idle connections, and without it the first request after a quiet spell fails
    on a dead socket instead of transparently reconnecting.

    ``statement_cache_size=0`` is applied only when
    :attr:`~app.config.Settings.DB_DISABLE_PREPARED_STATEMENTS` says to — that
    setting's docstring has the reasoning. It is off locally, where the connection
    is direct, and on behind Supabase's transaction pooler.
    """
    return create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        connect_args=driver_connect_args(settings),
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to ``engine``.

    ``expire_on_commit=False`` so an ORM object stays readable after the commit
    that persisted it — otherwise every attribute access after a commit fires a
    lazy refresh, which in async code is an error rather than a slow query.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
