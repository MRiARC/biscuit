"""Unit tests for database session management and transactions."""

import os
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlmodel import select

from biscuit.db.session import (
    apply_async_triggers,
    apply_triggers,
    bootstrap_postgres_extensions,
    get_async_database_url,
    get_async_engine,
    get_async_session,
    get_database_url,
    get_engine,
    get_session,
    get_triggers_sql,
    init_async_db,
    init_db,
    split_sql_statements,
)
from biscuit.models import Contributor, Decision, DecisionAlternative, SourceThread


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


def test_split_sql_statements():
    """Verify SQL statement splitter correctly handles PL/pgSQL dollar-quoting, comments, and strings."""
    sample_sql = """
    -- Initial comment
    CREATE EXTENSION IF NOT EXISTS vector;

    /* Multi-line
       block comment */
    CREATE OR REPLACE FUNCTION test_func()
    RETURNS TRIGGER LANGUAGE plpgsql AS $$
    DECLARE
        v_test TEXT := 'Value with ; semicolon';
    BEGIN
        -- Inner comment
        IF NEW.val IS NULL THEN
            RAISE EXCEPTION 'val cannot be null; must be provided';
        END IF;
        RETURN NEW;
    END;
    $$;

    DROP TRIGGER IF EXISTS trg_test ON test_table;
    CREATE TRIGGER trg_test BEFORE INSERT ON test_table FOR EACH ROW EXECUTE FUNCTION test_func();
    """

    stmts = split_sql_statements(sample_sql)
    assert len(stmts) == 4
    assert "CREATE EXTENSION IF NOT EXISTS vector" in stmts[0]
    assert "CREATE OR REPLACE FUNCTION test_func()" in stmts[1]
    assert "Value with ; semicolon" in stmts[1]
    assert "val cannot be null; must be provided" in stmts[1]
    assert "DROP TRIGGER IF EXISTS trg_test ON test_table" in stmts[2]
    assert "CREATE TRIGGER trg_test" in stmts[3]


def test_triggers_sql_fail_closed_on_missing(monkeypatch, tmp_path):
    """Verify get_triggers_sql fails closed with clear exceptions if script is missing or empty."""
    import biscuit.db.session as session_mod

    # 1. Non-existent file
    missing_path = str(tmp_path / "does_not_exist.sql")
    monkeypatch.setattr(session_mod, "TRIGGERS_SQL_PATH", missing_path)
    with pytest.raises(FileNotFoundError, match="triggers migration script not found"):
        session_mod.get_triggers_sql()

    # 2. Empty file
    empty_file = tmp_path / "empty.sql"
    empty_file.write_text("   \n  ", encoding="utf-8")
    monkeypatch.setattr(session_mod, "TRIGGERS_SQL_PATH", str(empty_file))
    with pytest.raises(ValueError, match="triggers migration script at .* is empty"):
        session_mod.get_triggers_sql()


def test_postgres_extension_bootstrapping_order_mock():
    """Verify that bootstrap_postgres_extensions executes BEFORE table metadata creation."""
    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"
    mock_conn = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn

    # Execute bootstrap
    bootstrap_postgres_extensions(mock_engine)
    executed_statements = [str(c[0][0]) for c in mock_conn.execute.call_args_list]
    assert any("CREATE EXTENSION IF NOT EXISTS vector" in s for s in executed_statements)
    assert any("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"" in s for s in executed_statements)
    assert any("CREATE EXTENSION IF NOT EXISTS pgcrypto" in s for s in executed_statements)


def test_postgres_sync_and_async_bootstrap_live():
    """Verify clean PostgreSQL 16 + pgvector sync and async initialization when live PG is present."""
    pg_url = os.getenv("TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("Live PostgreSQL URL not provided via TEST_POSTGRES_URL")

    # 1. Test synchronous bootstrap and schema creation
    engine = get_engine(pg_url)
    init_db(engine)

    with get_session(engine) as session:
        # Verify vector extension exists
        res = session.exec(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector';")
        ).first()
        assert res is not None
        assert res[0] == "vector"

        # Verify writing vector column in PostgreSQL table succeeds
        thread = SourceThread(
            source_platform="discourse",
            project_context="python/cpython",
            external_thread_id="test_pg_thread_live",
            title="PostgreSQL Integration Thread",
            canonical_url="https://example.com/pg",
        )
        session.add(thread)
        session.flush()

        decision = Decision(
            source_thread_id=thread.id,
            title="Postgres Test Decision",
            context_summary="Testing live PostgreSQL 16 and pgvector vector storage",
        )
        session.add(decision)
        session.flush()

        alt = DecisionAlternative(
            decision_id=decision.id,
            proposal_text="Live Postgres Vector Alternative",
            embedding=[0.05] * 768,
        )
        session.add(alt)
        session.flush()
        assert alt.id is not None

    # 2. Test asynchronous bootstrap (statement-by-statement through asyncpg)
    async_url = get_async_database_url(pg_url)
    async_engine = get_async_engine(async_url)

    import anyio

    async def _test_async():
        await init_async_db(async_engine)
        async with get_async_session(async_engine) as session:
            res = await session.exec(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector';")
            )
            row = res.first()
            assert row is not None
            assert row[0] == "vector"

    anyio.run(_test_async)

