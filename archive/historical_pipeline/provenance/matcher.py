"""
Candidate Span Matcher.
Locates exact zero-based Unicode code-point offsets for quotes in message texts.
"""

from uuid import UUID

from .types import EvidenceRole, EvidenceSpan, EvidenceStance, VerificationStatus, compute_sha256


class CandidateSpanMatcher:
    """
    Finds exact substring occurrences in message text and constructs EvidenceSpan objects.
    Enforces zero fuzzy matching: matches must be exact substrings.
    """

    @staticmethod
    def find_all_occurrences(text: str, quote: str) -> list[tuple[int, int]]:
        """
        Locates all exact occurrences of quote within text using Unicode code-points.
        Returns list of (start_char, end_char) zero-based offsets.
        """
        if not quote or not text:
            return []

        occurrences: list[tuple[int, int]] = []
        start = 0
        quote_len = len(quote)

        while True:
            idx = text.find(quote, start)
            if idx == -1:
                break
            occurrences.append((idx, idx + quote_len))
            start = idx + 1

        return occurrences

    @classmethod
    def create_evidence_span(
        cls,
        source_message_version_id: UUID,
        message_content: str,
        quote: str,
        evidence_role: EvidenceRole,
        stance: EvidenceStance,
        decision_id: UUID | None = None,
        alternative_id: UUID | None = None,
        argument_id: UUID | None = None,
        code_anchor_id: UUID | None = None,
        occurrence_index: int = 0,
    ) -> EvidenceSpan | None:
        """
        Locates the quote in message_content and constructs an EvidenceSpan.
        Returns None if quote does not exist in message_content.
        """
        occurrences = cls.find_all_occurrences(message_content, quote)
        if not occurrences or occurrence_index >= len(occurrences):
            return None

        start_char, end_char = occurrences[occurrence_index]
        return EvidenceSpan(
            source_message_version_id=source_message_version_id,
            decision_id=decision_id,
            alternative_id=alternative_id,
            argument_id=argument_id,
            code_anchor_id=code_anchor_id,
            exact_quote=quote,
            start_char=start_char,
            end_char=end_char,
            content_hash=compute_sha256(quote),
            evidence_role=evidence_role,
            stance=stance,
            verification_status=VerificationStatus.UNVERIFIED,
        )
