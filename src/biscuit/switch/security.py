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
import sqlite3
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from biscuit.switch.types import (
    HITLApprovalRequest,
    HITLEventAction,
    HITLRequestStatus,
    SwitchActionType,
    canonical_json,
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


@dataclass(frozen=True)
class VerifiedProjectRoleGrant:
    """Trusted identity/role mapping resolved for one exact project."""

    contributor_id: str
    source_platform: str
    platform_id: str
    project_context: str
    role: str
    active: bool = True
    verified: bool = True
    revoked: bool = False


# The resolver must query a trusted contributor/grant store, not callback claims.
RoleGrantResolver = Callable[[str, str, str, str], Iterable[VerifiedProjectRoleGrant]]


def sign_body(secret: str, body: bytes) -> str:
    """Compute the ``sha256=<hex>`` HMAC signature over a raw callback body."""
    digest = hmac.new(secret.encode("utf-8"), body, "sha256").hexdigest()
    return f"sha256={digest}"


def _parse_signature(header_value: str | None) -> str | None:
    if header_value is None:
        return None
    match = _SIGNATURE_RE.match(header_value.strip())
    return match.group(1).lower() if match else None


def _parse_iso(ts_str: str) -> datetime:
    cleaned = ts_str.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class CallbackSecurity:
    """Validates Switch approval callbacks and prevents replayed actions."""

    def __init__(
        self,
        secret: str,
        tolerance_seconds: float = 300.0,
        role_grant_resolver: RoleGrantResolver | None = None,
        replay_db_path: str | None = None,
        allow_volatile_replay_for_tests: bool = False,
    ) -> None:
        self._secret = secret
        self.tolerance_seconds = tolerance_seconds
        self._role_grant_resolver = role_grant_resolver
        self._replay_db_path = replay_db_path
        self._allow_volatile_replay_for_tests = allow_volatile_replay_for_tests
        self._ledger_lock = threading.Lock()
        self._ledger: sqlite3.Connection | None = None
        if replay_db_path is not None:
            if replay_db_path == ":memory:" and not allow_volatile_replay_for_tests:
                raise ValueError("production callbacks require a durable replay_db_path")
            self._ledger = sqlite3.connect(replay_db_path, check_same_thread=False, isolation_level=None)
            self._ledger.execute(
                """CREATE TABLE IF NOT EXISTS consumed_switch_callbacks (
                    request_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    body_sha256 TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                )"""
            )
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

        if idempotency_key in self._consumed_keys or self._already_consumed(request.id, idempotency_key):
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

        # Never accept absent/malformed JSON claims: a valid HMAC alone does
        # not authenticate the approval's target, action or freshness.
        try:
            payload_data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False, "callback body must be valid UTF-8 JSON", body_sha256
        if not isinstance(payload_data, dict):
            return False, "callback body must be a JSON object", body_sha256

        ts_str = payload_data.get("timestamp")
        if not isinstance(ts_str, str) or not ts_str:
            return False, "signed callback timestamp is required", body_sha256
        try:
            cb_time = _parse_iso(ts_str)
            if abs((current - cb_time).total_seconds()) > self.tolerance_seconds:
                return False, "callback timestamp outside 5-minute freshness window", body_sha256
        except (ValueError, TypeError, OverflowError):
            return False, "malformed callback timestamp format", body_sha256

        body_req_id = payload_data.get("request_id")
        if body_req_id not in (request.id, request.switch_approval_id):
            return False, "signed callback request_id does not match approval request", body_sha256
        if payload_data.get("idempotency_key") != idempotency_key:
            return False, "signed callback idempotency key does not match", body_sha256
        body_payload_sha = payload_data.get("action_payload_sha256")
        if not isinstance(body_payload_sha, str) or body_payload_sha != request.action_payload_sha256:
            return False, "signed callback payload hash missing or does not match pinned approval", body_sha256
        if compute_sha256_hex(canonical_json(request.card_rendered_payload)) != request.action_payload_sha256:
            return False, "approval card was modified after the request was created", body_sha256
        for claim in ("action", "actor_contributor_id", "actor_platform_id", "actor_source_platform"):
            if not isinstance(payload_data.get(claim), str) or not payload_data[claim]:
                return False, f"signed callback {claim} is required", body_sha256

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

        # All fields below were required and authenticated by verify().
        payload_data = json.loads(body.decode("utf-8"))
        if payload_data["action"] != action.value:
            return None, "signed callback action does not match requested action"
        if payload_data["actor_contributor_id"] != actor_contributor_id:
            return None, "signed callback contributor does not match authenticated actor"
        if payload_data["actor_platform_id"] != actor_platform_id:
            return None, "signed callback platform actor ID does not match"
        if payload_data["actor_source_platform"] != actor_source_platform:
            return None, "signed callback source platform does not match"

        # Role grants are resolved from trusted storage for this precise
        # project, never inferred from caller-supplied role names.
        project = request.project_context or request.target_repository
        if not project or self._role_grant_resolver is None:
            return None, "verified project-specific role resolver is required"
        try:
            grants = list(self._role_grant_resolver(
                actor_source_platform, actor_platform_id, actor_contributor_id, project
            ))
        except Exception:  # noqa: BLE001 - fail closed on any trusted-resolver error
            return None, "could not verify the actor's project-specific role grants"
        qualified = [
            g for g in grants
            if g.contributor_id == actor_contributor_id
            and g.platform_id == actor_platform_id
            and g.source_platform == actor_source_platform
            and g.project_context == project
            and g.verified and g.active and not g.revoked
            and g.role in request.authorized_roles
        ]
        if not qualified:
            return None, f"actor {actor_contributor_id} does not hold an authorized maintainer role for {project}"
        if verified_actor_roles is not None and not any(
            g.role in verified_actor_roles for g in qualified
        ):
            return None, f"actor {actor_contributor_id} does not hold an authorized maintainer role for {project}"

        if self._ledger is None:
            return None, "durable callback replay ledger must be configured"

        allowed = _ACTION_COMPATIBILITY.get(request.action_type, ())
        if action not in allowed:
            return None, (
                f"action {action.value} is not valid for request type {request.action_type.value}"
            )

        # SQLite's UNIQUE constraints enforce exactly one consumption even
        # across different processes sharing the same on-disk database.
        with self._ledger_lock:
            try:
                self._ledger.execute(
                    "INSERT INTO consumed_switch_callbacks (request_id, idempotency_key, body_sha256, consumed_at) VALUES (?, ?, ?, ?)",
                    (request.id, request.idempotency_key, compute_sha256_hex(body.decode("utf-8")), datetime.now(UTC).isoformat()),
                )
            except sqlite3.IntegrityError:
                return None, "replayed callback: request already finalized (409 Conflict)"
            request.status = HITLRequestStatus.DECIDED
            self._consumed_keys.add(request.idempotency_key)
        return request, "approved"

    # ── helpers ─────────────────────────────────────────────────────────

    def _already_consumed(self, request_id: str, key: str) -> bool:
        if self._ledger is None:
            return False
        with self._ledger_lock:
            return self._ledger.execute(
                "SELECT 1 FROM consumed_switch_callbacks WHERE request_id = ? OR idempotency_key = ? LIMIT 1",
                (request_id, key),
            ).fetchone() is not None

    def close(self) -> None:
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None

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
