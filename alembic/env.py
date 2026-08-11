"""Alembic environment — async, and configured from app/config.py.

The database URL comes from ``get_settings()`` rather than ``alembic.ini`` so that
migrations and the running application can never disagree about which database
they mean, and so a missing ``DATABASE_URL`` fails here the same loud way it fails
at app startup.
"""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import context
from app.config import get_settings
from app.db.models import Base
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config

# Autogenerate compares against this. Importing app.db.models is what registers
# every table on it — a model in a module that nothing imports is invisible here.
target_metadata = Base.metadata

_CONTEXT_OPTS: dict[str, Any] = {
    "target_metadata": target_metadata,
    "compare_type": True,
    "compare_server_default": True,
    # Keeps the constraint naming convention in app/db/models.py authoritative for
    # generated migrations too.
    "include_schemas": False,
}


def _database_url() -> str:
    return get_settings().DATABASE_URL


def run_migrations_offline() -> None:
    """Render SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_CONTEXT_OPTS,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """The synchronous half, driven by ``run_sync`` on an async connection."""
    context.configure(connection=connection, **_CONTEXT_OPTS)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect with the asyncpg driver and run the migrations in one transaction."""
    engine = create_async_engine(_database_url(), poolclass=None, future=True)

    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
