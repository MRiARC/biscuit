"""Database engine, connection pooling, and session management."""

import os
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

DEFAULT_SQLITE_URL = "sqlite:///:memory:"
DEFAULT_ASYNC_SQLITE_URL = "sqlite+aiosqlite:///:memory:"


def get_database_url() -> str:
    """Retrieve database URL from environment or return default SQLite memory URL."""
    return os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL)


def get_engine(db_url: str | None = None, echo: bool = False):
    """Create a synchronous SQLAlchemy / SQLModel engine."""
    url = db_url or get_database_url()
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(url, echo=echo, connect_args=connect_args)


def get_async_database_url(db_url: str | None = None) -> str:
    """Resolve an asynchronous database URL from a sync or raw URL."""
    url = db_url or get_database_url()
    if url.startswith("sqlite:///"):
        # Convert sqlite:/// to sqlite+aiosqlite:///
        return url.replace("sqlite:///", "sqlite+aiosqlite:///")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://")
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://")
    return url


def get_async_engine(db_url: str | None = None, echo: bool = False) -> AsyncEngine:
    """Create an asynchronous SQLAlchemy engine."""
    async_url = get_async_database_url(db_url)
    connect_args = {}
    if "sqlite" in async_url:
        connect_args["check_same_thread"] = False
    return create_async_engine(async_url, echo=echo, connect_args=connect_args)


def init_db(engine=None) -> None:
    """Create all SQLModel tables in database."""
    # Ensure all models are imported before creating tables
    import biscuit.models  # noqa: F401

    target_engine = engine or get_engine()
    SQLModel.metadata.create_all(target_engine)


async def init_async_db(async_engine: AsyncEngine | None = None) -> None:
    """Asynchronously create all SQLModel tables in database."""
    import biscuit.models  # noqa: F401

    target_engine = async_engine or get_async_engine()
    async with target_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


@contextmanager
def get_session(engine=None) -> Generator[Session, None, None]:
    """Provide a transactional synchronous database session."""
    target_engine = engine or get_engine()
    with Session(target_engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


@asynccontextmanager
async def get_async_session(
    async_engine: AsyncEngine | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional asynchronous database session."""
    target_engine = async_engine or get_async_engine()
    async_session_factory = sessionmaker(
        target_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
