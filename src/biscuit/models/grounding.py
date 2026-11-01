"""Grounding and AST Lineage models: Code Anchors, Anchor Lineage, and PR Watchdog Audit Log."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field

from biscuit.models.base import TimestampMixin, UUIDModel, utcnow
from biscuit.models.enums import (
    AnchorState,
    CodeSymbolType,
    ExecutionMode,
    PrWatchdogAction,
    PrWatchdogVerdict,
    SourceLocatorType,
)


class CodeAnchor(UUIDModel, TimestampMixin, table=True):
    """AST/CST symbol anchor binding a decision alternative to a codebase location."""

    __tablename__ = "code_anchors"
    __table_args__ = (
        CheckConstraint(
            "mapping_confidence IS NULL OR (mapping_confidence >= 0.0 AND mapping_confidence <= 1.0)",
            name="chk_anchor_confidence",
        ),
        Index("idx_code_anchors_alt", "alternative_id"),
        Index("idx_code_anchors_sym", "qualified_symbol_name"),
    )

    alternative_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    repository_url: str = Field(
        default="https://github.com/python/cpython",
        sa_column=Column(Text, nullable=False),
    )
    language: str = Field(
        sa_column=Column(Text, nullable=False),
        description="c, python, rust, go, etc.",
    )
    symbol_type: CodeSymbolType = Field(
        sa_column=Column(Text, nullable=False),
    )
    qualified_symbol_name: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Canonical qualified symbol path, e.g. cpython.Parser.Tokenizer.get_next_token",
    )
    source_locator_type: SourceLocatorType = Field(
        default=SourceLocatorType.AST_NODE_QUERY,
        sa_column=Column(Text, nullable=False),
    )
    start_line: int | None = Field(default=None, nullable=True)
    end_line: int | None = Field(default=None, nullable=True)
    start_byte: int | None = Field(default=None, nullable=True)
    end_byte: int | None = Field(default=None, nullable=True)
    parser_name: str = Field(sa_column=Column(Text, nullable=False))
    parser_version: str = Field(sa_column=Column(Text, nullable=False))
    grammar_version: str = Field(sa_column=Column(Text, nullable=False))
    fingerprint_algorithm: str = Field(sa_column=Column(Text, nullable=False))
    ast_normalization_version: str = Field(sa_column=Column(Text, nullable=False))
    ast_structural_fingerprint: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    signature_hash: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    anchor_state: AnchorState = Field(
        default=AnchorState.HISTORICAL,
        sa_column=Column(Text, nullable=False),
    )
    historical_file_path: str = Field(sa_column=Column(Text, nullable=False))
    historical_commit_sha: str = Field(sa_column=Column(Text, nullable=False))
    head_commit_sha: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    mapping_confidence: float | None = Field(default=None, nullable=True)


class CodeAnchorLineage(UUIDModel, table=True):
    """Forward commit-DAG lineage step tracking code anchor transitions."""

    __tablename__ = "code_anchor_lineage"
    __table_args__ = (
        CheckConstraint("parent_index >= 0", name="chk_lineage_parent_index"),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0.0 AND confidence_score <= 1.0)",
            name="chk_lineage_confidence",
        ),
        UniqueConstraint(
            "code_anchor_id",
            "transition_commit_sha",
            "compared_parent_sha",
            "historical_path",
            name="uq_anchor_lineage_transition",
        ),
    )

    code_anchor_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("code_anchors.id", ondelete="RESTRICT"), nullable=False
        )
    )
    historical_commit_sha: str = Field(sa_column=Column(Text, nullable=False))
    transition_commit_sha: str = Field(sa_column=Column(Text, nullable=False))
    compared_parent_sha: str = Field(sa_column=Column(Text, nullable=False))
    parent_index: int = Field(nullable=False)
    target_commit_sha: str = Field(sa_column=Column(Text, nullable=False))
    traversal_strategy: str = Field(
        default="FORWARD_COMMIT_DAG_DIFF_M",
        sa_column=Column(Text, nullable=False),
    )
    historical_path: str = Field(sa_column=Column(Text, nullable=False))
    migrated_path: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    transition_type: str = Field(sa_column=Column(Text, nullable=False))
    mapping_evidence: dict[str, Any] = Field(
        sa_column=Column(JSON, nullable=False),
        description="Patch/tree digest, symbol matches, parser/grammar versions",
    )
    confidence_score: float | None = Field(default=None, nullable=True)
    recorded_at: datetime = Field(default_factory=utcnow, nullable=False)


class PrWatchdogAuditLog(UUIDModel, table=True):
    """PR Watchdog evaluation audit log (dry-run and live checks)."""

    __tablename__ = "pr_watchdog_audit_log"

    repository_url: str = Field(sa_column=Column(Text, nullable=False))
    pr_number: int = Field(nullable=False)
    commit_sha: str = Field(sa_column=Column(Text, nullable=False))
    checked_at: datetime = Field(default_factory=utcnow, nullable=False)
    execution_mode: ExecutionMode = Field(
        default=ExecutionMode.AUDIT_DRY_RUN,
        sa_column=Column(Text, nullable=False),
    )
    candidate_alternative_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    evaluated_similarity: float | None = Field(default=None, nullable=True)
    invariants_satisfied: bool = Field(default=False, nullable=False)
    authorization_verdict: PrWatchdogVerdict = Field(
        sa_column=Column(Text, nullable=False),
    )
    action_taken: PrWatchdogAction = Field(
        sa_column=Column(Text, nullable=False),
    )
    comment_payload: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
