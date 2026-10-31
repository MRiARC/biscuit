"""
Data types and schemas for evidence spans and provenance tracking.
Conforms strictly to docs/STORAGE_AND_RETRIEVAL.md Section 7.
"""

import hashlib
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class EvidenceRole(str, Enum):
    PROPOSAL = "PROPOSAL"
    OBJECTION = "OBJECTION"
    REBUTTAL = "REBUTTAL"
    RULING = "RULING"
    CONCESSION = "CONCESSION"
    BENCHMARK_REPORT = "BENCHMARK_REPORT"


class EvidenceStance(str, Enum):
    SUPPORT = "SUPPORT"
    OBJECT = "OBJECT"
    NEUTRAL = "NEUTRAL"


class VerificationStatus(str, Enum):
    VERIFIED_EXACT = "VERIFIED_EXACT"
    STALE_EDITED = "STALE_EDITED"
    UNVERIFIED = "UNVERIFIED"
    UNAVAILABLE = "UNAVAILABLE"


def compute_sha256(text: str) -> str:
    """Computes SHA-256 over UTF-8 encoded bytes of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EvidenceSpan(BaseModel):
    """
    Exact, tamper-proof character span locator pointing into a source_message_version.
    Offsets use 0-based, start-inclusive, end-exclusive Unicode codepoints [start_char, end_char).
    """

    id: UUID = Field(default_factory=uuid4)
    source_message_version_id: UUID
    decision_id: UUID | None = None
    alternative_id: UUID | None = None
    argument_id: UUID | None = None
    code_anchor_id: UUID | None = None
    exact_quote: str
    start_char: int
    end_char: int
    content_hash: str = ""
    evidence_role: EvidenceRole
    stance: EvidenceStance
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def set_content_hash_and_offsets(cls, values: dict) -> dict:
        quote = values.get("exact_quote", "")
        if not values.get("content_hash") and quote:
            values["content_hash"] = compute_sha256(quote)
        return values

    @model_validator(mode="after")
    def validate_invariants(self) -> "EvidenceSpan":
        if self.start_char < 0:
            raise ValueError(f"start_char must be >= 0, got {self.start_char}")
        if self.end_char <= self.start_char:
            raise ValueError(f"end_char ({self.end_char}) must be > start_char ({self.start_char})")
        # Ensure length matches quote code-point count
        expected_len = len(self.exact_quote)
        span_len = self.end_char - self.start_char
        if expected_len != span_len:
            raise ValueError(
                f"Quote character length ({expected_len}) does not match span length ({span_len})"
            )
        # Enforce database constraint: at most one typed target can be non-null
        targets = [self.decision_id, self.alternative_id, self.argument_id, self.code_anchor_id]
        non_null_count = sum(1 for t in targets if t is not None)
        if non_null_count > 1:
            raise ValueError(
                f"At most one evidence target (decision_id, alternative_id, argument_id, code_anchor_id) "
                f"may be non-null, got {non_null_count}"
            )
        return self
