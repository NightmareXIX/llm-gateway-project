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
from app.db.session import driver_connect_args
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config

# Autogenerate compares against this. Importing app.db.models is what registers
# every table on it — a model in a module that nothing imports is invisible here.
target_metadata = Base.metadata

# This module runs *before* uvicorn binds a port (see start.sh), so anything
# that blocks here is invisible: the platform sees a container that never
# opened a socket and kills it on a port scan, with no log line to say why. A
# migration that never returns and a process that never started look identical
# from the outside. Both timeouts below exist to turn that silence into a
# non-zero exit with a traceback.
#
# asyncpg already defaults ``timeout`` to 60s, so the connect is bounded with
# or without this; ten seconds is a tightening, not a fix — a healthy connect
# from any region is well under a second, and waiting a full minute to learn
# the database is unreachable wastes a deploy.
CONNECT_TIMEOUT_S = 10.0

# ``command_timeout`` is the one that matters, because asyncpg's connect
# timeout covers *connecting* and nothing after it. A pooler that accepts the
# connection and then queues the statement — which is what Supabase's does
# under saturation, since Supavisor queues per transaction rather than per
# connection — leaves a query waiting with no bound at all.
#
# Five minutes is deliberately generous. It has to clear the slowest migration
# this repo could ever run (today's are trivial, but an index build on a large
# table is the thing that would legitimately take minutes) while still landing
# well inside a deploy timeout. The cost is real and worth naming: a migration
# that genuinely needs longer than this will be killed. It is killed *inside*
# ``context.begin_transaction()``, so it rolls back rather than leaving the
# schema half-applied — and the fix is to raise this value deliberately, which
# is a better failure than a deploy that hangs until a platform kills it.
COMMAND_TIMEOUT_S = 300.0

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
    settings = get_settings()

    # ``connect_args`` is shared with the application's own engine rather than
    # re-derived, so a migration and the process that follows it can never
    # disagree about how to talk to the database. Getting this wrong is not
    # hypothetical: ``DB_DISABLE_PREPARED_STATEMENTS`` is mandatory behind a
    # transaction pooler, and an engine built without it fails with
    # ``DuplicatePreparedStatementError`` under any concurrency at all.
    connect_args = dict(driver_connect_args(settings))
    connect_args.setdefault("timeout", CONNECT_TIMEOUT_S)
    connect_args.setdefault("command_timeout", COMMAND_TIMEOUT_S)

    engine = create_async_engine(
        _database_url(),
        poolclass=None,
        future=True,
        connect_args=connect_args,
    )

    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
