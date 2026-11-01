"""Biscuit Switch FastMCP Agent Bridge (Stream 4, Issue #20).

Public surface:

* :class:`~biscuit.switch.server.FastMCPServer` — Biscuit's MCP server
  (``fastmcp>=3.2.0,<4``) exposing ``query_decisions``, ``check_pr_diff``
  and ``draft_living_decision`` to Switch.
* :class:`~biscuit.switch.activity_client.SwitchActivityClient` — HTTP
  client for the pinned Switch agent-controller 0.31.0 activity routes.
* :class:`~biscuit.switch.cards.BlockKitBuilder` — Slack Block Kit HITL
  approval cards and Switch ``ApprovalOpen`` payloads.
* :class:`~biscuit.switch.security.CallbackSecurity` — HMAC-SHA256 callback
  verification with idempotency keys, replay prevention and 72h expiry.
"""

from biscuit.switch.activity_client import (
    DEFAULT_BASE_URL,
    MockTransport,
    SwitchActivityClient,
)
from biscuit.switch.cards import BlockKitBuilder
from biscuit.switch.persistence import TranscriptStore
from biscuit.switch.security import (
    CALLBACK_REJECTED,
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    CallbackSecurity,
    sign_body,
)
from biscuit.switch.server import (
    SERVER_NAME,
    CheckPRDiffParams,
    DraftLivingDecisionParams,
    FastMCPServer,
    LiveMessageParams,
    QueryDecisionsParams,
    build_default_server,
)
from biscuit.switch.types import (
    APPROVAL_TTL_HOURS,
    SWITCH_UPSTREAM_COMMIT,
    ApprovalOutcome,  # noqa: F401  (re-exported public name)
    CallbackVerification,
    CheckPRDiffInput,
    DraftResult,
    DraftStatus,
    HITLApprovalEvent,
    HITLApprovalRequest,
    HITLEventAction,
    HITLRequestStatus,
    LiveMessagePayload,
    LivingDecisionDraft,
    SourcePlatform,
    SwitchActionType,
    canonical_json,
    compute_sha256_hex,
    new_idempotency_key,
)

__all__ = [
    "APPROVAL_TTL_HOURS",
    "CALLBACK_REJECTED",
    "DEFAULT_BASE_URL",
    "IDEMPOTENCY_HEADER",
    "SERVER_NAME",
    "SIGNATURE_HEADER",
    "SWITCH_UPSTREAM_COMMIT",
    "BlockKitBuilder",
    "CallbackSecurity",
    "CallbackVerification",
    "CheckPRDiffInput",
    "CheckPRDiffParams",
    "DraftLivingDecisionParams",
    "DraftResult",
    "DraftStatus",
    "FastMCPServer",
    "HITLApprovalEvent",
    "HITLApprovalRequest",
    "HITLEventAction",
    "HITLRequestStatus",
    "LiveMessageParams",
    "LiveMessagePayload",
    "LivingDecisionDraft",
    "MockTransport",
    "QueryDecisionsParams",
    "SourcePlatform",
    "SwitchActionType",
    "SwitchActivityClient",
    "TranscriptStore",
    "build_default_server",
    "canonical_json",
    "compute_sha256_hex",
    "new_idempotency_key",
    "sign_body",
]
