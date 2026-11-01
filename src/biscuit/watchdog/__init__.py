"""Biscuit PR Diff Watchdog & Guardrail Gate (Stream 4, Issue #20).

Public surface:

* :class:`~biscuit.watchdog.inspector.PRDiffInspector` — unified git diff
  parsing and masked-AST structural fingerprints.
* :class:`~biscuit.watchdog.retriever.CandidateRetriever` — candidate
  retrieval (signal only; cosine >= 0.82 is a retrieval filter, never proof).
* :class:`~biscuit.watchdog.gate.WarningAuthorizationGate` — the four
  non-negotiable warning-authorization invariants with AUDIT_DRY_RUN default.
* :class:`~biscuit.watchdog.formatter.PRCommentFormatter` — verifiable
  GitHub review comment rendering.
"""

from biscuit.watchdog.formatter import PRCommentFormatter
from biscuit.watchdog.gate import AuditLog, WarningAuthorizationGate
from biscuit.watchdog.inspector import (
    AST_NORMALIZATION_VERSION,
    PRDiffInspector,
    structural_fingerprint,
)
from biscuit.watchdog.retriever import (
    CANDIDATE_COSINE_THRESHOLD,
    CandidateRetriever,
    cosine_similarity,
)
from biscuit.watchdog.types import (
    ACTIVE_ANCHOR_STATES,
    HUMAN_RULING_AUTHORITIES,
    AnchorState,
    AuditLogEntry,
    AuthorizationResult,
    CandidateEvaluation,
    CandidateMatch,
    CodeAnchorRecord,
    DecisionAlternativeRecord,
    DecisionRecord,
    DecisionStore,
    DiffFile,
    DiffHunk,
    EvidenceRole,
    EvidenceSpanRecord,
    ExecutionMode,
    FileAnalysis,
    InMemoryDecisionStore,
    InvariantCheck,
    InvariantKey,
    PRDiff,
    ProposalLifecycle,
    SignoffRecord,
    VerificationStatus,
    WatchdogAction,
    WatchdogVerdict,
    compute_sha256,
)

__all__ = [
    "ACTIVE_ANCHOR_STATES",
    "AST_NORMALIZATION_VERSION",
    "CANDIDATE_COSINE_THRESHOLD",
    "HUMAN_RULING_AUTHORITIES",
    "AnchorState",
    "AuditLog",
    "AuditLogEntry",
    "AuthorizationResult",
    "CandidateEvaluation",
    "CandidateMatch",
    "CandidateRetriever",
    "CodeAnchorRecord",
    "DecisionAlternativeRecord",
    "DecisionRecord",
    "DecisionStore",
    "DiffFile",
    "DiffHunk",
    "EvidenceRole",
    "EvidenceSpanRecord",
    "ExecutionMode",
    "FileAnalysis",
    "InMemoryDecisionStore",
    "InvariantCheck",
    "InvariantKey",
    "PRCommentFormatter",
    "PRDiff",
    "PRDiffInspector",
    "ProposalLifecycle",
    "SignoffRecord",
    "VerificationStatus",
    "WarningAuthorizationGate",
    "WatchdogAction",
    "WatchdogVerdict",
    "compute_sha256",
    "cosine_similarity",
    "structural_fingerprint",
]
