"""
Zero-Based Exact Evidence Verifier.
Anti-hallucination verification contract from docs/STORAGE_AND_RETRIEVAL.md Section 7.
"""

from .types import EvidenceSpan, VerificationStatus, compute_sha256


class EvidenceVerifier:
    """
    Verifies that cited quotes exist verbatim at the recorded zero-based Unicode
    code-point offsets and match the recorded SHA-256 hash.
    Zero fuzzy matching: promotes to VERIFIED_EXACT only on 100% exact equality.
    """

    @staticmethod
    def verify_span(
        span: EvidenceSpan, message_content: str, is_edited: bool = False
    ) -> tuple[bool, EvidenceSpan]:
        """
        Verifies an individual evidence span against source message text.

        Returns:
            Tuple[bool, EvidenceSpan]: (is_valid, updated_span)
        """
        # If the underlying message version was modified/tombstoned remotely after extraction
        if is_edited:
            updated = span.model_copy(
                update={"verification_status": VerificationStatus.STALE_EDITED}
            )
            return False, updated

        # Boundary checks on Unicode code-point offsets
        if span.start_char < 0 or span.end_char > len(message_content):
            updated = span.model_copy(update={"verification_status": VerificationStatus.UNVERIFIED})
            return False, updated

        # Extract exact substring slice
        extracted_slice = message_content[span.start_char : span.end_char]

        # 1. Byte-for-byte exact equality check (Zero fuzzy matching)
        if extracted_slice != span.exact_quote:
            updated = span.model_copy(update={"verification_status": VerificationStatus.UNVERIFIED})
            return False, updated

        # 2. Cryptographic SHA-256 integrity check
        calculated_hash = compute_sha256(span.exact_quote)
        if calculated_hash.lower() != span.content_hash.lower():
            updated = span.model_copy(update={"verification_status": VerificationStatus.UNVERIFIED})
            return False, updated

        # Verification successful: promote to VERIFIED_EXACT
        promoted = span.model_copy(
            update={"verification_status": VerificationStatus.VERIFIED_EXACT}
        )
        return True, promoted

    @classmethod
    def verify_spans(
        cls, spans: list[EvidenceSpan], message_content: str, is_edited: bool = False
    ) -> list[EvidenceSpan]:
        """Batch-verifies multiple evidence spans against a message version."""
        results = []
        for span in spans:
            _, updated = cls.verify_span(span, message_content, is_edited=is_edited)
            results.append(updated)
        return results
