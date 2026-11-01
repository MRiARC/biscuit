"""Evidence provenance and exact substring verification module."""

from .matcher import CandidateSpanMatcher
from .types import (
    EvidenceRole,
    EvidenceSpan,
    EvidenceStance,
    VerificationStatus,
    compute_sha256,
)
from .verifier import EvidenceVerifier

__all__ = [
    "CandidateSpanMatcher",
    "EvidenceRole",
    "EvidenceSpan",
    "EvidenceStance",
    "EvidenceVerifier",
    "VerificationStatus",
    "compute_sha256",
]
