"""Pure, database-independent data contracts for the PR Diff Watchdog.

These records mirror the canonical PostgreSQL DDL in
``docs/STORAGE_AND_RETRIEVAL.md`` (``decision_alternatives``,
``evidence_spans``, ``code_anchors``, ``pr_watchdog_audit_log``) so the
watchdog can be exercised end-to-end against mock decision records with
zero dependency on a live database, Ollama, or GitHub.

Stream 4 (Issue #20) owns this package exclusively; it must not import
modules owned by other workstreams.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


def compute_sha256(text: str) -> str:
    """SHA-256 hex digest over the UTF-8 bytes of ``text``.

    Used for ``content_hash`` on source message versions and evidence
    spans (docs/STORAGE_AND_RETRIEVAL.md, section 4).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProposalLifecycle(str, Enum):
    """Lifecycle states of a decision alternative (DDL ``proposal_lifecycle``)."""

    PROPOSED = "PROPOSED"
    UNDER_DISCUSSION = "UNDER_DISCUSSION"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    DEFERRED = "DEFERRED"
    SUPERSEDED = "SUPERSEDED"
    UNRESOLVED = "UNRESOLVED"
    DISPUTED = "DISPUTED"
    ACCEPTED_WITH_MODIFICATION = "ACCEPTED_WITH_MODIFICATION"


class VerificationStatus(str, Enum):
    """Content-fidelity status of an evidence span (DDL ``verification_status``)."""

    VERIFIED_EXACT = "VERIFIED_EXACT"
    STALE_EDITED = "STALE_EDITED"
    UNVERIFIED = "UNVERIFIED"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceRole(str, Enum):
    """Role an evidence span plays in a deliberation (DDL ``evidence_role``)."""

    PROPOSAL = "PROPOSAL"
    OBJECTION = "OBJECTION"
    REBUTTAL = "REBUTTAL"
    RULING = "RULING"
    CONCESSION = "CONCESSION"
    BENCHMARK_REPORT = "BENCHMARK_REPORT"


class AnchorState(str, Enum):
    """Resolution state of a code anchor (DDL ``anchor_state``)."""

    HISTORICAL = "HISTORICAL"
    CURRENT = "CURRENT"
    MIGRATED = "MIGRATED"
    SPLIT = "SPLIT"
    MERGED = "MERGED"
    DELETED = "DELETED"
    AMBIGUOUS = "AMBIGUOUS"


#: Anchor states that are considered *active* for warning authorization
#: (invariant 3 requires a CURRENT or MIGRATED anchor).
ACTIVE_ANCHOR_STATES = frozenset({AnchorState.CURRENT, AnchorState.MIGRATED})

#: The only ruling authorities a human rejection may carry. Model inference
#: (e.g. a ``MODEL_INFERRED`` label from Pass 3) is never authority.
HUMAN_RULING_AUTHORITIES = frozenset({"BDFL", "CORE_MAINTAINER_CONSENSUS", "WORKING_GROUP"})

#: Project roles that may sign off on rulings, per the lifecycle authority contract.
BDFL_ROLE = "BDFL"
CORE_MAINTAINER_ROLE = "Core Maintainer"
WG_CHAIR_ROLE = "Working Group Chair"
WG_MEMBER_ROLE = "Working Group Member"


class ExecutionMode(str, Enum):
    """Watchdog execution mode (docs/CODE_GROUNDING_AND_AST.md, section 8)."""

    AUDIT_DRY_RUN = "AUDIT_DRY_RUN"
    INTERACTIVE_HITL = "INTERACTIVE_HITL"
    PRODUCTION_COMMENT = "PRODUCTION_COMMENT"


class WatchdogVerdict(str, Enum):
    """Authorization verdict recorded in ``pr_watchdog_audit_log``."""

    WARNING_AUTHORIZED = "WARNING_AUTHORIZED"
    ABSTAINED_UNCERTAIN = "ABSTAINED_UNCERTAIN"
    NO_REJECTED_MATCH = "NO_REJECTED_MATCH"
    ALTERNATIVE_MISMATCH = "ALTERNATIVE_MISMATCH"


class WatchdogAction(str, Enum):
    """Action taken for a checked PR, recorded in ``pr_watchdog_audit_log``."""

    LOGGED_ONLY = "LOGGED_ONLY"
    HITL_PENDING = "HITL_PENDING"
    HITL_CARD_POSTED = "HITL_CARD_POSTED"
    COMMENT_AUTHORIZED = "COMMENT_AUTHORIZED"
    COMMENT_PUBLISHED = "COMMENT_PUBLISHED"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    PUBLISH_FAILED = "PUBLISH_FAILED"


class InvariantKey(str, Enum):
    """Identifier of one of the four non-negotiable authorization invariants."""

    DECISION_STATE = "INVARIANT_1_DECISION_STATE"
    AUTHORITATIVE_RULING = "INVARIANT_2_AUTHORITATIVE_RULING"
    ALTERNATIVE_CODE_ANCHOR = "INVARIANT_3_ALTERNATIVE_CODE_ANCHOR"
    AST_STRUCTURAL_EQUIVALENCE = "INVARIANT_4_AST_STRUCTURAL_EQUIVALENCE"


@dataclass(frozen=True)
class SignoffRecord:
    """One verified project-scoped ruling sign-off.

    Mirrors ``decision_alternative_ruling_signoffs`` joined with
    ``contributor_project_role_grants``. A sign-off is only counted when it is
    active (not expired, not revoked) and scoped to the decision's project.
    """

    contributor_id: str
    role: str
    project_context: str
    active: bool = True

    def is_active_for(self, project_context: str, role: str | None = None) -> bool:
        return (
            self.active
            and self.project_context == project_context
            and (role is None or self.role == role)
        )


@dataclass(frozen=True)
class EvidenceSpanRecord:
    """Mock of an ``evidence_spans`` row relevant to warning authorization."""

    id: str
    alternative_id: str
    evidence_role: EvidenceRole
    verification_status: VerificationStatus
    exact_quote: str
    start_char: int
    end_char: int
    author_handle: str
    author_contributor_id: str
    permalink: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            object.__setattr__(self, "content_hash", compute_sha256(self.exact_quote))

    def is_verified_exact_ruling(self) -> bool:
        """True when the span is a VERIFIED_EXACT RULING for its alternative."""
        return (
            self.evidence_role == EvidenceRole.RULING
            and self.verification_status == VerificationStatus.VERIFIED_EXACT
        )


@dataclass(frozen=True)
class CodeAnchorRecord:
    """Mock of a ``code_anchors`` row bound to one alternative.

    ``head_file_path`` is the lineage-resolved modern path (present when the
    anchor is CURRENT or MIGRATED); ``historical_file_path`` is the path at
    the time of the debate. ``lineage_resolved`` mirrors the parent-aware
    forward commit-DAG outcome: an ambiguous or split lineage stays False and
    the gate abstains.
    """

    id: str
    alternative_id: str
    repository_url: str
    language: str
    symbol_type: str
    qualified_symbol_name: str
    historical_file_path: str
    anchor_state: AnchorState
    head_file_path: str | None = None
    ast_structural_fingerprint: str | None = None
    lineage_resolved: bool = True
    mapping_confidence: float | None = None

    @property
    def symbol_name(self) -> str:
        return self.qualified_symbol_name.rsplit(".", 1)[-1]

    @property
    def current_path(self) -> str:
        """Path the anchor resolves to at modern HEAD."""
        return self.head_file_path or self.historical_file_path

    @property
    def is_active(self) -> bool:
        """Active for authorization: CURRENT/MIGRATED with unambiguous lineage."""
        return self.anchor_state in ACTIVE_ANCHOR_STATES and self.lineage_resolved


@dataclass(frozen=True)
class DecisionAlternativeRecord:
    """Mock of a ``decision_alternatives`` row plus its ruling metadata.

    ``ruling_authority`` is kept as a plain string so mock records can carry a
    ``MODEL_INFERRED`` (or missing) authority: invariant 2 must reject those.
    """

    id: str
    decision_id: str
    proposal_text: str
    lifecycle_status: ProposalLifecycle
    rejection_summary: str | None = None
    rejection_category: str | None = None
    embedding: tuple[float, ...] | None = None
    ruling_evidence_id: str | None = None
    ruling_authority: str | None = None
    ruling_actor_id: str | None = None
    project_context: str = "python/cpython"
    ruling_signoffs: tuple[SignoffRecord, ...] = ()
    anchors: tuple[CodeAnchorRecord, ...] = ()


@dataclass(frozen=True)
class DecisionRecord:
    """Mock of a ``decisions`` row owning one or more alternatives."""

    id: str
    title: str
    context_summary: str
    source_thread_url: str
    project_context: str = "python/cpython"


class DecisionStore(Protocol):
    """Read-side port over the decision knowledge base used by the watchdog."""

    def all_alternatives(self) -> list[DecisionAlternativeRecord]: ...

    def get_alternative(self, alternative_id: str) -> DecisionAlternativeRecord | None: ...

    def get_decision(self, decision_id: str) -> DecisionRecord | None: ...

    def get_evidence_span(self, span_id: str) -> EvidenceSpanRecord | None: ...


class InMemoryDecisionStore:
    """Zero-dependency in-memory ``DecisionStore`` for tests and local runs."""

    def __init__(
        self,
        decisions: Iterable[DecisionRecord] = (),
        alternatives: Iterable[DecisionAlternativeRecord] = (),
        evidence_spans: Iterable[EvidenceSpanRecord] = (),
    ) -> None:
        self._decisions: dict[str, DecisionRecord] = {d.id: d for d in decisions}
        self._alternatives: dict[str, DecisionAlternativeRecord] = {a.id: a for a in alternatives}
        self._spans: dict[str, EvidenceSpanRecord] = {s.id: s for s in evidence_spans}

    def add_decision(self, decision: DecisionRecord) -> None:
        self._decisions[decision.id] = decision

    def add_alternative(self, alternative: DecisionAlternativeRecord) -> None:
        self._alternatives[alternative.id] = alternative

    def add_evidence_span(self, span: EvidenceSpanRecord) -> None:
        self._spans[span.id] = span

    def all_alternatives(self) -> list[DecisionAlternativeRecord]:
        return list(self._alternatives.values())

    def get_alternative(self, alternative_id: str) -> DecisionAlternativeRecord | None:
        return self._alternatives.get(alternative_id)

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        return self._decisions.get(decision_id)

    def get_evidence_span(self, span_id: str) -> EvidenceSpanRecord | None:
        return self._spans.get(span_id)


# ─────────────────────────────────────────────────────────────────────────────
# Parsed PR diff structures (produced by PRDiffInspector)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiffHunk:
    """One unified-diff hunk, tracked on the new (PR) side."""

    new_start: int
    old_start: int
    added_lines: tuple[str, ...]
    removed_lines: tuple[str, ...]
    context_lines: tuple[str, ...]
    section_heading: str = ""


@dataclass(frozen=True)
class DiffFile:
    """One file touched by a PR diff."""

    path: str
    old_path: str | None
    hunks: tuple[DiffHunk, ...]
    is_new_file: bool = False
    is_deleted_file: bool = False
    is_rename: bool = False

    @property
    def added_lines(self) -> tuple[str, ...]:
        added: list[str] = []
        for hunk in self.hunks:
            added.extend(hunk.added_lines)
        return tuple(added)

    @property
    def removed_lines(self) -> tuple[str, ...]:
        removed: list[str] = []
        for hunk in self.hunks:
            removed.extend(hunk.removed_lines)
        return tuple(removed)

    @property
    def added_text(self) -> str:
        return "\n".join(self.added_lines)


@dataclass(frozen=True)
class FileAnalysis:
    """AST-level analysis of one diff file's added lines.

    ``subtree_fingerprints`` holds the masked normalized-AST fingerprint of
    every subtree in the parsed fragment (Tier 3 hashing). A fragment that
    cannot be parsed is marked ``parseable=False`` and the authorization gate
    abstains rather than guessing.
    """

    path: str
    language: str
    added_text: str
    parseable: bool
    top_level_symbols: tuple[str, ...] = ()
    subtree_fingerprints: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PRDiff:
    """A parsed unified git diff for one pull request."""

    files: tuple[DiffFile, ...]
    analyses: tuple[FileAnalysis, ...] = field(default=())
    repository_url: str = ""

    def analysis_for(self, path: str) -> FileAnalysis | None:
        for analysis in self.analyses:
            if analysis.path == path:
                return analysis
        return None

    @property
    def touched_paths(self) -> frozenset[str]:
        return frozenset(f.path for f in self.files)

    @property
    def all_symbols(self) -> frozenset[str]:
        symbols: set[str] = set()
        for analysis in self.analyses:
            symbols.update(analysis.top_level_symbols)
        return frozenset(symbols)

    @property
    def added_text(self) -> str:
        return "\n".join(f.added_text for f in self.files)


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval and authorization results
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateMatch:
    """One candidate alternative produced by retrieval (signal only)."""

    alternative_id: str
    similarity: float | None
    matched_anchor_ids: tuple[str, ...]
    matched_symbols: tuple[str, ...]
    signals: tuple[str, ...]

    @property
    def via_anchor(self) -> bool:
        return bool(self.matched_anchor_ids)


@dataclass(frozen=True)
class InvariantCheck:
    """Outcome of a single authorization invariant for one candidate."""

    key: InvariantKey
    satisfied: bool
    detail: str


@dataclass(frozen=True)
class CandidateEvaluation:
    """Full invariant evaluation of one candidate alternative."""

    alternative_id: str
    proposal_text: str
    similarity: float | None
    invariant_checks: tuple[InvariantCheck, ...]
    authorized: bool
    matched_anchor_ids: tuple[str, ...] = ()

    def check(self, key: InvariantKey) -> InvariantCheck:
        for invariant in self.invariant_checks:
            if invariant.key == key:
                return invariant
        raise KeyError(key)

    @property
    def failed_invariants(self) -> tuple[InvariantCheck, ...]:
        return tuple(i for i in self.invariant_checks if not i.satisfied)


@dataclass(frozen=True)
class AuditLogEntry:
    """One row of ``pr_watchdog_audit_log`` (append-only audit trail)."""

    id: str
    repository_url: str
    pr_number: int
    commit_sha: str
    checked_at: str
    execution_mode: ExecutionMode
    candidate_alternative_ids: tuple[str, ...]
    evaluated_similarity: float | None
    invariants_satisfied: bool
    authorization_verdict: WatchdogVerdict
    action_taken: WatchdogAction
    comment_payload: dict | None

    def to_json_dict(self) -> dict:
        return {
            "id": self.id,
            "repository_url": self.repository_url,
            "pr_number": self.pr_number,
            "commit_sha": self.commit_sha,
            "checked_at": self.checked_at,
            "execution_mode": self.execution_mode.value,
            "candidate_alternative_ids": list(self.candidate_alternative_ids),
            "evaluated_similarity": self.evaluated_similarity,
            "invariants_satisfied": self.invariants_satisfied,
            "authorization_verdict": self.authorization_verdict.value,
            "action_taken": self.action_taken.value,
            "comment_payload": self.comment_payload,
        }


@dataclass(frozen=True)
class AuthorizationResult:
    """Result of running the 4-invariant Warning Authorization Gate."""

    repository_url: str
    pr_number: int
    commit_sha: str
    checked_at: str
    execution_mode: ExecutionMode
    verdict: WatchdogVerdict
    action_taken: WatchdogAction
    candidate_evaluations: tuple[CandidateEvaluation, ...]
    invariants_satisfied: bool
    comment_markdown: str | None = None
    comment_payload: dict | None = None
    hitl_card_payload: dict | None = None
    authorized_alternative_ids: tuple[str, ...] = ()

    @property
    def evaluated_similarity(self) -> float | None:
        similarities = [
            e.similarity for e in self.candidate_evaluations if e.similarity is not None
        ]
        return max(similarities) if similarities else None

    @property
    def abstained(self) -> bool:
        return self.verdict != WatchdogVerdict.WARNING_AUTHORIZED

    def to_audit_entry(self) -> AuditLogEntry:
        """Convert to an append-only ``pr_watchdog_audit_log`` row."""
        return AuditLogEntry(
            id=str(uuid.uuid4()),
            repository_url=self.repository_url,
            pr_number=self.pr_number,
            commit_sha=self.commit_sha,
            checked_at=self.checked_at,
            execution_mode=self.execution_mode,
            candidate_alternative_ids=tuple(e.alternative_id for e in self.candidate_evaluations),
            evaluated_similarity=self.evaluated_similarity,
            invariants_satisfied=self.invariants_satisfied,
            authorization_verdict=self.verdict,
            action_taken=self.action_taken,
            comment_payload=self.comment_payload,
        )
