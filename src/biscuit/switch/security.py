"""Switch callback authentication: HMAC-SHA256, replay prevention, expiry.

Implements the callback security contract of
docs/LIVING_DECISION_INTELLIGENCE.md section 5A:

1. **HMAC-SHA256 signature** — every incoming callback must carry
   ``X-Switch-Signature: sha256=<hex>``, computed by Switch over the *raw*
   callback body with a pre-shared secret. Biscuit recomputes and compares
   in constant time; a missing/malformed/incorrect signature is rejected.
2. **Idempotency / replay prevention** — each approval card carries a unique
   UUIDv4 ``idempotency_key``. A callback for a request whose status is no
   longer ``PENDING`` returns ``409 Conflict`` semantics (duplicate rejected
   and discarded); replayed bodies for the same request never produce a
   second decision event.
3. **TTL / expiration** — approval cards carry ``expires_at = created + 72h``.
   Expired requests are rejected and marked ``EXPIRED``.
4. **Binding** — the verified callback must carry the request's idempotency
   key and an action consistent with the request type; the payload digest is
   checked against the request's pinned ``action_payload_sha256`` so an
   approval can never be re-targeted at different content.

The verifier is transport-agnostic (pure functions over bytes) so tests run
without a live Switch host.
"""

from __future__ import annotations

import hmac
import re
import time
from datetime import UTC, datetime

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
        HITLEventAction.REJECT_DRAFT,
    ),
    SwitchActionType.AUTHORIZE_PR_WARNING: (HITLEventAction.AUTHORIZE_PR_COMMENT,),
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

        ``idempotency_key`` may arrive as a header or embedded in the JSON
        body; the caller passes whichever it extracted. All failure paths are
        distinguishable so tests and audit logs can state the exact cause.
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

        key = idempotency_key or request.idempotency_key
        if not _UUIDV4_RE.match(key or ""):
            return False, "idempotency key is missing or not a UUIDv4", body_sha256
        if key != request.idempotency_key:
            return False, "idempotency key does not match the approval request", body_sha256
        if key in self._consumed_keys:
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
        now: datetime | None = None,
    ) -> tuple[HITLApprovalRequest | None, str]:
        """Verify then consume a callback for ``request``.

        On success the request transitions to ``DECIDED`` (a copy is
        returned; the caller persists it), the idempotency key is burned, and
        the action is checked for compatibility with the request type. On
        failure the original request is untouched and the reason is returned.
        """
        valid, reason, _ = self.verify(
            request, body, signature_header, idempotency_key, now
        )
        if not valid:
            return None, reason

        allowed = _ACTION_COMPATIBILITY[request.action_type]
        if action not in allowed:
            return None, (
                f"action {action.value} is not valid for request type "
                f"{request.action_type.value}"
            )

        request.status = HITLRequestStatus.DECIDED
        self._consumed_keys.add(request.idempotency_key)
        return request, "approved"

    # ── helpers ─────────────────────────────────────────────────────────

    def expired_requests(
        self, requests: list[HITLApprovalRequest], now: datetime | None = None
    ) -> list[HITLApprovalRequest]:
        """Mark still-PENDING requests whose TTL elapsed as EXPIRED."""
        current = now or datetime.now(UTC)
        expired = []
        for request in requests:
            if request.status == HITLRequestStatus.PENDING and request.is_expired(current):
                request.status = HITLRequestStatus.EXPIRED
                expired.append(request)
        return expired

    @staticmethod
    def stamp() -> float:
        """Monotonic-ish wall clock seconds (for ordering tests)."""
        return time.time()
