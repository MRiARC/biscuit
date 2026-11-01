"""Decisions and Alternative lifecycles, Arguments, and Decision Events."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel

from biscuit.models.base import TimestampMixin, UUIDModel, VectorColumn, utcnow
from biscuit.models.enums import (
    DecisionResolution,
    EvidenceStance,
    ProposalLifecycle,
    RejectionCategory,
    ResolutionType,
    RulingAuthority,
)


class Decision(UUIDModel, TimestampMixin, table=True):
    """Architectural or language design decision evaluated over source discussions."""

    __tablename__ = "decisions"
    __table_args__ = (
        # Canonical DDL §7: a decision may only cite evidence that is itself
        # bound to that decision (ruling back-link).
        ForeignKeyConstraint(
            ["ruling_evidence_id", "id"],
            ["evidence_spans.id", "evidence_spans.decision_id"],
            name="fk_decision_ruling_evidence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "resolution_status IN ('RESOLVED', 'UNRESOLVED', 'DISPUTED')",
            name="chk_decision_resolution_status",
        ),
    )

    source_thread_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_threads.id", ondelete="RESTRICT"), nullable=False
        )
    )
    title: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Decision title / design question",
    )
    context_summary: str = Field(
        sa_column=Column(Text, nullable=False),
        description="High-level background summary",
    )
    resolution_status: DecisionResolution = Field(
        default=DecisionResolution.UNRESOLVED,
        sa_column=Column(Text, nullable=False),
    )
    resolution_type: ResolutionType | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    ruling_authority: RulingAuthority | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    ruling_evidence_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        nullable=False,
    )


class DecisionAlternative(UUIDModel, table=True):
    """Specific proposal or syntax choice evaluated within a decision."""

    __tablename__ = "decision_alternatives"
    __table_args__ = (
        UniqueConstraint("decision_id", "proposal_text", name="uq_alt_proposal"),
        CheckConstraint(
            "lifecycle_status <> 'REJECTED' OR ("
            "ruling_evidence_id IS NOT NULL AND ruling_authority IS NOT NULL "
            "AND ruling_actor_id IS NOT NULL AND ruling_actor_grant_id IS NOT NULL)",
            name="chk_rejected_requires_ruling",
        ),
        CheckConstraint(
            "lifecycle_status IN ('PROPOSED', 'UNDER_DISCUSSION', 'ACCEPTED', "
            "'REJECTED', 'WITHDRAWN', 'DEFERRED', 'SUPERSEDED', 'UNRESOLVED', "
            "'DISPUTED', 'ACCEPTED_WITH_MODIFICATION')",
            name="chk_alternative_lifecycle_status",
        ),
        # Canonical DDL §7: the ruling evidence cited by an alternative must be
        # an evidence span bound to that exact alternative.
        ForeignKeyConstraint(
            ["ruling_evidence_id", "id"],
            ["evidence_spans.id", "evidence_spans.alternative_id"],
            name="fk_alternative_ruling_evidence",
            ondelete="RESTRICT",
        ),
    )

    decision_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("decisions.id", ondelete="RESTRICT"), nullable=False
        )
    )
    proposal_text: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Exact syntax or architectural design proposed",
    )
    lifecycle_status: ProposalLifecycle = Field(
        default=ProposalLifecycle.PROPOSED,
        sa_column=Column(Text, nullable=False),
    )
    rejection_category: RejectionCategory | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    rejection_summary: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Canonical rationale summary if rejected",
    )
    proposed_by_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="SET NULL"), nullable=True
        ),
    )
    ruling_evidence_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    ruling_authority: RulingAuthority | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    ruling_actor_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    ruling_actor_grant_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("contributor_project_role_grants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(VectorColumn(768), nullable=True),
        description="768-dim vector embedding (nomic-embed-text-v1.5) for candidate retrieval only",
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False)


class Argument(UUIDModel, TimestampMixin, table=True):
    """Specific argument advanced for or against a decision alternative."""

    __tablename__ = "arguments"
    __table_args__ = (
        Index("idx_arguments_alt", "alternative_id"),
        # Canonical DDL §7: an argument may only cite evidence bound to that
        # exact argument.
        ForeignKeyConstraint(
            ["evidence_span_id", "id"],
            ["evidence_spans.id", "evidence_spans.argument_id"],
            name="fk_argument_evidence_span",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "stance IN ('SUPPORT', 'OBJECT', 'NEUTRAL')", name="chk_argument_stance"
        ),
    )

    alternative_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    author_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="SET NULL"), nullable=True
        ),
    )
    stance: EvidenceStance = Field(
        sa_column=Column(Text, nullable=False),
        description="SUPPORT, OBJECT, or NEUTRAL",
    )
    rationale: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Argument rationale text",
    )
    evidence_span_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=True
        ),
    )


class ProposalOccurrence(UUIDModel, table=True):
    """Recurrence tracking for proposal resurfacing across discussions."""

    __tablename__ = "proposal_occurrences"
    __table_args__ = (
        CheckConstraint(
            "linkage_similarity IS NULL OR (linkage_similarity >= 0.0 AND linkage_similarity <= 1.0)",
            name="chk_occurrence_similarity",
        ),
        UniqueConstraint(
            "id", "alternative_id", "source_thread_id", name="uq_occurrence_alt_thread"
        ),
        ForeignKeyConstraint(
            ["initiating_message_id", "source_thread_id"],
            ["source_messages.id", "source_messages.thread_id"],
            name="fk_occurrence_message_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["initiating_message_version_id", "initiating_message_id"],
            ["source_message_versions.id", "source_message_versions.message_id"],
            name="fk_occurrence_message_version",
            ondelete="RESTRICT",
        ),
        # Canonical DDL §8: linkage evidence must be an evidence span bound to
        # this exact alternative and initiating message version.
        ForeignKeyConstraint(
            [
                "linkage_evidence_span_id",
                "alternative_id",
                "initiating_message_version_id",
            ],
            [
                "evidence_spans.id",
                "evidence_spans.alternative_id",
                "evidence_spans.source_message_version_id",
            ],
            name="fk_occurrence_evidence_alternative",
            ondelete="RESTRICT",
        ),
        Index("idx_occurrences_alt", "alternative_id"),
        Index("idx_occurrences_thread", "source_thread_id"),
    )

    alternative_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    source_thread_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_threads.id", ondelete="RESTRICT"), nullable=False
        )
    )
    initiating_message_id: uuid.UUID = Field(
        sa_column=Column(nullable=False)
    )
    initiating_message_version_id: uuid.UUID = Field(
        sa_column=Column(nullable=False)
    )
    occurred_at: datetime = Field(nullable=False)
    proposal_wording_version: str = Field(
        sa_column=Column(Text, nullable=False),
        description="How the proposal was phrased in this occurrence",
    )
    occurrence_context: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Context in which recurrence appeared",
    )
    occurrence_outcome: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Outcome of this recurrence",
    )
    linkage_similarity: float | None = Field(default=None, nullable=True)
    linkage_status: str = Field(
        default="PROPOSED_RECURRENCE",
        sa_column=Column(Text, nullable=False),
    )
    linkage_evidence_span_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False)


class DecisionAlternativeRulingSignoff(SQLModel, table=True):
    """Maintainer consensus or working-group member sign-off on a ruling."""

    __tablename__ = "decision_alternative_ruling_signoffs"

    alternative_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
    )
    contributor_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
    )
    project_role_grant_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributor_project_role_grants.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    evidence_span_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=False
        )
    )
    signed_at: datetime = Field(default_factory=utcnow, nullable=False)


class DecisionEventLog(UUIDModel, table=True):
    """Append-only audit trail of lifecycle status transitions."""

    __tablename__ = "decision_event_log"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('STATUS_CHANGE', 'RULING_OVERTURNED', 'RECURRENCE_RECORDED', 'RECURRENCE_CONFIRMED')",
            name="chk_decision_event_type",
        ),
        CheckConstraint("recurrence_number >= 1", name="chk_recurrence_number"),
        CheckConstraint(
            "event_type <> 'RECURRENCE_RECORDED' OR "
            "(proposal_occurrence_id IS NOT NULL AND source_thread_id IS NOT NULL)",
            name="chk_recurrence_event_link",
        ),
        ForeignKeyConstraint(
            ["evidence_span_id", "alternative_id"],
            ["evidence_spans.id", "evidence_spans.alternative_id"],
            name="fk_decision_event_evidence",
            ondelete="RESTRICT",
        ),
    )

    alternative_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    source_thread_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("source_threads.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    proposal_occurrence_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("proposal_occurrences.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    recurrence_number: int = Field(default=1, nullable=False)
    event_type: str = Field(
        sa_column=Column(Text, nullable=False),
        description="STATUS_CHANGE, RULING_OVERTURNED, RECURRENCE_RECORDED, RECURRENCE_CONFIRMED",
    )
    previous_status: ProposalLifecycle | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    new_status: ProposalLifecycle = Field(
        sa_column=Column(Text, nullable=False),
    )
    reason: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Logged reason for lifecycle transition",
    )
    evidence_span_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    actor_contributor_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    authority_snapshot: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="Immutable snapshot of authority grant & sign-off IDs at decision time",
    )
    recorded_at: datetime = Field(default_factory=utcnow, nullable=False)
