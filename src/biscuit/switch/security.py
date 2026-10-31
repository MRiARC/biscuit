"""Switch callback authentication: HMAC-SHA256, replay prevention, expiry.

Implements the callback security contract of
docs/LIVING_DECISION_INTELLIGENCE.md section 5A:

1. **HMAC-SHA256 signature** — every incoming callback must carry
   ``X-Switch-Signature: sha256=<hex>``, computed by Switch over the *raw*
   callback body with a pre-shared secret. Biscuit recomputes and compares
   in constant time; a missing/malformed/incorrect signature is rejected.
2. **Idempotency / replay prevention** — each approval card carries a unique
   UUIDv4 ``idempotency_key``. The key must be explicitly provided by the caller.
   A callback for a request whose status is no longer ``PENDING`` returns
   ``409 Conflict`` semantics (duplicate rejected and discarded); replayed
   bodies for the same request never produce a second decision event.
3. **Freshness & TTL** — incoming callbacks must fall within a 5-minute
   freshness window, and requests expire after a 72-hour TTL.
4. **Binding & Project Authority** — the verified callback must match the request's
   idempotency key, action compatibility, and pinned payload digest
   (``action_payload_sha256``). The acting contributor must hold an active,
   verified project-scoped maintainer role.
"""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime

from dateutil import parser as dt_parser

from biscuit.switch.types import (
    HITLApprovalRequest,
    HITLEventAction,
    HITLRequestStatus,
    SwitchActionType,
    compute_sha256_hex,
)

SIGNATURE_HEADER = "X-Switch-Signature"
IDEMPOTENCY_HEADER = "X-Switch-Idempotency-Key"

_SIGNATURE_RE = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_UUIDV4_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

#: Returned to callers for rejected callbacks (409 Conflict semantics).
CALLBACK_REJECTED = 409

_ACTION_COMPATIBILITY = {
    SwitchActionType.APPROVE_LIVING_DECISION: (
        HITLEventAction.APPROVE_DRAFT,
        HITLEventAction.EDIT_RATIONALE,
        HITLEventAction.REJECT_DRAFT,
    ),
    SwitchActionType.AUTHORIZE_PR_WARNING: (
        HITLEventAction.AUTHORIZE_PR_COMMENT,
        HITLEventAction.DISMISS_PR_COMMENT,
    ),
    SwitchActionType.DISMISS_PR_WARNING: (HITLEventAction.DISMISS_PR_COMMENT,),
}


def sign_body(secret: str, body: bytes) -> str:
    """Compute the ``sha256=<hex>`` HMAC signature over a raw callback body."""
    digest = hmac.new(secret.encode("utf-8"), body, "sha256").hexdigest()
    return f"sha256={digest}"


def _parse_signature(header_value: str | None) -> str | None:
    if header_value is None:
        return None
    match = _SIGNATURE_RE.match(header_value.strip())
    return match.group(1).lower() if match else None


class CallbackSecurity:
    """Validates Switch approval callbacks and prevents replayed actions."""

    def __init__(self, secret: str, tolerance_seconds: float = 300.0) -> None:
        self._secret = secret
        self.tolerance_seconds = tolerance_seconds
        # Bodies already seen for a request; a replay carries identical bytes.
        self._seen_bodies: dict[str, set[str]] = {}
        # Idempotency keys already consumed by a finalized request.
        self._consumed_keys: set[str] = set()

    # ── verification ────────────────────────────────────────────────────

    def verify(
        self,
        request: HITLApprovalRequest | None,
        body: bytes,
        signature_header: str | None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str, str]:
        """Authenticate one callback; returns ``(valid, reason, body_sha256)``.

        Requires caller to explicitly provide the UUIDv4 ``idempotency_key``
        (no silent fallback to request). Enforces HMAC integrity, replay prevention,
        72h request TTL, and 5-minute callback freshness when timestamped.
        """
        body_sha256 = compute_sha256_hex(body.decode("utf-8", errors="replace"))

        signature = _parse_signature(signature_header)
        if signature is None:
            return False, "missing or malformed X-Switch-Signature header", body_sha256
        expected = hmac.new(self._secret.encode("utf-8"), body, "sha256").hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False, "HMAC-SHA256 signature does not match raw callback body", body_sha256

        if request is None:
            return False, "no approval request found for the callback", body_sha256

        if not idempotency_key:
            return False, "idempotency key is missing", body_sha256

        if not _UUIDV4_RE.match(idempotency_key):
            return False, "idempotency key is missing or not a UUIDv4", body_sha256

        if idempotency_key != request.idempotency_key:
            return False, "idempotency key does not match the approval request", body_sha256

        if idempotency_key in self._consumed_keys:
            return False, "replayed callback: request already finalized (409 Conflict)", body_sha256

        if body_sha256 in self._seen_bodies.get(request.id, set()):
            return False, "replayed callback body for this request (409 Conflict)", body_sha256

        current = now or datetime.now(UTC)
        if request.status != HITLRequestStatus.PENDING:
            return (
                False,
                f"request status is {request.status.value}, not PENDING (409 Conflict)",
                body_sha256,
            )
        if request.is_expired(current):
            return False, "approval request has expired (72h TTL)", body_sha256

        # Parse JSON payload claims if available
        try:
            payload_data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload_data = None

        if isinstance(payload_data, dict):
            # Check 5-minute freshness window if callback provides timestamp
            ts_str = (
                payload_data.get("timestamp")
                or payload_data.get("created_at")
                or payload_data.get("answeredAt")
            )
            if ts_str:
                try:
                    cb_time = dt_parser.isoparse(ts_str)
                    if cb_time.tzinfo is None:
                        cb_time = cb_time.replace(tzinfo=UTC)
                    if abs((current - cb_time).total_seconds()) > self.tolerance_seconds:
                        return (
                            False,
                            "callback timestamp outside 5-minute freshness window",
                            body_sha256,
                        )
                except (ValueError, TypeError):
                    return False, "malformed callback timestamp format", body_sha256

            # Bound request ID check
            body_req_id = payload_data.get("request_id") or payload_data.get("requestId")
            if body_req_id and body_req_id not in (request.id, request.switch_approval_id):
                return (
                    False,
                    f"callback request_id '{body_req_id}' does not match approval request '{request.id}'",
                    body_sha256,
                )

            # Bound payload hash check
            body_payload_sha = payload_data.get("action_payload_sha256") or payload_data.get(
                "payload_sha256"
            )
            if body_payload_sha and body_payload_sha != request.action_payload_sha256:
                return (
                    False,
                    "callback action_payload_sha256 does not match pinned request payload",
                    body_sha256,
                )

        self._seen_bodies.setdefault(request.id, set()).add(body_sha256)
        return True, "callback authenticated", body_sha256

    # ── finalization ────────────────────────────────────────────────────

    def finalize(
        self,
        request: HITLApprovalRequest,
        action: HITLEventAction,
        body: bytes,
        signature_header: str | None,
        actor_platform_id: str,
        actor_source_platform: str,
        actor_contributor_id: str,
        idempotency_key: str | None = None,
        verified_actor_roles: tuple[str, ...] | list[str] | None = None,
        now: datetime | None = None,
    ) -> tuple[HITLApprovalRequest | None, str]:
        """Verify then consume a callback for ``request``.

        On success the request transitions to ``DECIDED``, the idempotency key
        is burned atomically, and the action and project-scoped maintainer
        authority are validated.
        """
        valid, reason, _ = self.verify(request, body, signature_header, idempotency_key, now)
        if not valid:
            return None, reason

        # Signed body claim validation
        try:
            payload_data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload_data = None

        if isinstance(payload_data, dict):
            body_action = payload_data.get("action")
            if body_action and body_action != action.value:
                return None, (
                    f"signed callback action '{body_action}' does not match "
                    f"requested action '{action.value}'"
                )
            body_actor = payload_data.get("actor_contributor_id")
            if body_actor and body_actor != actor_contributor_id:
                return None, (
                    f"signed callback actor '{body_actor}' does not match "
                    f"caller '{actor_contributor_id}'"
                )

        # Project-scoped maintainer authority check
        if verified_actor_roles is not None and not any(
            r in request.authorized_roles for r in verified_actor_roles
        ):
            target_desc = request.target_repository or "target project"
            return None, (
                f"actor {actor_contributor_id} does not hold an authorized "
                f"maintainer role for {target_desc}"
            )

        allowed = _ACTION_COMPATIBILITY.get(request.action_type, ())
        if action not in allowed:
            return None, (
                f"action {action.value} is not valid for request type {request.action_type.value}"
            )

        request.status = HITLRequestStatus.DECIDED
        self._consumed_keys.add(request.idempotency_key)
        return request, "approved"

    # ── helpers ─────────────────────────────────────────────────────────

    @property
    def consumed_keys(self) -> frozenset[str]:
        """Set of burned idempotency keys (replay protection)."""
        return frozenset(self._consumed_keys)

    def expired_requests(
        self, requests: list[HITLApprovalRequest], now: datetime | None = None
    ) -> list[HITLApprovalRequest]:
        """Mark still-PENDING requests whose TTL elapsed as EXPIRED."""
        now = now or datetime.now(UTC)
        expired: list[HITLApprovalRequest] = []
        for req in requests:
            if req.status == HITLRequestStatus.PENDING and req.is_expired(now):
                req.status = HITLRequestStatus.EXPIRED
                expired.append(req)
        return expired
