"""Unit tests for Biscuit SQLModel models and database schema integrity."""

import uuid
from datetime import UTC, datetime

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
    """Create in-memory SQLite engine and yield transactional session."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
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

    version = SourceMessageVersion(
        message_id=message.id,
        version_number=1,
        raw_content=raw_text,
        content_hash="dummy_hash_64_chars_length_hex_string_0123456789abcdef0123456789abcde",
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
        content_hash="dummy_hash_evidence_quote_0123456789abcdef0123456789abcdef0123456789",
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

    request = HitlApprovalRequest(
        switch_approval_id="switch_req_123",
        action_type=HitlActionType.AUTHORIZE_PR_WARNING,
        status=HitlRequestStatus.PENDING,
        idempotency_key=str(uuid.uuid4()),
        target_pr_number=505,
        target_repository="python/cpython",
        target_commit_sha="d" * 40,
        card_rendered_payload={"text": "Authorize warning on PR #505"},
        action_payload_sha256="e" * 64,
        authorized_roles=["BDFL", "Core Maintainer"],
        callback_hmac_secret_id="secret_v1",
        expires_at=datetime.now(UTC),
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
