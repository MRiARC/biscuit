"""Provenance models: Contributors, Threads, Messages, Message Versions, and Evidence Spans."""

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
from sqlmodel import Field, SQLModel

from biscuit.models.base import TimestampMixin, UUIDModel, utcnow
from biscuit.models.enums import (
    ContributorProjectRole,
    EvidenceRole,
    EvidenceStance,
    MessageEditState,
    VerificationStatus,
)


class Contributor(UUIDModel, TimestampMixin, table=True):
    """Platform contributor identity resolution."""

    __tablename__ = "contributors"
    __table_args__ = (
        UniqueConstraint(
            "source_platform", "platform_user_id", name="uq_contributor_platform"
        ),
        Index("idx_contributors_handle", "author_handle"),
    )

    source_platform: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Source platform, e.g. discourse, github, slack, mbox",
    )
    platform_user_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Platform-specific user ID",
    )
    canonical_name: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Human readable name",
    )
    author_handle: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Handle/username, e.g. gvanrossum",
    )
    email_prefix: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Sanitized email prefix for mailing list archives",
    )


class ContributorProjectRoleGrant(UUIDModel, table=True):
    """Verified project-scoped authority grant (append-only)."""

    __tablename__ = "contributor_project_role_grants"
    __table_args__ = (
        Index(
            "idx_project_role_grants",
            "contributor_id",
            "project_context",
            "role",
        ),
        CheckConstraint(
            "length(trim(project_context)) > 0", name="chk_grant_project"
        ),
        CheckConstraint(
            "length(trim(verification_source_url)) > 0", name="chk_grant_source"
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="chk_grant_window",
        ),
    )

    contributor_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=False
        )
    )
    project_context: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Target project, e.g. python/cpython",
    )
    role: ContributorProjectRole = Field(
        sa_column=Column(Text, nullable=False),
        description="Role in the project, e.g. BDFL, Core Maintainer",
    )
    verified_by_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=False
        )
    )
    verification_source_url: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Proof URL for role grant verification",
    )
    verified_at: datetime = Field(nullable=False)
    valid_from: datetime = Field(nullable=False)
    valid_until: datetime | None = Field(default=None, nullable=True)
    recorded_at: datetime = Field(default_factory=utcnow, nullable=False)


class ContributorProjectRoleRevocation(SQLModel, table=True):
    """Append-only revocation of a role grant."""

    __tablename__ = "contributor_project_role_revocations"
    __table_args__ = (
        CheckConstraint("length(trim(reason)) > 0", name="chk_revocation_reason"),
    )

    grant_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributor_project_role_grants.id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
    )
    revoked_by_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=False
        )
    )
    revoked_at: datetime = Field(default_factory=utcnow, nullable=False)
    reason: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Justification for role revocation",
    )


class SourceThread(UUIDModel, TimestampMixin, table=True):
    """Deliberation discussion thread container."""

    __tablename__ = "source_threads"
    __table_args__ = (
        UniqueConstraint(
            "source_platform",
            "project_context",
            "external_thread_id",
            name="uq_source_thread",
        ),
    )

    source_platform: str = Field(
        sa_column=Column(Text, nullable=False),
        description="discourse, github_pr, mbox, slack, etc.",
    )
    project_context: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Project identifier, e.g. python/cpython",
    )
    external_thread_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Platform thread ID, e.g. topic_1234 or pull_505",
    )
    title: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Thread topic title",
    )
    canonical_url: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Canonical permalink to thread",
    )
    source_revision: str = Field(default="v1", nullable=False)
    ingestion_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
        description="Platform ingestion metadata",
    )


class SourceMessage(UUIDModel, TimestampMixin, table=True):
    """Discrete message inside a discussion thread."""

    __tablename__ = "source_messages"
    __table_args__ = (
        UniqueConstraint(
            "thread_id", "external_message_id", name="uq_source_message"
        ),
        UniqueConstraint("id", "thread_id", name="uq_source_message_id_thread"),
        UniqueConstraint(
            "id", "thread_id", "author_id", name="uq_source_message_author_thread"
        ),
        Index("idx_source_messages_thread", "thread_id"),
        Index("idx_source_messages_parent", "parent_message_id"),
    )

    thread_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_threads.id", ondelete="RESTRICT"), nullable=False
        )
    )
    external_message_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Platform post/message ID, e.g. post_12",
    )
    parent_message_id: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Platform parent post ID if reply",
    )
    author_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            # RESTRICT per Issue #17 §3.A: deleting a contributor must never
            # silently erase message attribution relied on by ruling-author checks.
            ForeignKey("contributors.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    original_timestamp: datetime = Field(
        nullable=False,
        description="Original post timestamp from platform",
    )
    canonical_permalink: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Direct permalink to this specific message",
    )


class SourceMessageVersion(UUIDModel, table=True):
    """Immutable version of a source message capturing exact wire bytes."""

    __tablename__ = "source_message_versions"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "version_number", name="uq_message_version"
        ),
        UniqueConstraint("id", "message_id", name="uq_message_version_id_message"),
        Index(
            "uq_message_single_current",
            "message_id",
            unique=True,
            sqlite_where=Column("is_current") == True,
            postgresql_where=Column("is_current") == True,
        ),
        CheckConstraint("version_number >= 1", name="chk_message_version_number"),
        CheckConstraint(
            "edit_state IN ('ORIGINAL', 'EDITED', 'DELETED', 'UNAVAILABLE')",
            name="chk_message_version_edit_state",
        ),
        CheckConstraint(
            "length(content_hash) = 64", name="chk_message_version_hash_length"
        ),
    )

    message_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_messages.id", ondelete="RESTRICT"), nullable=False
        )
    )
    version_number: int = Field(
        default=1,
        nullable=False,
        description="Sequential 1-based version number",
    )
    raw_content: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Exact untouched wire content bytes decoded as UTF-8 string; never normalized",
    )
    content_hash: str = Field(
        sa_column=Column(Text, nullable=False),
        description="SHA-256 hex digest of UTF-8 bytes of exact raw_content",
    )
    captured_at: datetime = Field(default_factory=utcnow, nullable=False)
    is_current: bool = Field(
        default=True,
        nullable=False,
        description="True for active version, rotated transactionally",
    )
    edit_state: MessageEditState = Field(
        default=MessageEditState.ORIGINAL,
        sa_column=Column(Text, nullable=False),
    )
    edit_reason: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    deleted_at: datetime | None = Field(default=None, nullable=True)
    derived_normalized_content: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Optional normalized retrieval view; never used for evidence offsets",
    )
    derived_cleaned_content: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Noise-filtered view; never used for evidence offsets",
    )


class EvidenceSpan(UUIDModel, TimestampMixin, table=True):
    """Exact verbatim slice of a source message version proving a claim."""

    __tablename__ = "evidence_spans"
    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN decision_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN alternative_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN argument_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN code_anchor_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="chk_evidence_target_exactly_one",
        ),
        CheckConstraint(
            "start_char >= 0 AND end_char > start_char",
            name="chk_char_offsets",
        ),
        UniqueConstraint(
            "id", "alternative_id", name="uq_evidence_id_alternative"
        ),
        UniqueConstraint("id", "decision_id", name="uq_evidence_id_decision"),
        UniqueConstraint("id", "argument_id", name="uq_evidence_id_argument"),
        UniqueConstraint(
            "id",
            "alternative_id",
            "source_message_version_id",
            name="uq_evidence_edge_source",
        ),
        CheckConstraint(
            "verification_status IN ('UNVERIFIED', 'VERIFIED_EXACT', 'STALE_EDITED', 'UNAVAILABLE')",
            name="chk_evidence_verification_status",
        ),
        CheckConstraint(
            "evidence_role IN ('PROPOSAL', 'OBJECTION', 'REBUTTAL', 'RULING', "
            "'CONCESSION', 'BENCHMARK_REPORT')",
            name="chk_evidence_role",
        ),
        CheckConstraint(
            "stance IN ('SUPPORT', 'OBJECT', 'NEUTRAL')", name="chk_evidence_stance"
        ),
        CheckConstraint(
            "length(content_hash) = 64", name="chk_evidence_hash_length"
        ),
    )

    source_message_version_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("source_message_versions.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    decision_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("decisions.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    alternative_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("decision_alternatives.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    argument_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("arguments.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    code_anchor_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("code_anchors.id", ondelete="RESTRICT"), nullable=True
        ),
    )
    exact_quote: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Verbatim quote slice matching raw_content[start_char:end_char]",
    )
    start_char: int = Field(
        nullable=False,
        description="Zero-based start Unicode code point offset (inclusive)",
    )
    end_char: int = Field(
        nullable=False,
        description="Zero-based end Unicode code point offset (exclusive)",
    )
    content_hash: str = Field(
        sa_column=Column(Text, nullable=False),
        description="SHA-256 hex digest of exact_quote UTF-8 bytes",
    )
    evidence_role: EvidenceRole = Field(
        sa_column=Column(Text, nullable=False),
        description="Role played by evidence: PROPOSAL, OBJECTION, RULING, etc.",
    )
    stance: EvidenceStance = Field(
        sa_column=Column(Text, nullable=False),
        description="SUPPORT, OBJECT, or NEUTRAL",
    )
    verification_status: VerificationStatus = Field(
        default=VerificationStatus.UNVERIFIED,
        sa_column=Column(Text, nullable=False),
        description="UNVERIFIED until cryptographic slice check promotes to VERIFIED_EXACT",
    )
