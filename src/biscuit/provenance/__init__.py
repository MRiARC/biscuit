"""Evidence provenance and exact substring verification module."""

from .types import (
    EvidenceRole,
    EvidenceStance,
    VerificationStatus,
    EvidenceSpan,
    compute_sha256,
)
from .verifier import EvidenceVerifier
from .matcher import CandidateSpanMatcher

__all__ = [
    "EvidenceRole",
    "EvidenceStance",
    "VerificationStatus",
    "EvidenceSpan",
    "EvidenceVerifier",
    "CandidateSpanMatcher",
    "compute_sha256",
]
