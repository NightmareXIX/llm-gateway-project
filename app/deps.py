"""Shared FastAPI dependencies.

Resources are created once in the lifespan and read from ``request.app.state``
here. Nothing in this module constructs an engine, a client, or a pool — that
would give every test its own hidden connection pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.providers.registry import ProviderRegistry


def get_engine(request: Request) -> AsyncEngine:
    """The process-wide engine. Used by ``/readyz``; endpoints want a session."""
    engine: AsyncEngine = request.app.state.db_engine
    return engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed when the request ends.

    Deliberately does not commit. The endpoint or repository owns the transaction
    boundary; an auto-commit here would turn a half-finished handler into a
    half-written conversation.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.db_session_factory
    async with factory() as session:
        yield session


def get_http_client(request: Request) -> httpx.AsyncClient:
    """The process-wide outbound client.

    Endpoints rarely want this — provider adapters already hold it, bound at
    registry-build time. It is exposed for the odd caller that needs to make a
    one-off outbound call without inventing a second connection pool.
    """
    client: httpx.AsyncClient = request.app.state.http_client
    return client


def get_registry(request: Request) -> ProviderRegistry:
    """The slot table, built once in the lifespan.

    Built rather than looked up per request, so a config problem cannot appear
    mid-traffic: by the time an endpoint can call this, the registry is known
    good.
    """
    registry: ProviderRegistry = request.app.state.provider_registry
    return registry


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
