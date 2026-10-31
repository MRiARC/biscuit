"""
Exhaustive unit test suite for Evidence Provenance and Verification.
Verifies Unicode codepoint indexing, SHA-256 integrity, zero fuzzy matching,
and state transitions.
"""

from uuid import uuid4

from biscuit.provenance import (
    CandidateSpanMatcher,
    EvidenceRole,
    EvidenceSpan,
    EvidenceStance,
    EvidenceVerifier,
    VerificationStatus,
    compute_sha256,
)


def test_exact_verification_success():
    raw_content = "Guido stated: We reject unary ? because of ternary syntax ambiguity."
    quote = "We reject unary ? because of ternary syntax ambiguity."
    start = raw_content.index(quote)
    end = start + len(quote)

    span = EvidenceSpan(
        source_message_version_id=uuid4(),
        exact_quote=quote,
        start_char=start,
        end_char=end,
        evidence_role=EvidenceRole.RULING,
        stance=EvidenceStance.OBJECT,
        verification_status=VerificationStatus.UNVERIFIED,
    )

    is_valid, verified_span = EvidenceVerifier.verify_span(span, raw_content)
    assert is_valid is True
    assert verified_span.verification_status == VerificationStatus.VERIFIED_EXACT
    assert verified_span.content_hash == compute_sha256(quote)


def test_unicode_and_multiline_codepoint_offsets():
    # Contains emojis, non-ASCII characters, and newlines
    raw_content = (
        "Line 1: 🚀 Starting discussion\n"
        "Line 2: Décision: Rejeté car c'est 2× plus lent.\n"
        "Line 3: 👍"
    )  # noqa: RUF001
    quote = "Décision: Rejeté car c'est 2× plus lent."  # noqa: RUF001

    start = raw_content.index(quote)
    end = start + len(quote)

    span = EvidenceSpan(
        source_message_version_id=uuid4(),
        exact_quote=quote,
        start_char=start,
        end_char=end,
        evidence_role=EvidenceRole.OBJECTION,
        stance=EvidenceStance.OBJECT,
    )

    is_valid, verified_span = EvidenceVerifier.verify_span(span, raw_content)
    assert is_valid is True
    assert verified_span.verification_status == VerificationStatus.VERIFIED_EXACT


def test_single_character_drift_remains_unverified():
    raw_content = "I reject Option A because of deadlock risks under fork()."
    quote = "I reject Option A because of deadlock risks under fork()."
    start = raw_content.index(quote)

    # Intentionally shift start offset by 1
    span = EvidenceSpan(
        source_message_version_id=uuid4(),
        exact_quote=quote,
        start_char=start + 1,
        end_char=start + 1 + len(quote),
        evidence_role=EvidenceRole.OBJECTION,
        stance=EvidenceStance.OBJECT,
    )

    # raw_content[start+1 : start+1+len] does not match quote
    is_valid, verified_span = EvidenceVerifier.verify_span(span, raw_content)
    assert is_valid is False
    assert verified_span.verification_status == VerificationStatus.UNVERIFIED


def test_zero_fuzzy_matching_rejects_subtle_differences():
    raw_content = "We measured 2.0x slowdown on large inputs."
    quote = "We measured 2x slowdown on large inputs."  # Missing .0
    start = 0
    end = len(quote)

    span = EvidenceSpan(
        source_message_version_id=uuid4(),
        exact_quote=quote,
        start_char=start,
        end_char=end,
        evidence_role=EvidenceRole.BENCHMARK_REPORT,
        stance=EvidenceStance.OBJECT,
    )

    # Even though 95% similar, strict exact match must fail
    is_valid, verified_span = EvidenceVerifier.verify_span(span, raw_content)
    assert is_valid is False
    assert verified_span.verification_status == VerificationStatus.UNVERIFIED


def test_hash_mismatch_remains_unverified():
    raw_content = "Allocating in loop causes memory leaks."
    quote = "Allocating in loop causes memory leaks."
    start = 0
    end = len(quote)

    span = EvidenceSpan(
        source_message_version_id=uuid4(),
        exact_quote=quote,
        start_char=start,
        end_char=end,
        content_hash="0000000000000000000000000000000000000000000000000000000000000000",
        evidence_role=EvidenceRole.OBJECTION,
        stance=EvidenceStance.OBJECT,
    )

    is_valid, verified_span = EvidenceVerifier.verify_span(span, raw_content)
    assert is_valid is False
    assert verified_span.verification_status == VerificationStatus.UNVERIFIED


def test_edited_message_marks_stale_edited():
    raw_content = "Initial comment about approach."
    quote = "Initial comment about approach."
    span = EvidenceSpan(
        source_message_version_id=uuid4(),
        exact_quote=quote,
        start_char=0,
        end_char=len(quote),
        evidence_role=EvidenceRole.PROPOSAL,
        stance=EvidenceStance.SUPPORT,
    )

    is_valid, verified_span = EvidenceVerifier.verify_span(span, raw_content, is_edited=True)
    assert is_valid is False
    assert verified_span.verification_status == VerificationStatus.STALE_EDITED


def test_candidate_span_matcher_creation():
    content = "First proposal was bad. Second proposal was good. First proposal was bad. Finished."
    quote = "First proposal was bad."

    occurrences = CandidateSpanMatcher.find_all_occurrences(content, quote)
    assert len(occurrences) == 2
    assert occurrences[0] == (0, 23)
    assert occurrences[1] == (50, 73)

    msg_id = uuid4()
    span1 = CandidateSpanMatcher.create_evidence_span(
        source_message_version_id=msg_id,
        message_content=content,
        quote=quote,
        evidence_role=EvidenceRole.OBJECTION,
        stance=EvidenceStance.OBJECT,
        occurrence_index=0,
    )
    assert span1 is not None
    assert span1.start_char == 0
    assert span1.end_char == 23

    # Verification on constructed span succeeds
    is_valid, verified = EvidenceVerifier.verify_span(span1, content)
    assert is_valid is True
    assert verified.verification_status == VerificationStatus.VERIFIED_EXACT
