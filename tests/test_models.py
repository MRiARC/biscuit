"""Unit tests for Biscuit SQLModel models and database schema integrity."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, create_engine

from biscuit.db.session import init_db
from biscuit.models import (
    CodeAnchor,
    CodeAnchorLineage,
    CodeSymbolType,
    Contributor,
    ContributorProjectRole,
    ContributorProjectRoleGrant,
    ContributorProjectRoleRevocation,
    Decision,
    DecisionAlternative,
    EvidenceRole,
    EvidenceSpan,
    EvidenceStance,
    HitlActionType,
    HitlApprovalEvent,
    HitlApprovalRequest,
    HitlRequestStatus,
    PlatformType,
    ProposalLifecycle,
    SourceMessage,
    SourceMessageVersion,
    SourceThread,
    VerificationStatus,
)


@pytest.fixture
def test_db_session():
    """Create in-memory SQLite engine and yield transactional session with FKs enabled."""
    from sqlalchemy import event

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    init_db(engine)
    with Session(engine) as session:
        yield session


def test_contributor_and_role_grants(test_db_session: Session):
    """Test contributor creation and project-scoped role grants."""
    contributor = Contributor(
        source_platform="discourse",
        platform_user_id="user_42",
        canonical_name="Guido van Rossum",
        author_handle="gvanrossum",
        email_prefix="guido",
    )
    test_db_session.add(contributor)
    test_db_session.commit()
    assert contributor.id is not None

    grant = ContributorProjectRoleGrant(
        contributor_id=contributor.id,
        project_context="python/cpython",
        role=ContributorProjectRole.BDFL,
        verified_by_id=contributor.id,
        verification_source_url="https://peps.python.org/pep-0013/",
        verified_at=datetime.now(UTC),
        valid_from=datetime.now(UTC),
    )
    test_db_session.add(grant)
    test_db_session.commit()
    assert grant.id is not None

    # Test revocation
    revocation = ContributorProjectRoleRevocation(
        grant_id=grant.id,
        revoked_by_id=contributor.id,
        reason="Stepped down to BDFL Emeritus in 2018",
    )
    test_db_session.add(revocation)
    test_db_session.commit()
    assert revocation.grant_id == grant.id


def test_thread_message_and_versions(test_db_session: Session):
    """Test thread, message, and immutable versioning models."""
    thread = SourceThread(
        source_platform=str(PlatformType.DISCOURSE),
        project_context="python/cpython",
        external_thread_id="topic_100",
        title="Discussion on None-aware operators",
        canonical_url="https://discuss.python.org/t/100",
    )
    test_db_session.add(thread)
    test_db_session.commit()

    author = Contributor(
        source_platform="discourse",
        platform_user_id="user_1",
        canonical_name="Alice",
        author_handle="alice",
    )
    test_db_session.add(author)
    test_db_session.commit()

    message = SourceMessage(
        thread_id=thread.id,
        external_message_id="post_1",
        author_id=author.id,
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://discuss.python.org/t/100/1",
    )
    test_db_session.add(message)
    test_db_session.commit()

    raw_text = "I propose introducing ?.\r\nThis avoids AttributeError."
    version1 = SourceMessageVersion(
        message_id=message.id,
        version_number=1,
        raw_content=raw_text,
        content_hash="d75608b6ff9bc8b776be2d69941a38e83f0f7f3a8b27dd338b5849be5c3b9991",
        is_current=True,
    )
    test_db_session.add(version1)
    test_db_session.commit()

    assert version1.version_number == 1
    assert version1.is_current is True
    assert version1.raw_content == raw_text  # Untouched CRLF preserved


def test_evidence_span_offsets_and_relationship(test_db_session: Session):
    """Test EvidenceSpan offset precision and zero-based slicing."""
    thread = SourceThread(
        source_platform=str(PlatformType.DISCOURSE),
        project_context="python/cpython",
        external_thread_id="topic_200",
        title="PEP 505 Evaluation",
        canonical_url="https://discuss.python.org/t/200",
    )
    test_db_session.add(thread)
    test_db_session.commit()

    message = SourceMessage(
        thread_id=thread.id,
        external_message_id="post_1",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://discuss.python.org/t/200/1",
    )
    test_db_session.add(message)
    test_db_session.commit()

    raw_text = "PEP 505 introduces '?.' and '?[' for safe navigation."
    quote = "'?.' and '?['"
    start_char = raw_text.index(quote)
    end_char = start_char + len(quote)

    import hashlib

    version = SourceMessageVersion(
        message_id=message.id,
        version_number=1,
        raw_content=raw_text,
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        is_current=True,
    )
    test_db_session.add(version)
    test_db_session.commit()

    decision = Decision(
        source_thread_id=thread.id,
        title="None-aware Safe Navigation Operators",
        context_summary="Syntax proposal for ?. and ?[",
    )
    test_db_session.add(decision)
    test_db_session.commit()

    alternative = DecisionAlternative(
        decision_id=decision.id,
        proposal_text="Syntax '?.' and '?[' for None-coalescing navigation",
        lifecycle_status=ProposalLifecycle.PROPOSED,
        embedding=[0.05] * 768,
    )
    test_db_session.add(alternative)
    test_db_session.commit()

    # Evidence span attached to alternative
    evidence = EvidenceSpan(
        source_message_version_id=version.id,
        alternative_id=alternative.id,
        exact_quote=quote,
        start_char=start_char,
        end_char=end_char,
        content_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        evidence_role=EvidenceRole.PROPOSAL,
        stance=EvidenceStance.SUPPORT,
        verification_status=VerificationStatus.UNVERIFIED,
    )
    test_db_session.add(evidence)
    test_db_session.commit()

    # Verify zero-based substring slice invariant:
    # raw_content[start_char:end_char] == exact_quote
    assert raw_text[evidence.start_char : evidence.end_char] == evidence.exact_quote


def test_code_anchor_and_lineage(test_db_session: Session):
    """Test CodeAnchor and forward DAG lineage models."""
    thread = SourceThread(
        source_platform="github_pr",
        project_context="python/cpython",
        external_thread_id="pull_505",
        title="PEP 505 PR",
        canonical_url="https://github.com/python/cpython/pull/505",
    )
    test_db_session.add(thread)
    test_db_session.commit()

    decision = Decision(
        source_thread_id=thread.id,
        title="PEP 505 PR decision",
        context_summary="PR diff evaluation",
    )
    test_db_session.add(decision)
    test_db_session.commit()

    alternative = DecisionAlternative(
        decision_id=decision.id,
        proposal_text="Add token support for None-aware operator in tokenizer.c",
    )
    test_db_session.add(alternative)
    test_db_session.commit()

    anchor = CodeAnchor(
        alternative_id=alternative.id,
        language="c",
        symbol_type=CodeSymbolType.FUNCTION,
        qualified_symbol_name="cpython.Parser.Tokenizer.tok_get",
        parser_name="tree-sitter-c",
        parser_version="0.20.0",
        grammar_version="c-1.0",
        fingerprint_algorithm="sha256",
        ast_normalization_version="v1",
        historical_file_path="Parser/tokenizer.c",
        historical_commit_sha="a" * 40,
        mapping_confidence=0.95,
    )
    test_db_session.add(anchor)
    test_db_session.commit()

    lineage = CodeAnchorLineage(
        code_anchor_id=anchor.id,
        historical_commit_sha="a" * 40,
        transition_commit_sha="b" * 40,
        compared_parent_sha="a" * 40,
        parent_index=0,
        target_commit_sha="c" * 40,
        historical_path="Parser/tokenizer.c",
        migrated_path="Parser/lexer/tokenizer.c",
        transition_type="RENAME_M",
        mapping_evidence={"method": "git_diff_M", "similarity": 0.98},
        confidence_score=0.98,
    )
    test_db_session.add(lineage)
    test_db_session.commit()

    assert lineage.id is not None
    assert lineage.confidence_score == 0.98


def test_hitl_approval_models(test_db_session: Session):
    """Test HITL approval request and event models."""
    author = Contributor(
        source_platform="discourse",
        platform_user_id="user_99",
        canonical_name="Maintainer Bob",
        author_handle="bob",
    )
    test_db_session.add(author)
    test_db_session.commit()

    # AUTHORIZE_PR_WARNING must target a real decision alternative (chk_hitl_action_target)
    thread = SourceThread(
        source_platform="github_pr",
        project_context="python/cpython",
        external_thread_id="pull_505",
        title="PEP 505 PR",
        canonical_url="https://github.com/python/cpython/pull/505",
    )
    test_db_session.add(thread)
    test_db_session.commit()

    decision = Decision(
        source_thread_id=thread.id,
        title="PEP 505 PR decision",
        context_summary="PR diff evaluation",
    )
    test_db_session.add(decision)
    test_db_session.commit()

    alternative = DecisionAlternative(
        decision_id=decision.id,
        proposal_text="Add token support for None-aware operator in tokenizer.c",
    )
    test_db_session.add(alternative)
    test_db_session.commit()

    request = HitlApprovalRequest(
        switch_approval_id="switch_req_123",
        action_type=HitlActionType.AUTHORIZE_PR_WARNING,
        status=HitlRequestStatus.PENDING,
        idempotency_key=str(uuid.uuid4()),
        target_pr_number=505,
        target_repository="python/cpython",
        target_commit_sha="d" * 39 + "e",  # 40-char hex sha
        decision_alternative_id=alternative.id,
        card_rendered_payload={"text": "Authorize warning on PR #505"},
        action_payload_sha256="e" * 63 + "f",  # 64-char hex digest
        authorized_roles=["BDFL", "Core Maintainer"],
        callback_hmac_secret_id="secret_v1",
        expires_at=datetime.now(UTC) + timedelta(hours=72),
    )
    test_db_session.add(request)
    test_db_session.commit()

    event = HitlApprovalEvent(
        approval_request_id=request.id,
        actor_source_platform="slack",
        actor_platform_id="U12345",
        actor_contributor_id=author.id,
        action_taken="AUTHORIZE_PR_COMMENT",
        payload_signature="f" * 64,
        callback_body_sha256="a" * 64,
    )
    test_db_session.add(event)
    test_db_session.commit()

    assert event.id is not None
    assert event.action_taken == "AUTHORIZE_PR_COMMENT"


def test_hitl_action_target_shape_negative(test_db_session: Session):
    """Negative: AUTHORIZE_PR_WARNING without a decision alternative target is rejected."""
    from sqlalchemy.exc import IntegrityError

    request = HitlApprovalRequest(
        switch_approval_id="switch_req_bad_target",
        action_type=HitlActionType.AUTHORIZE_PR_WARNING,
        status=HitlRequestStatus.PENDING,
        idempotency_key=str(uuid.uuid4()),
        target_pr_number=505,
        target_repository="python/cpython",
        target_commit_sha="d" * 39 + "e",
        card_rendered_payload={"text": "Bad target"},
        action_payload_sha256="e" * 63 + "f",
        authorized_roles=["BDFL"],
        callback_hmac_secret_id="secret_v1",
        expires_at=datetime.now(UTC) + timedelta(hours=72),
    )
    test_db_session.add(request)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_hitl_expiry_check_negative(test_db_session: Session):
    """Negative: expires_at earlier than created_at is rejected by chk_hitl_expiry."""
    from sqlalchemy.exc import IntegrityError

    request = HitlApprovalRequest(
        switch_approval_id="switch_req_expired",
        action_type=HitlActionType.DISMISS_PR_WARNING,
        status=HitlRequestStatus.PENDING,
        idempotency_key=str(uuid.uuid4()),
        target_pr_number=505,
        target_repository="python/cpython",
        target_commit_sha="d" * 39 + "e",
        card_rendered_payload={"text": "Expired request"},
        action_payload_sha256="e" * 63 + "f",
        authorized_roles=["BDFL"],
        callback_hmac_secret_id="secret_v1",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    test_db_session.add(request)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_proposal_occurrence_composite_fk_negative(test_db_session: Session):
    """Negative test: composite FK rejects occurrence pointing to thread A with message from thread B."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import ProposalOccurrence

    # Thread A
    thread_a = SourceThread(
        source_platform="DISCOURSE",
        project_context="python/cpython",
        external_thread_id="thread_a",
        title="Thread A",
        canonical_url="https://example.com/a",
    )
    # Thread B
    thread_b = SourceThread(
        source_platform="DISCOURSE",
        project_context="python/cpython",
        external_thread_id="thread_b",
        title="Thread B",
        canonical_url="https://example.com/b",
    )
    test_db_session.add(thread_a)
    test_db_session.add(thread_b)
    test_db_session.commit()

    # Message belongs to Thread A
    msg_a = SourceMessage(
        thread_id=thread_a.id,
        external_message_id="msg_a_1",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/a/1",
    )
    test_db_session.add(msg_a)
    test_db_session.commit()

    v_a = SourceMessageVersion(
        message_id=msg_a.id,
        version_number=1,
        raw_content="Content A",
        content_hash="0" * 64,
        is_current=True,
    )
    test_db_session.add(v_a)

    decision = Decision(
        source_thread_id=thread_a.id,
        title="Decision A",
        context_summary="Summary",
    )
    test_db_session.add(decision)
    test_db_session.commit()

    alternative = DecisionAlternative(
        decision_id=decision.id,
        proposal_text="Alt 1",
    )
    test_db_session.add(alternative)
    test_db_session.commit()

    # Try to insert ProposalOccurrence with source_thread_id = Thread B,
    # but initiating_message_id = msg_a (which belongs to Thread A).
    bad_occurrence = ProposalOccurrence(
        alternative_id=alternative.id,
        source_thread_id=thread_b.id,  # Thread B!
        initiating_message_id=msg_a.id,  # Message from Thread A!
        initiating_message_version_id=v_a.id,
        occurred_at=datetime.now(UTC),
        proposal_wording_version="wording",
        occurrence_context="context",
        occurrence_outcome="REJECTED",
    )
    test_db_session.add(bad_occurrence)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_postgres_vector_column_compilation():
    """Verify that VectorColumn compiles to VECTOR(768) on PostgreSQL dialect."""
    from sqlalchemy import Column
    from sqlalchemy.dialects import postgresql

    from biscuit.models.base import VectorColumn

    col = Column("embedding", VectorColumn(768))
    compiled = col.type.compile(dialect=postgresql.dialect())
    assert "VECTOR(768)" in str(compiled).upper()


# ─────────────────────────────────────────────────────────────────────────────
# Review follow-up: edge target shape, composite provenance FKs, authority
# back-links, and author RESTRICT behavior (canonical DDL §3, §7, §8, §12, §15).
# ─────────────────────────────────────────────────────────────────────────────


def _build_discussion_graph(session, with_author: bool = True):
    """Create thread → message → version → decision → alternative → evidence."""
    import hashlib

    author = Contributor(
        source_platform="discourse",
        platform_user_id="user_graph",
        canonical_name="Graph Author",
        author_handle="graphauthor",
    )
    session.add(author)
    session.flush()

    thread = SourceThread(
        source_platform="DISCOURSE",
        project_context="python/cpython",
        external_thread_id="graph_thread",
        title="Graph Thread",
        canonical_url="https://example.com/graph",
    )
    session.add(thread)
    session.flush()

    message = SourceMessage(
        thread_id=thread.id,
        external_message_id="graph_msg_1",
        author_id=author.id if with_author else None,
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/graph/1",
    )
    session.add(message)
    session.flush()

    raw_text = "We should reject the coalesce operator approach."
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
        title="Graph decision",
        context_summary="Summary",
    )
    session.add(decision)
    session.flush()

    alternative = DecisionAlternative(
        decision_id=decision.id,
        proposal_text="Graph alternative",
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
    session.commit()
    return {
        "author": author,
        "thread": thread,
        "message": message,
        "version": version,
        "decision": decision,
        "alternative": alternative,
        "evidence": evidence,
    }


def test_discussion_edge_target_shape_semantic_requires_evidence(test_db_session):
    """Negative: semantic edge without evidence span or version is rejected."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import DiscussionEdge, EdgeProvenance

    graph = _build_discussion_graph(test_db_session)

    edge = DiscussionEdge(
        source_thread_id=graph["thread"].id,
        from_message_id=graph["message"].id,
        to_message_id=graph["message"].id,
        target_alternative_id=graph["alternative"].id,
        target_decision_id=None,
        source_message_version_id=None,  # missing
        evidence_span_id=None,  # missing
        edge_type="SUPPORTS",
        edge_provenance=EdgeProvenance.DETERMINISTIC_RULE,
        confidence_score=0.9,
    )
    test_db_session.add(edge)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_discussion_edge_target_shape_conversational_blocks_alternative(
    test_db_session,
):
    """Negative: DIRECT_REPLY carrying an alternative target is rejected."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import DiscussionEdge, EdgeProvenance

    graph = _build_discussion_graph(test_db_session)

    edge = DiscussionEdge(
        source_thread_id=graph["thread"].id,
        from_message_id=graph["message"].id,
        to_message_id=graph["message"].id,
        target_alternative_id=graph["alternative"].id,
        edge_type="DIRECT_REPLY",
        edge_provenance=EdgeProvenance.PLATFORM_METADATA,
        confidence_score=1.0,
    )
    test_db_session.add(edge)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_discussion_edge_target_shape_references_exactly_one_target(test_db_session):
    """Negative: REFERENCES with both decision and alternative targets is rejected."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import DiscussionEdge, EdgeProvenance

    graph = _build_discussion_graph(test_db_session)

    edge = DiscussionEdge(
        source_thread_id=graph["thread"].id,
        from_message_id=graph["message"].id,
        to_message_id=graph["message"].id,
        target_decision_id=graph["decision"].id,
        target_alternative_id=graph["alternative"].id,
        edge_type="REFERENCES",
        edge_provenance=EdgeProvenance.PLATFORM_METADATA,
        confidence_score=1.0,
    )
    test_db_session.add(edge)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_discussion_edge_semantic_positive(test_db_session):
    """Positive: fully-provenanced semantic edge with matched evidence is accepted."""
    from biscuit.models import DiscussionEdge, EdgeProvenance

    graph = _build_discussion_graph(test_db_session)

    edge = DiscussionEdge(
        source_thread_id=graph["thread"].id,
        from_message_id=graph["message"].id,
        to_message_id=graph["message"].id,
        target_alternative_id=graph["alternative"].id,
        source_message_version_id=graph["version"].id,
        evidence_span_id=graph["evidence"].id,
        edge_type="RESOLVES",
        edge_provenance=EdgeProvenance.PLATFORM_METADATA,
        confidence_score=1.0,
    )
    test_db_session.add(edge)
    test_db_session.commit()
    assert edge.id is not None


def test_discussion_edge_cross_thread_composite_fk_negative(test_db_session):
    """Negative: an edge cannot link a message from another thread."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import DiscussionEdge, EdgeProvenance

    graph = _build_discussion_graph(test_db_session)

    other_thread = SourceThread(
        source_platform="DISCOURSE",
        project_context="python/cpython",
        external_thread_id="graph_thread_other",
        title="Other Thread",
        canonical_url="https://example.com/other",
    )
    test_db_session.add(other_thread)
    test_db_session.flush()

    # from_message belongs to graph thread, but source_thread_id points elsewhere
    edge = DiscussionEdge(
        source_thread_id=other_thread.id,
        from_message_id=graph["message"].id,
        to_message_id=graph["message"].id,
        edge_type="DIRECT_REPLY",
        edge_provenance=EdgeProvenance.PLATFORM_METADATA,
        confidence_score=1.0,
    )
    test_db_session.add(edge)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_discussion_edge_evidence_mismatch_composite_fk_negative(test_db_session):
    """Negative: semantic edge citing evidence bound to another alternative/version."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import DiscussionEdge, EdgeProvenance

    graph = _build_discussion_graph(test_db_session)

    # A second alternative; the evidence span belongs to the first one, so the
    # composite FK (evidence_span_id, alternative_id, version_id) must fail.
    other_alt = DecisionAlternative(
        decision_id=graph["decision"].id,
        proposal_text="Graph alternative 2",
    )
    test_db_session.add(other_alt)
    test_db_session.flush()

    edge = DiscussionEdge(
        source_thread_id=graph["thread"].id,
        from_message_id=graph["message"].id,
        to_message_id=graph["message"].id,
        target_alternative_id=other_alt.id,
        source_message_version_id=graph["version"].id,
        evidence_span_id=graph["evidence"].id,
        edge_type="SUPPORTS",
        edge_provenance=EdgeProvenance.DETERMINISTIC_RULE,
        confidence_score=0.9,
    )
    test_db_session.add(edge)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_alternative_ruling_evidence_backlink_negative(test_db_session):
    """Negative: an alternative cannot cite evidence bound to another alternative."""
    from sqlalchemy.exc import IntegrityError

    graph = _build_discussion_graph(test_db_session)

    other_alt = DecisionAlternative(
        decision_id=graph["decision"].id,
        proposal_text="Backlink alt",
    )
    test_db_session.add(other_alt)
    test_db_session.flush()

    from sqlalchemy import text

    # Exercise the physical FK rather than relying on ORM dirty tracking.
    # Raw SQLite UUID parameters must use the 32-character hexadecimal form.
    assert test_db_session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    fk_rows = test_db_session.execute(text("PRAGMA foreign_key_list(decision_alternatives)")).all()
    from sqlalchemy.schema import CreateTable

    declared = list(DecisionAlternative.__table__.foreign_key_constraints)
    compiled = str(
        CreateTable(
            DecisionAlternative.__table__,
            include_foreign_key_constraints=declared,
        ).compile(dialect=test_db_session.get_bind().dialect)
    )
    assert any(r[2] == "evidence_spans" and r[3] == "ruling_evidence_id" for r in fk_rows), (
        fk_rows,
        [(fk.name, repr(fk._create_rule), repr(fk._ddl_if), [(e.parent.table.schema, e.column.table.schema) for e in fk.elements])
         for fk in declared],
        compiled,
    )
    good = test_db_session.execute(
        text("UPDATE decision_alternatives SET ruling_evidence_id=:eid WHERE id=:aid"),
        {"eid": graph["evidence"].id.hex, "aid": graph["alternative"].id.hex},
    )
    assert good.rowcount == 1
    test_db_session.commit()

    # The same evidence belongs to alternative 1, not alternative 2.
    with pytest.raises(IntegrityError):
        test_db_session.execute(
            text("UPDATE decision_alternatives SET ruling_evidence_id=:eid WHERE id=:aid"),
            {"eid": graph["evidence"].id.hex, "aid": other_alt.id.hex},
        )
        test_db_session.commit()
    test_db_session.rollback()


def test_decision_ruling_evidence_backlink_negative(test_db_session):
    """Negative: a decision cannot cite evidence not bound to that decision."""
    from sqlalchemy.exc import IntegrityError

    graph = _build_discussion_graph(test_db_session)

    other_decision = Decision(
        source_thread_id=graph["thread"].id,
        title="Other decision",
        context_summary="Summary",
    )
    test_db_session.add(other_decision)
    test_db_session.flush()

    # The evidence targets an alternative (decision_id NULL), not this
    # decision. Use an actual SQL UPDATE and assert the intended row matched.
    from sqlalchemy import text

    assert test_db_session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    fk_rows = test_db_session.execute(text("PRAGMA foreign_key_list(decisions)")).all()
    from sqlalchemy.schema import CreateTable

    declared = list(Decision.__table__.foreign_key_constraints)
    compiled = str(
        CreateTable(
            Decision.__table__,
            include_foreign_key_constraints=declared,
        ).compile(dialect=test_db_session.get_bind().dialect)
    )
    assert any(r[2] == "evidence_spans" and r[3] == "ruling_evidence_id" for r in fk_rows), (
        fk_rows,
        [(fk.name, repr(fk._create_rule), repr(fk._ddl_if), [(e.parent.table.schema, e.column.table.schema) for e in fk.elements])
         for fk in declared],
        compiled,
    )
    with pytest.raises(IntegrityError):
        updated = test_db_session.execute(
            text("UPDATE decisions SET ruling_evidence_id=:eid WHERE id=:did"),
            {"eid": graph["evidence"].id.hex, "did": other_decision.id.hex},
        )
        assert updated.rowcount == 1
        test_db_session.commit()
    test_db_session.rollback()


def test_living_draft_author_attribution_negative(test_db_session):
    """Negative: a living draft cannot attribute another author to the initiating message."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import LivingDecisionDraft, LivingDecisionState, ProposalLifecycle

    graph = _build_discussion_graph(test_db_session)

    other_author = Contributor(
        source_platform="discourse",
        platform_user_id="user_other",
        canonical_name="Other Person",
        author_handle="otherperson",
    )
    test_db_session.add(other_author)
    test_db_session.flush()

    draft = LivingDecisionDraft(
        switch_session_id="switch_session_1",
        source_thread_id=graph["thread"].id,
        initiating_message_id=graph["message"].id,
        initiating_message_version_id=graph["version"].id,
        initiating_author_id=other_author.id,  # NOT the message author
        topic="Wrong attribution",
        draft_status=LivingDecisionState.DRAFT_PENDING_APPROVAL,
        proposed_lifecycle=ProposalLifecycle.PROPOSED,
        proposal_text="Proposal text",
    )
    test_db_session.add(draft)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_living_draft_author_attribution_positive(test_db_session):
    """Positive: draft with the true initiating author is accepted."""
    from biscuit.models import LivingDecisionDraft, LivingDecisionState, ProposalLifecycle

    graph = _build_discussion_graph(test_db_session)

    draft = LivingDecisionDraft(
        switch_session_id="switch_session_2",
        source_thread_id=graph["thread"].id,
        initiating_message_id=graph["message"].id,
        initiating_message_version_id=graph["version"].id,
        initiating_author_id=graph["author"].id,
        topic="Correct attribution",
        draft_status=LivingDecisionState.DRAFT_PENDING_APPROVAL,
        proposed_lifecycle=ProposalLifecycle.PROPOSED,
        proposal_text="Proposal text",
    )
    test_db_session.add(draft)
    test_db_session.commit()
    assert draft.id is not None


def test_occurrence_linkage_evidence_composite_fk_negative(test_db_session):
    """Negative: occurrence linkage evidence must match alternative AND version."""
    from sqlalchemy.exc import IntegrityError

    from biscuit.models import ProposalOccurrence

    graph = _build_discussion_graph(test_db_session)

    other_alt = DecisionAlternative(
        decision_id=graph["decision"].id,
        proposal_text="Occurrence alt",
    )
    test_db_session.add(other_alt)
    test_db_session.flush()

    occurrence = ProposalOccurrence(
        alternative_id=other_alt.id,  # evidence is bound to alternative 1
        source_thread_id=graph["thread"].id,
        initiating_message_id=graph["message"].id,
        initiating_message_version_id=graph["version"].id,
        occurred_at=datetime.now(UTC),
        proposal_wording_version="wording",
        occurrence_context="context",
        occurrence_outcome="REJECTED",
        linkage_evidence_span_id=graph["evidence"].id,
    )
    test_db_session.add(occurrence)
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_source_message_author_delete_restrict(test_db_session):
    """Deleting a contributor referenced by a message is rejected (RESTRICT)."""
    from sqlalchemy.exc import IntegrityError

    graph = _build_discussion_graph(test_db_session)

    test_db_session.delete(graph["author"])
    with pytest.raises(IntegrityError):
        test_db_session.commit()
    test_db_session.rollback()


def test_message_version_hash_length_and_enum_checks(test_db_session):
    """Negative: bad direct SQL values (short hash, invalid edit state) rejected.

    Uses raw SQL to exercise the DB-level CHECK constraints rather than the
    pydantic model validation layer.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    graph = _build_discussion_graph(test_db_session)

    with pytest.raises(IntegrityError):
        test_db_session.execute(
            text(
                "INSERT INTO source_message_versions "
                "(id, message_id, version_number, raw_content, content_hash, "
                "captured_at, is_current, edit_state) VALUES "
                "(lower(hex(randomblob(16))), :mid, 2, 'short', 'notahexhash', "
                "CURRENT_TIMESTAMP, 0, 'ORIGINAL')"
            ),
            {"mid": str(graph["message"].id)},
        )
        test_db_session.commit()
    test_db_session.rollback()

    with pytest.raises(IntegrityError):
        test_db_session.execute(
            text(
                "INSERT INTO source_message_versions "
                "(id, message_id, version_number, raw_content, content_hash, "
                "captured_at, is_current, edit_state) VALUES "
                "(lower(hex(randomblob(16))), :mid, 3, 'content', :h, "
                "CURRENT_TIMESTAMP, 0, 'NOT_A_STATE')"
            ),
            {"mid": str(graph["message"].id), "h": "a" * 64},
        )
        test_db_session.commit()
    test_db_session.rollback()


def test_evidence_span_enum_and_hash_checks(test_db_session):
    """Negative: bad direct SQL values (role, verification status, hash) rejected.

    Uses raw SQL to exercise the DB-level CHECK constraints rather than the
    pydantic model validation layer.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    graph = _build_discussion_graph(test_db_session)
    insert_sql = (
        "INSERT INTO evidence_spans "
        "(id, source_message_version_id, alternative_id, exact_quote, "
        "start_char, end_char, content_hash, evidence_role, stance, "
        "verification_status, created_at) VALUES "
        "(lower(hex(randomblob(16))), :vid, :aid, 'q', 0, 1, :h, :role, 'NEUTRAL', "
        ":status, CURRENT_TIMESTAMP)"
    )
    base = {
        "vid": str(graph["version"].id),
        "aid": str(graph["alternative"].id),
        "h": "b" * 64,
        "role": "PROPOSAL",
        "status": "UNVERIFIED",
    }

    for field, value in (
        ("role", "NOT_A_ROLE"),
        ("status", "MAYBE"),
        ("h", "zzz"),
    ):
        params = dict(base)
        params[field] = value
        with pytest.raises(IntegrityError):
            test_db_session.execute(text(insert_sql), params)
            test_db_session.commit()
        test_db_session.rollback()
