"""Unit tests for database session management and transactions."""

import pytest
from sqlmodel import select

from biscuit.db.session import (
    apply_async_triggers,
    apply_triggers,
    get_async_database_url,
    get_async_engine,
    get_async_session,
    get_database_url,
    get_engine,
    get_session,
    get_triggers_sql,
    init_async_db,
    init_db,
)
from biscuit.models import Contributor, SourceThread


def test_database_url_resolution(monkeypatch):
    """Test environment variable and dialect conversion for database URLs."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert get_database_url() == "sqlite:///:memory:"

    assert get_async_database_url("sqlite:///foo.db") == "sqlite+aiosqlite:///foo.db"
    assert get_async_database_url("postgresql://user:pass@localhost/db") == "postgresql+asyncpg://user:pass@localhost/db"
    assert get_async_database_url("postgres://user:pass@localhost/db") == "postgresql+asyncpg://user:pass@localhost/db"


def test_triggers_sql_loading_and_application():
    """Verify that triggers.sql exists, is readable, and contains canonical functions."""
    sql = get_triggers_sql()
    assert len(sql) > 0
    assert "enforce_authoritative_alternative_rejection" in sql
    assert "enforce_discussion_edge_evidence" in sql
    assert "verify_source_message_version_hash" in sql

    # Test that calling apply_triggers on non-postgres (e.g. SQLite) is safely a no-op
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    apply_triggers(engine)


@pytest.mark.anyio
async def test_async_apply_triggers_sqlite_noop():
    """Verify that apply_async_triggers is a safe no-op on non-postgres."""
    aengine = get_async_engine("sqlite+aiosqlite:///:memory:")
    await init_async_db(aengine)
    await apply_async_triggers(aengine)


def test_sync_session_commit_and_query():
    """Test synchronous database session creation, write, commit, and query."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)

    with get_session(engine) as session:
        thread = SourceThread(
            source_platform="discourse",
            project_context="python/cpython",
            external_thread_id="test_sync_1",
            title="Sync Session Thread",
            canonical_url="https://example.com/sync",
        )
        session.add(thread)

    with get_session(engine) as session:
        statement = select(SourceThread).where(
            SourceThread.external_thread_id == "test_sync_1"
        )
        found = session.exec(statement).first()
        assert found is not None
        assert found.title == "Sync Session Thread"


def test_sync_session_rollback_on_exception():
    """Test synchronous session rolls back on error."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)

    with pytest.raises(RuntimeError), get_session(engine) as session:
        c = Contributor(
            source_platform="discourse",
            platform_user_id="user_rollback",
            canonical_name="Rollback User",
            author_handle="rollback_user",
        )
        session.add(c)
        raise RuntimeError("Forced error for rollback")

    with get_session(engine) as session:
        statement = select(Contributor).where(
            Contributor.platform_user_id == "user_rollback"
        )
        assert session.exec(statement).first() is None


@pytest.mark.anyio
async def test_async_session_commit_and_query():
    """Test asynchronous database session write, commit, and query."""
    engine = get_async_engine("sqlite+aiosqlite:///:memory:")
    await init_async_db(engine)

    async with get_async_session(engine) as session:
        thread = SourceThread(
            source_platform="github_pr",
            project_context="python/cpython",
            external_thread_id="test_async_1",
            title="Async Session Thread",
            canonical_url="https://example.com/async",
        )
        session.add(thread)

    async with get_async_session(engine) as session:
        statement = select(SourceThread).where(
            SourceThread.external_thread_id == "test_async_1"
        )
        result = await session.exec(statement)
        found = result.first()
        assert found is not None
        assert found.title == "Async Session Thread"


@pytest.mark.anyio
async def test_async_session_rollback_on_exception():
    """Test asynchronous session rolls back on error."""
    engine = get_async_engine("sqlite+aiosqlite:///:memory:")
    await init_async_db(engine)

    with pytest.raises(ValueError):
        async with get_async_session(engine) as session:
            c = Contributor(
                source_platform="github",
                platform_user_id="async_rollback_user",
                canonical_name="Async Rollback",
                author_handle="async_rollback",
            )
            session.add(c)
            raise ValueError("Forced async error for rollback")

    async with get_async_session(engine) as session:
        statement = select(Contributor).where(
            Contributor.platform_user_id == "async_rollback_user"
        )
        result = await session.exec(statement)
        assert result.first() is None
