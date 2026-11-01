"""Unit tests for database session management and transactions."""

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
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



def test_sqlite_fk_enforced_via_get_engine():
    """FK constraints are enforced on connections from get_engine directly."""
    from biscuit.models import SourceMessage

    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    with get_session(engine) as session:
        bad = SourceMessage(
            thread_id="00000000-0000-0000-0000-000000000001",
            external_message_id="orphan_1",
            original_timestamp=datetime.now(UTC),
            canonical_permalink="https://example.com/orphan",
        )
        session.add(bad)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.anyio
async def test_sqlite_fk_enforced_via_get_async_engine():
    """FK constraints are enforced on connections from get_async_engine directly."""
    from biscuit.models import SourceMessage

    aengine = get_async_engine("sqlite+aiosqlite:///:memory:")
    await init_async_db(aengine)
    async with get_async_session(aengine) as session:
        bad = SourceMessage(
            thread_id="00000000-0000-0000-0000-000000000002",
            external_message_id="orphan_async_1",
            original_timestamp=datetime.now(UTC),
            canonical_permalink="https://example.com/orphan-async",
        )
        session.add(bad)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


def test_biscuit_test_pg_dsn_alias(monkeypatch):
    """BISCUIT_TEST_PG_DSN (Issue #17) is accepted as the database URL source."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BISCUIT_TEST_PG_DSN", "postgresql://u:p@localhost:5432/biscuit")
    assert get_database_url() == "postgresql://u:p@localhost:5432/biscuit"


def test_get_triggers_sql_contains_lifecycle_audit_machinery():
    """The trigger script contains the atomic lifecycle audit DDL from spec §15."""
    sql = get_triggers_sql()
    assert "record_alternative_status_change" in sql
    assert "trg_alternative_status_audit" in sql
    assert "transition_alternative_lifecycle" in sql
    assert "chk_edge_target_shape" not in sql  # CHECK lives in the ORM schema
    assert "idx_alternatives_embedding" in sql  # canonical HNSW index
    assert "USING hnsw (embedding vector_cosine_ops)" in sql


# ─────────────────────────────────────────────────────────────────────────────
# Live PostgreSQL 16 + pgvector negative enforcement suite (env-gated).
# Skipped unless TEST_POSTGRES_URL (CI) or BISCUIT_TEST_PG_DSN (Issue #17) is set.
# ─────────────────────────────────────────────────────────────────────────────


def _pg_url():
    url = os.getenv("TEST_POSTGRES_URL") or os.getenv("BISCUIT_TEST_PG_DSN")
    if not url:
        pytest.skip("Live PostgreSQL URL not provided (TEST_POSTGRES_URL / BISCUIT_TEST_PG_DSN)")
    return url


def _seed_verified_ruling_graph(session):
    """Seed contributor → grant → thread → message → version → decision →
    alternative → VERIFIED_EXACT RULING evidence bound to that alternative."""
    import hashlib
    from datetime import UTC, datetime, timedelta

    from biscuit.models import (
        Contributor,
        ContributorProjectRole,
        ContributorProjectRoleGrant,
        Decision,
        DecisionAlternative,
        EvidenceRole,
        EvidenceSpan,
        EvidenceStance,
        ProposalLifecycle,
        SourceMessage,
        SourceMessageVersion,
        SourceThread,
        VerificationStatus,
    )

    bdfl = Contributor(
        source_platform="discourse",
        platform_user_id="pg_bdfl",
        canonical_name="PG BDFL",
        author_handle="pg_bdfl",
    )
    session.add(bdfl)
    session.flush()

    grant = ContributorProjectRoleGrant(
        contributor_id=bdfl.id,
        project_context="python/cpython",
        role=ContributorProjectRole.BDFL,
        verified_by_id=bdfl.id,
        verification_source_url="https://example.org/bdfl-proof",
        verified_at=datetime.now(UTC) - timedelta(days=30),
        valid_from=datetime.now(UTC) - timedelta(days=30),
    )
    session.add(grant)
    session.flush()

    thread = SourceThread(
        source_platform="DISCOURSE",
        project_context="python/cpython",
        external_thread_id=f"pg_triggers_{bdfl.id}",
        title="PG Trigger Thread",
        canonical_url="https://example.com/pg-triggers",
    )
    session.add(thread)
    session.flush()

    message = SourceMessage(
        thread_id=thread.id,
        external_message_id="pg_msg_1",
        author_id=bdfl.id,
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/pg-triggers/1",
    )
    session.add(message)
    session.flush()

    raw_text = "I reject the coalesce operator proposal for performance reasons."
    version = SourceMessageVersion(
        message_id=message.id,
        version_number=1,
        raw_content=raw_text,
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        is_current=True,
    )
    session.add(version)
    session.flush()

    decision = Decision(
        source_thread_id=thread.id,
        title="PG trigger decision",
        context_summary="Summary",
    )
    session.add(decision)
    session.flush()

    # Insert as PROPOSED (the audit trigger's allowed initial state), then
    # move to UNDER_DISCUSSION through the audited transition entrypoint.
    alternative = DecisionAlternative(
        decision_id=decision.id,
        proposal_text="PG trigger alternative",
        lifecycle_status=ProposalLifecycle.PROPOSED,
    )
    session.add(alternative)
    session.flush()

    quote = raw_text
    evidence = EvidenceSpan(
        source_message_version_id=version.id,
        alternative_id=alternative.id,
        exact_quote=quote,
        start_char=0,
        end_char=len(quote),
        content_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        evidence_role=EvidenceRole.RULING,
        stance=EvidenceStance.OBJECT,
        verification_status=VerificationStatus.VERIFIED_EXACT,
    )
    session.add(evidence)
    session.flush()

    session.execute(
        text(
            "SELECT transition_alternative_lifecycle(:alt, 'UNDER_DISCUSSION', "
            ":actor, :reason)"
        ),
        {"alt": str(alternative.id), "actor": str(bdfl.id), "reason": "seed"},
    )
    session.commit()
    return {
        "bdfl": bdfl,
        "grant": grant,
        "thread": thread,
        "message": message,
        "version": version,
        "decision": decision,
        "alternative": alternative,
        "evidence": evidence,
    }


def test_postgres_negative_trigger_enforcement_live():
    """Live PG: triggers reject forbidden writes and audits lifecycle changes."""

    from biscuit.models import (
        DecisionEventLog,
        RulingAuthority,
    )

    engine = get_engine(_pg_url())
    init_db(engine)  # idempotent bootstrap: extensions → tables → triggers → HNSW

    with get_session(engine) as session:
        graph = _seed_verified_ruling_graph(session)

        # 1. Hash forgery: raw_content with a wrong digest is rejected.
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "INSERT INTO source_message_versions "
                    "(id, message_id, version_number, raw_content, content_hash, "
                    "captured_at, is_current, edit_state) VALUES "
                    "(:id, :mid, 2, 'tampered', 'bad', NOW(), false, 'ORIGINAL')"
                ),
                {"id": str(uuid.uuid4()), "mid": str(graph["message"].id)},
            )
            session.commit()
        session.rollback()

        # 2. Version immutability: UPDATE of raw_content is rejected.
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE source_message_versions SET raw_content = 'rewritten' "
                    "WHERE id = :vid"
                ),
                {"vid": str(graph["version"].id)},
            )
            session.commit()
        session.rollback()

        # 3. Non-exact VERIFIED_EXACT: wrong quote is rejected.
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "INSERT INTO evidence_spans "
                    "(id, source_message_version_id, alternative_id, exact_quote, "
                    "start_char, end_char, content_hash, evidence_role, stance, "
                    "verification_status, created_at) VALUES "
                    "(:id, :vid, :aid, 'not the quote', 0, 13, :h, 'RULING', "
                    "'OBJECT', 'VERIFIED_EXACT', NOW())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "vid": str(graph["version"].id),
                    "aid": str(graph["alternative"].id),
                    "h": "c" * 64,
                },
            )
            session.commit()
        session.rollback()

        # 4. Unauthorized REJECTED ruling: model-inferred authority is rejected.
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE decision_alternatives SET lifecycle_status = 'REJECTED', "
                    "ruling_evidence_id = :eid, ruling_authority = 'MODEL_INFERRED', "
                    "ruling_actor_id = :aid, ruling_actor_grant_id = :gid "
                    "WHERE id = :alt"
                ),
                {
                    "eid": str(graph["evidence"].id),
                    "aid": str(graph["bdfl"].id),
                    "gid": str(graph["grant"].id),
                    "alt": str(graph["alternative"].id),
                },
            )
            session.commit()
        session.rollback()

        # 4b. Ruling/evidence composite back-links (canonical DDL §7): citing
        # another alternative's verified evidence from a second alternative is
        # rejected. These FKs are ALTER TABLE constraints, so only PostgreSQL
        # enforces them.
        other_alt_id = str(uuid.uuid4())
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "INSERT INTO decision_alternatives "
                    "(id, decision_id, proposal_text, lifecycle_status, created_at) "
                    "VALUES (:id, :did, 'Backlink alt', 'PROPOSED', NOW())"
                ),
                {"id": other_alt_id, "did": str(graph["decision"].id)},
            )
            session.execute(
                text(
                    "UPDATE decision_alternatives SET ruling_evidence_id = :eid "
                    "WHERE id = :alt"
                ),
                {"eid": str(graph["evidence"].id), "alt": other_alt_id},
            )
            session.commit()
        session.rollback()

        other_decision_id = str(uuid.uuid4())
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "INSERT INTO decisions "
                    "(id, source_thread_id, title, context_summary, "
                    "resolution_status, created_at, updated_at) "
                    "VALUES (:id, :tid, 'Other decision', 'Summary', 'UNRESOLVED', "
                    "NOW(), NOW())"
                ),
                {"id": other_decision_id, "tid": str(graph["thread"].id)},
            )
            session.execute(
                text(
                    "UPDATE decisions SET ruling_evidence_id = :eid "
                    "WHERE id = :did"
                ),
                {"eid": str(graph["evidence"].id), "did": other_decision_id},
            )
            session.commit()
        session.rollback()

        # 5. Audit immutability: UPDATE of append-only audit rows is rejected.
        # The seed's audited transition already produced the STATUS_CHANGE row.
        seeded_log = session.exec(
            select(DecisionEventLog).where(
                DecisionEventLog.alternative_id == graph["alternative"].id
            )
        ).first()
        assert seeded_log is not None
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE decision_event_log SET reason = 'tampered' WHERE id = :lid"),
                {"lid": str(seeded_log.id)},
            )
            session.commit()
        session.rollback()

        # 6. Direct lifecycle UPDATE without actor/reason GUCs is rejected.
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE decision_alternatives SET lifecycle_status = 'ACCEPTED' "
                    "WHERE id = :alt"
                ),
                {"alt": str(graph["alternative"].id)},
            )
            session.commit()
        session.rollback()

        # 7. Positive path: the audited transition entrypoint performs the
        # REJECTED ruling atomically and records the audit event.
        session.execute(
            text("SELECT transition_alternative_lifecycle(:alt, 'REJECTED', :actor, "
                 ":reason, :eid, :auth, :actor, :grant)"),
            {
                "alt": str(graph["alternative"].id),
                "actor": str(graph["bdfl"].id),
                "reason": "BDFL rejects for performance",
                "eid": str(graph["evidence"].id),
                "auth": RulingAuthority.BDFL.value,
                "grant": str(graph["grant"].id),
            },
        )
        session.commit()

        events = session.exec(
            select(DecisionEventLog).where(
                DecisionEventLog.alternative_id == graph["alternative"].id,
                DecisionEventLog.event_type == "STATUS_CHANGE",
                DecisionEventLog.new_status == "REJECTED",
            )
        ).all()
        assert len(events) >= 1
        audited = events[-1]
        assert audited.previous_status == "UNDER_DISCUSSION"
        assert audited.actor_contributor_id == graph["bdfl"].id
        assert "ruling_actor_id" in audited.authority_snapshot

        # 8. Repeat transition to the same status fails (idempotence guard).
        with pytest.raises(DBAPIError):
            session.execute(
                text("SELECT transition_alternative_lifecycle(:alt, 'REJECTED', "
                     ":actor, :reason)"),
                {
                    "alt": str(graph["alternative"].id),
                    "actor": str(graph["bdfl"].id),
                    "reason": "duplicate",
                },
            )
            session.commit()
        session.rollback()

        # 9. Rollback semantics: a failing transition leaves no audit row.
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE decision_alternatives SET lifecycle_status = 'ACCEPTED' "
                    "WHERE id = :alt"
                ),
                {"alt": str(graph["alternative"].id)},
            )
            session.commit()
        session.rollback()

        accepted_events = session.exec(
            select(DecisionEventLog).where(
                DecisionEventLog.alternative_id == graph["alternative"].id,
                DecisionEventLog.new_status == "ACCEPTED",
            )
        ).all()
        assert accepted_events == []


def test_postgres_lifecycle_transition_privileges_live():
    """Untrusted DB roles must not execute the SECURITY DEFINER transition function."""
    engine = get_engine(_pg_url())
    init_db(engine)

    signature = (
        "public.transition_alternative_lifecycle("
        "uuid,text,uuid,text,uuid,text,uuid,uuid)"
    )
    with get_session(engine) as session:
        executor_allowed = session.execute(
            text("SELECT has_function_privilege('biscuit_transition_executor', :fn, 'EXECUTE')"),
            {"fn": signature},
        ).scalar_one()
        untrusted_allowed = session.execute(
            text("SELECT has_function_privilege('pg_monitor', :fn, 'EXECUTE')"),
            {"fn": signature},
        ).scalar_one()
        assert executor_allowed is True
        assert untrusted_allowed is False

        # PostgreSQL ships pg_monitor as an unprivileged role. Attempt a
        # real unauthorized call, including a forged actor UUID.
        session.execute(text("SET LOCAL ROLE pg_monitor"))
        with pytest.raises(DBAPIError, match="permission denied"):
            session.execute(
                text(
                    "SELECT public.transition_alternative_lifecycle("
                    ":alt, 'ACCEPTED', :actor, :reason)"
                ),
                {
                    "alt": str(uuid.uuid4()),
                    "actor": str(uuid.uuid4()),
                    "reason": "forged actor",
                },
            )
        session.rollback()

    with get_session(engine) as session:
        session.execute(text("SET LOCAL ROLE pg_monitor"))
        with pytest.raises(DBAPIError, match="permission denied"):
            session.execute(
                text(
                    "UPDATE decision_alternatives "
                    "SET lifecycle_status = 'ACCEPTED' WHERE id = :alt"
                ),
                {"alt": str(uuid.uuid4())},
            )
        session.rollback()


@pytest.mark.anyio
async def test_postgres_async_negative_trigger_enforcement_live():
    """Live PG: async engine rejects the same forbidden writes (asyncpg path)."""
    from biscuit.models import SourceMessage

    url = _pg_url()
    aengine = get_async_engine(get_async_database_url(url))
    await init_async_db(aengine)

    async with get_async_session(aengine) as session:
        thread = SourceThread(
            source_platform="DISCOURSE",
            project_context="python/cpython",
            external_thread_id=f"pg_async_{uuid.uuid4()}",
            title="PG Async Thread",
            canonical_url="https://example.com/pg-async",
        )
        session.add(thread)
        await session.flush()

        with pytest.raises(IntegrityError):
            bad = SourceMessage(
                thread_id=thread.id,
                external_message_id="orphan_async",
                original_timestamp=datetime.now(UTC),
                canonical_permalink="https://example.com/x",
            )
            bad.thread_id = "00000000-0000-0000-0000-000000000003"
            session.add(bad)
            await session.commit()
        await session.rollback()


def test_sqlite_fk_emission_survives_postgres_bootstrap():
    """SQLite schema bootstrap emits all FKs even after a PostgreSQL create_all.

    Regression test: metadata.create_all() on the PostgreSQL dialect marks
    deferred FK constraints with a disable rule that suppresses their inline
    CREATE TABLE emission permanently. On CI the live-PG tests run before the
    SQLite test session, so the SQLite bootstrap must explicitly reset that
    marker or every FK (including the cyclic §7 ruling back-links) silently
    disappears from the SQLite schema. A mock PG engine reproduces the
    poisoning deterministically without a live database.
    """
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    import biscuit.models  # noqa: F401

    def _noop(sql, *args, **kwargs):
        return None

    pg_engine = create_engine("postgresql://", strategy="mock", executor=_noop)
    SQLModel.metadata.create_all(pg_engine, checkfirst=False)

    engine = get_engine("sqlite:///:memory:")
    init_db(engine, apply_postgres_triggers=False)

    with engine.connect() as conn:
        alt_fks = conn.execute(
            text("PRAGMA foreign_key_list(decision_alternatives)")
        ).all()
        decision_fks = conn.execute(
            text("PRAGMA foreign_key_list(decisions)")
        ).all()

    alt_columns = {row[3] for row in alt_fks}
    assert "ruling_evidence_id" in alt_columns
    assert "decision_id" in alt_columns
    decision_columns = {row[3] for row in decision_fks}
    assert "ruling_evidence_id" in decision_columns
    assert "source_thread_id" in decision_columns
