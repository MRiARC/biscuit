"""Deliberation graph models: Discussion Edges, Extraction Runs, Living Drafts, and HITL Approvals."""

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

from biscuit.models.base import TimestampMixin, UUIDModel, utcnow
from biscuit.models.enums import (
    DiscussionEdgeType,
    EdgeProvenance,
    HitlActionType,
    HitlRequestStatus,
    LivingDecisionState,
    ProposalLifecycle,
    RunStatus,
)


class DiscussionEdge(UUIDModel, TimestampMixin, table=True):
    """Semantic or conversational deliberation edge between messages and alternatives."""

    __tablename__ = "discussion_edges"
    __table_args__ = (
        CheckConstraint(
            "confidence_score >= 0.0 AND confidence_score <= 1.0",
            name="chk_edge_confidence",
        ),
        Index("idx_edges_thread", "source_thread_id"),
        Index("idx_edges_nodes", "from_message_id", "to_message_id"),
        Index("idx_edges_alt", "target_alternative_id"),
    )

    source_thread_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_threads.id", ondelete="RESTRICT"), nullable=False
        )
    )
    from_message_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_messages.id", ondelete="RESTRICT"), nullable=False
        )
    )
    to_message_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_messages.id", ondelete="RESTRICT"), nullable=False
        )
    )
    source_message_version_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("source_message_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    evidence_span_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    target_decision_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("decisions.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    target_alternative_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    edge_type: DiscussionEdgeType = Field(
        sa_column=Column(Text, nullable=False),
    )
    edge_provenance: EdgeProvenance = Field(
        sa_column=Column(Text, nullable=False),
    )
    confidence_score: float = Field(
        nullable=False,
        description="Confidence score between 0.0 and 1.0",
    )


class ExtractionRun(UUIDModel, table=True):
    """Reproducible audit record of an extraction pipeline execution."""

    __tablename__ = "extraction_runs"

    source_thread_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_threads.id", ondelete="RESTRICT"), nullable=False
        )
    )
    pipeline_version: str = Field(sa_column=Column(Text, nullable=False))
    schema_version: str = Field(sa_column=Column(Text, nullable=False))
    run_config: dict[str, Any] = Field(
        sa_column=Column(JSON, nullable=False),
        description="Slicer, models, seeds and parameter configuration",
    )
    run_config_sha256: str = Field(sa_column=Column(Text, nullable=False))
    environment_lock_sha256: str = Field(sa_column=Column(Text, nullable=False))
    model_pass1_name: str = Field(sa_column=Column(Text, nullable=False))
    model_pass1_revision: str = Field(sa_column=Column(Text, nullable=False))
    model_pass3_name: str = Field(sa_column=Column(Text, nullable=False))
    model_pass3_revision: str = Field(sa_column=Column(Text, nullable=False))
    model_pass4_name: str = Field(sa_column=Column(Text, nullable=False))
    model_pass4_revision: str = Field(sa_column=Column(Text, nullable=False))
    embedding_model_name: str = Field(sa_column=Column(Text, nullable=False))
    embedding_model_revision: str = Field(sa_column=Column(Text, nullable=False))
    prompt_templates_hash: str = Field(sa_column=Column(Text, nullable=False))
    source_thread_snapshot_sha256: str = Field(sa_column=Column(Text, nullable=False))
    run_status: RunStatus = Field(
        default=RunStatus.RUNNING,
        sa_column=Column(Text, nullable=False),
    )
    records_extracted: int = Field(default=0, nullable=False)
    started_at: datetime = Field(default_factory=utcnow, nullable=False)
    completed_at: datetime | None = Field(default=None, nullable=True)


class LivingDecisionDraft(UUIDModel, table=True):
    """Active discussion candidate captured before formal maintainer ruling."""

    __tablename__ = "living_decision_drafts"
    __table_args__ = (
        Index("idx_living_drafts_thread", "source_thread_id"),
        ForeignKeyConstraint(
            ["initiating_message_id", "source_thread_id"],
            ["source_messages.id", "source_messages.thread_id"],
            name="fk_living_draft_message_thread",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["initiating_message_version_id", "initiating_message_id"],
            ["source_message_versions.id", "source_message_versions.message_id"],
            name="fk_living_draft_message_version",
            ondelete="RESTRICT",
        ),
    )

    switch_session_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Switch session ID originating draft",
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
    initiating_author_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=False
        )
    )
    topic: str = Field(sa_column=Column(Text, nullable=False))
    draft_status: LivingDecisionState = Field(
        default=LivingDecisionState.DRAFT_PENDING_APPROVAL,
        sa_column=Column(Text, nullable=False),
    )
    proposed_lifecycle: ProposalLifecycle = Field(
        default=ProposalLifecycle.PROPOSED,
        sa_column=Column(Text, nullable=False),
    )
    proposal_text: str = Field(sa_column=Column(Text, nullable=False))
    rejection_summary: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    target_subsystem: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    created_at: datetime = Field(default_factory=utcnow, nullable=False)
    resolved_at: datetime | None = Field(default=None, nullable=True)


class LivingDecisionDraftEvidence(SQLModel, table=True):
    """Association table linking evidence spans to a living decision draft."""

    __tablename__ = "living_decision_draft_evidence"

    living_draft_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("living_decision_drafts.id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
    )
    evidence_span_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("evidence_spans.id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
    )


class HitlApprovalRequest(UUIDModel, table=True):
    """Switch Human-In-The-Loop approval request record."""

    __tablename__ = "hitl_approval_requests"
    __table_args__ = (
        UniqueConstraint(
            "switch_approval_id", name="uq_hitl_switch_approval_id"
        ),
        UniqueConstraint("idempotency_key", name="uq_hitl_idempotency_key"),
        Index("idx_hitl_approval_switch", "switch_approval_id"),
    )

    switch_approval_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Switch host-side approval request ID",
    )
    action_type: HitlActionType = Field(
        sa_column=Column(Text, nullable=False),
    )
    status: HitlRequestStatus = Field(
        default=HitlRequestStatus.PENDING,
        sa_column=Column(Text, nullable=False),
    )
    idempotency_key: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Unique UUID key preventing replay attacks",
    )
    target_pr_number: int | None = Field(default=None, nullable=True)
    target_repository: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    living_draft_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("living_decision_drafts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    decision_alternative_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    target_commit_sha: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    card_rendered_payload: dict[str, Any] = Field(
        sa_column=Column(JSON, nullable=False),
        description="Interactive card content payload rendered in Switch",
    )
    action_payload_sha256: str = Field(
        sa_column=Column(Text, nullable=False),
        description="SHA-256 digest of card_rendered_payload",
    )
    authorized_roles: list[str] = Field(
        default_factory=lambda: ["BDFL", "Core Maintainer"],
        sa_column=Column(JSON, nullable=False),
        description="Roles eligible to approve/reject this request",
    )
    callback_hmac_secret_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Secret key ID used to sign callback webhook",
    )
    expires_at: datetime = Field(nullable=False)
    created_at: datetime = Field(default_factory=utcnow, nullable=False)


class HitlApprovalEvent(UUIDModel, table=True):
    """Append-only audit record of a maintainer approval decision."""

    __tablename__ = "hitl_approval_events"
    __table_args__ = (
        UniqueConstraint(
            "approval_request_id", name="uq_hitl_event_per_request"
        ),
        Index("idx_hitl_events_request", "approval_request_id"),
    )

    approval_request_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("hitl_approval_requests.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    actor_source_platform: str = Field(
        sa_column=Column(Text, nullable=False),
    )
    actor_platform_id: str = Field(
        sa_column=Column(Text, nullable=False),
    )
    actor_contributor_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=False
        )
    )
    action_taken: str = Field(
        sa_column=Column(Text, nullable=False),
        description="APPROVE_DRAFT, REJECT_DRAFT, AUTHORIZE_PR_COMMENT, DISMISS_PR_COMMENT",
    )
    payload_signature: str = Field(
        sa_column=Column(Text, nullable=False),
        description="HMAC-SHA256 signature verified before insert",
    )
    callback_body_sha256: str = Field(
        sa_column=Column(Text, nullable=False),
    )
    recorded_at: datetime = Field(default_factory=utcnow, nullable=False)
