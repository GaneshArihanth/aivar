"""Async SQLAlchemy engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

log = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine

    url = settings.effective_database_url
    if settings.is_embedded:
        Path(settings.embedded_db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, future=True)
    else:
        _engine = create_async_engine(
            url,
            future=True,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
        )

    _sessionmaker = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )
    log.info("db.engine_initialised", url=url.split("@")[-1])
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, roll back on failure."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency.

    Note for mutating endpoints: FastAPI runs the exit half of a ``yield``
    dependency *after* the response has been sent, so a failure in the commit
    below cannot influence the status code — the client would receive a
    cheerful 2xx for a transaction that then rolled back. Endpoints that change
    state must therefore ``await session.commit()`` themselves before returning,
    so that a database error surfaces as an error. The commit here is the
    backstop for read paths and for anything the endpoint left pending.
    """
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
