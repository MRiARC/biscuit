"""Pure data contracts for the Switch FastMCP agent bridge (Stream 4).

Mirrors the canonical Pillar-2 model in ``docs/LIVING_DECISION_INTELLIGENCE.md``
and ``docs/STORAGE_AND_RETRIEVAL.md`` sections 12–14:

* Switch agent-controller **0.31.0 pinned upstream commit**
  ``7e57cd433a922bc91a00150c106e6ef1f15b1057`` — the activity API surface
  used here is ``POST /agent-sessions/{session_id}/approvals``,
  ``POST /agent-sessions/{session_id}/approvals/{request_id}/close``,
  ``POST /agent-sessions/{session_id}/approvals/{request_id}/delivered`` and
  ``GET /agent-sessions/approvals/outcomes``. Biscuit talks to Switch only
  through this published HTTP boundary; importing Switch's internal Python
  packages is prohibited (Protocol Separation invariant).
* HITL approval requests carry a UUIDv4 ``idempotency_key``, a 72-hour
  ``expires_at`` and an ``action_payload_sha256`` over the canonical JSON of
  the rendered card payload.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

#: Pinned upstream Switch integration target (agent-controller 0.31.0).
SWITCH_UPSTREAM_COMMIT = "7e57cd433a922bc91a00150c106e6ef1f15b1057"

#: Approval cards expire 72 hours after creation.
APPROVAL_TTL_HOURS = 72

#: Canonical JSON serialization used for action payload hashing and HMAC.
# (sort_keys + compact separators, so the digest is stable across processes.)


def canonical_json(payload: dict | list) -> str:
    """Canonical JSON text for hashing/signing (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_sha256_hex(text: str) -> str:
    """SHA-256 hex digest over the UTF-8 bytes of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_idempotency_key() -> str:
    """Fresh UUIDv4 idempotency key (matches the DDL CHECK constraint)."""
    return str(uuid.uuid4())


class SwitchActionType(str, Enum):
    """HITL approval request action types (DDL ``hitl_action_type``)."""

    APPROVE_LIVING_DECISION = "APPROVE_LIVING_DECISION"
    AUTHORIZE_PR_WARNING = "AUTHORIZE_PR_WARNING"
    DISMISS_PR_WARNING = "DISMISS_PR_WARNING"


class HITLRequestStatus(str, Enum):
    """HITL approval request lifecycle (DDL ``hitl_request_status``)."""

    PENDING = "PENDING"
    DECIDED = "DECIDED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class HITLEventAction(str, Enum):
    """Terminal actions recorded in ``hitl_approval_events``."""

    APPROVE_DRAFT = "APPROVE_DRAFT"
    EDIT_RATIONALE = "EDIT_RATIONALE"
    REJECT_DRAFT = "REJECT_DRAFT"
    AUTHORIZE_PR_COMMENT = "AUTHORIZE_PR_COMMENT"
    DISMISS_PR_COMMENT = "DISMISS_PR_COMMENT"


class DraftStatus(str, Enum):
    """Living decision draft lifecycle (DDL ``living_decision_state``)."""

    DRAFT_PENDING_APPROVAL = "DRAFT_PENDING_APPROVAL"
    MAINTAINER_APPROVED = "MAINTAINER_APPROVED"
    MAINTAINER_REJECTED = "MAINTAINER_REJECTED"
    EDIT_REQUESTED = "EDIT_REQUESTED"


class SourcePlatform(str, Enum):
    """Chat platforms a living decision can originate from."""

    SLACK = "slack"
    DISCORD = "discord"
    ZULIP = "zulip"


@dataclass(frozen=True)
class LiveMessagePayload:
    """One streamed chat message (FastMCP ``draft_living_decision`` input)."""

    external_message_id: str
    author_platform_id: str
    author_handle: str
    timestamp: str
    raw_content: str
    parent_message_id: str | None = None


@dataclass(frozen=True)
class LivingDecisionDraft:
    """Mock of a ``living_decision_drafts`` row created from live chat.

    Draft publication approval (Transition A) only sets ``draft_status``;
    alternative lifecycle assignment (Transition B) and authoritative
    rejection (Transition C) are separate human actions that this record
    deliberately does not conflate.
    """

    id: str
    switch_session_id: str
    source_platform: SourcePlatform
    channel_id: str
    thread_ts: str
    topic: str
    proposal_text: str
    initiating_author_handle: str
    draft_status: DraftStatus = DraftStatus.DRAFT_PENDING_APPROVAL
    target_subsystem: str | None = None
    rejection_summary: str | None = None
    evidence_quotes: tuple[tuple[str, str, int, int], ...] = ()
    created_at: str = ""

    @property
    def external_thread_id(self) -> str:
        return f"{self.channel_id}:{self.thread_ts}"

    def canonical_url(self) -> str:
        if self.source_platform == SourcePlatform.SLACK:
            return (
                f"https://slack.com/archives/{self.channel_id}/p{self.thread_ts.replace('.', '')}"
            )
        return f"https://{self.source_platform.value}/channels/{self.channel_id}"


@dataclass
class HITLApprovalRequest:
    """Mock of a ``hitl_approval_requests`` row.

    ``action_payload_sha256`` pins the exact approvable payload: the hash of
    the canonical JSON of ``card_rendered_payload``. Requests are one-way
    (PENDING -> terminal) and expire after 72 hours.
    """

    id: str
    switch_approval_id: str
    action_type: SwitchActionType
    idempotency_key: str
    card_rendered_payload: dict
    authorized_roles: tuple[str, ...] = ("BDFL", "Core Maintainer")
    status: HITLRequestStatus = HITLRequestStatus.PENDING
    target_pr_number: int | None = None
    target_repository: str | None = None
    target_commit_sha: str | None = None
    living_draft_id: str | None = None
    decision_alternative_id: str | None = None
    callback_hmac_secret_id: str = "switch-callback-secret-1"
    expires_at: datetime | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        now = datetime.now(UTC)
        if self.created_at is None:
            self.created_at = now
        if self.expires_at is None:
            self.expires_at = self.created_at + timedelta(hours=APPROVAL_TTL_HOURS)

    @property
    def action_payload_sha256(self) -> str:
        return compute_sha256_hex(canonical_json(self.card_rendered_payload))

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return now >= self.expires_at

    def to_json_dict(self) -> dict:
        return {
            "id": self.id,
            "switch_approval_id": self.switch_approval_id,
            "action_type": self.action_type.value,
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "target_pr_number": self.target_pr_number,
            "target_repository": self.target_repository,
            "target_commit_sha": self.target_commit_sha,
            "living_draft_id": self.living_draft_id,
            "decision_alternative_id": self.decision_alternative_id,
            "authorized_roles": list(self.authorized_roles),
            "action_payload_sha256": self.action_payload_sha256,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class HITLApprovalEvent:
    """Append-only ``hitl_approval_events`` row for one decided request."""

    id: str
    approval_request_id: str
    actor_source_platform: str
    actor_platform_id: str
    actor_contributor_id: str
    action_taken: HITLEventAction
    payload_signature: str
    callback_body_sha256: str
    recorded_at: str = ""


@dataclass(frozen=True)
class ApprovalOutcome:
    """Parsed ``ApprovalView`` returned by Switch's outcomes endpoint."""

    session_id: str
    request_id: str
    state: str
    answered_by: str | None
    answered_at: str | None
    expires_at: str | None
    selected_option_id: str | None = None

    @classmethod
    def from_json(cls, payload: dict) -> ApprovalOutcome:
        answer = payload.get("answer")
        selected_option_id: str | None = None
        if isinstance(answer, dict):
            selected_option_id = (
                answer.get("optionId") or answer.get("option_id") or answer.get("id")
            )
        elif isinstance(answer, str):
            selected_option_id = answer

        if not selected_option_id:
            selected_option_id = (
                payload.get("selectedOptionId")
                or payload.get("selected_option_id")
                or payload.get("decision")
            )

        return cls(
            session_id=payload.get("sessionId") or payload.get("session_id", ""),
            request_id=payload.get("requestId") or payload.get("request_id", ""),
            state=payload.get("state", ""),
            answered_by=payload.get("answeredBy") or payload.get("answered_by"),
            answered_at=payload.get("answeredAt") or payload.get("answered_at"),
            expires_at=payload.get("expiresAt") or payload.get("expires_at"),
            selected_option_id=selected_option_id,
        )


@dataclass(frozen=True)
class CallbackVerification:
    """Result of authenticating one Switch callback."""

    valid: bool
    request: HITLApprovalRequest | None
    reason: str = ""
    signature: str = ""
    body_sha256: str = ""

    @property
    def accepted(self) -> bool:
        return self.valid


@dataclass(frozen=True)
class QueryDecisionsInput:
    """``query_decisions`` MCP tool input."""

    query_text: str
    repository_context: str = "python/cpython"
    top_k: int = 3


@dataclass(frozen=True)
class CheckPRDiffInput:
    """``check_pr_diff`` MCP tool input."""

    repository_url: str
    pr_number: int
    diff_text: str
    commit_sha: str = ""


@dataclass(frozen=True)
class DraftLivingDecisionInput:
    """``draft_living_decision`` MCP tool input."""

    switch_session_id: str
    source_platform: str
    channel_id: str
    thread_ts: str
    initiating_author_handle: str
    messages: tuple[LiveMessagePayload, ...] = ()


@dataclass(frozen=True)
class DecisionSummary:
    """One decision returned by ``query_decisions``."""

    decision_id: str
    title: str
    project_context: str
    top_alternative_text: str
    top_alternative_status: str
    ruling_authority: str | None
    source_thread_url: str
    similarity: float | None = None

    def to_json_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "title": self.title,
            "project_context": self.project_context,
            "top_alternative_text": self.top_alternative_text,
            "top_alternative_status": self.top_alternative_status,
            "ruling_authority": self.ruling_authority,
            "source_thread_url": self.source_thread_url,
            "similarity": self.similarity,
        }


@dataclass(frozen=True)
class DraftResult:
    """Result payload of ``draft_living_decision``."""

    draft_id: str
    topic: str
    proposal_text: str
    draft_status: str
    initiating_author_handle: str
    evidence_spans: tuple[dict, ...] = ()

    def to_json_dict(self) -> dict:
        return {
            "draft_id": self.draft_id,
            "topic": self.topic,
            "proposal_text": self.proposal_text,
            "draft_status": self.draft_status,
            "initiating_author_handle": self.initiating_author_handle,
            "evidence_spans": list(self.evidence_spans),
        }
