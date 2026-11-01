"""Database-backed decision store for the PR Diff Watchdog.

This module wires the watchdog's pure ``DecisionStore`` Protocol
(:mod:`biscuit.watchdog.types`) to the authoritative PostgreSQL relational
store defined by the SQLModel tables in :mod:`biscuit.models`. It is the
Stream-1/4 integration point required by Issue #20's "watchdog warning
quorum with real data" acceptance gate: the 4-invariant authorization gate
cannot authorize a warning unless a verified human ruling exists, and that
ruling is only authoritative when it comes from the production DB, not the
CLI's local SQLite cache.

The store is read-side only: it materialises the watchdog's frozen
dataclass records from live SQLModel rows. Writes (audit-log persistence,
role bootstrapping, provenanced decision import) live in
:mod:`biscuit.db.bootstrap` and :func:`persist_watchdog_audit_log` below.

The adapter respects the stream-4 ownership boundary documented in
``docs/LIVING_DECISION_INTELLIGENCE.md``: it never imports the watchdog's
private modules, only the public Protocol types re-exported from
``biscuit.watchdog``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from biscuit.models import (
    CodeAnchor,
    Contributor,
    ContributorProjectRoleGrant,
    ContributorProjectRoleRevocation,
    Decision,
    DecisionAlternative,
    DecisionAlternativeRulingSignoff,
    EvidenceRole,
    EvidenceSpan,
    ExecutionMode,
    ProposalLifecycle,
    PrWatchdogAction,
    PrWatchdogAuditLog,
    PrWatchdogVerdict,
    SourceMessage,
    SourceMessageVersion,
    SourceThread,
    VerificationStatus,
)
from biscuit.watchdog.types import (
    ACTIVE_ANCHOR_STATES,
    CodeAnchorRecord,
    DecisionAlternativeRecord,
    DecisionRecord,
    DecisionStore,
    EvidenceSpanRecord,
    SignoffRecord,
)

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    from biscuit.watchdog.types import AuditLogEntry


def _is_active_grant(
    grant: ContributorProjectRoleGrant,
    revocation_grant_ids: frozenset[uuid.UUID],
) -> bool:
    """An active grant: not revoked, and within its validity window."""
    from datetime import UTC, datetime

    if grant.id in revocation_grant_ids:
        return False
    now = datetime.now(UTC)
    if grant.valid_from > now:
        return False
    return not (grant.valid_until is not None and grant.valid_until <= now)


def _to_signoff_record(
    signoff: DecisionAlternativeRulingSignoff,
    grant_by_id: dict[uuid.UUID, ContributorProjectRoleGrant],
    revoked_grant_ids: frozenset[uuid.UUID],
) -> SignoffRecord:
    grant = grant_by_id.get(signoff.project_role_grant_id)
    role = grant.role if grant is not None else ""
    project_context = grant.project_context if grant is not None else ""
    active = (
        grant is not None
        and _is_active_grant(grant, revoked_grant_ids)
    )
    return SignoffRecord(
        contributor_id=str(signoff.contributor_id),
        role=role,
        project_context=project_context,
        active=active,
    )


def _to_anchor_record(
    anchor: CodeAnchor,
    project_context: str,
) -> CodeAnchorRecord:
    """Materialise a watchdog ``CodeAnchorRecord`` from a SQLModel row.

    ``head_file_path`` is not a column on ``code_anchors``; the lineage-resolved
    modern path lives in the latest ``code_anchor_lineage`` row. For the
    DB-backed store we treat ``historical_file_path`` as the active path when
    the anchor is CURRENT (the row was re-recorded after a migration) and leave
    lineage resolution to the Stream-2 grounding service. ``lineage_resolved``
    defaults to True for CURRENT/MIGRATED anchors and False otherwise, matching
    invariant 3's abstention semantics.
    """
    from biscuit.watchdog.types import AnchorState as WDAnchorState

    state_str = anchor.anchor_state if isinstance(anchor.anchor_state, str) else anchor.anchor_state.value
    wd_state = WDAnchorState(state_str)
    return CodeAnchorRecord(
        id=str(anchor.id),
        alternative_id=str(anchor.alternative_id),
        repository_url=anchor.repository_url,
        language=anchor.language,
        symbol_type=str(anchor.symbol_type) if not isinstance(anchor.symbol_type, str) else anchor.symbol_type,
        qualified_symbol_name=anchor.qualified_symbol_name,
        historical_file_path=anchor.historical_file_path,
        anchor_state=wd_state,
        head_file_path=anchor.historical_file_path if wd_state in ACTIVE_ANCHOR_STATES else None,
        ast_structural_fingerprint=anchor.ast_structural_fingerprint,
        lineage_resolved=wd_state in ACTIVE_ANCHOR_STATES,
        mapping_confidence=anchor.mapping_confidence,
    )


def _to_evidence_record(
    span: EvidenceSpan,
    author: Contributor | None,
    version: SourceMessageVersion | None,
    thread_url: str,
) -> EvidenceSpanRecord:
    role_str = span.evidence_role if isinstance(span.evidence_role, str) else span.evidence_role.value
    status_str = (
        span.verification_status
        if isinstance(span.verification_status, str)
        else span.verification_status.value
    )
    return EvidenceSpanRecord(
        id=str(span.id),
        alternative_id=str(span.alternative_id) if span.alternative_id else "",
        evidence_role=EvidenceRole(role_str),
        verification_status=VerificationStatus(status_str),
        exact_quote=span.exact_quote,
        start_char=span.start_char,
        end_char=span.end_char,
        author_handle=author.author_handle if author else "",
        author_contributor_id=str(author.id) if author else "",
        permalink=thread_url,
        content_hash=span.content_hash,
    )


def _to_alternative_record(
    alt: DecisionAlternative,
    decision: Decision,
    thread: SourceThread,
    anchors: list[CodeAnchor],
    ruling_span: EvidenceSpan | None,
    ruling_author: Contributor | None,
    ruling_version: SourceMessageVersion | None,
    signoffs: list[DecisionAlternativeRulingSignoff],
    grant_by_id: dict[uuid.UUID, ContributorProjectRoleGrant],
    revoked_grant_ids: frozenset[uuid.UUID],
) -> DecisionAlternativeRecord:
    lifecycle_str = (
        alt.lifecycle_status
        if isinstance(alt.lifecycle_status, str)
        else alt.lifecycle_status.value
    )
    authority = (
        alt.ruling_authority
        if isinstance(alt.ruling_authority, str) or alt.ruling_authority is None
        else alt.ruling_authority.value
        if alt.ruling_authority
        else None
    )
    anchor_records = tuple(_to_anchor_record(a, project_context=thread.project_context) for a in anchors)
    signoff_records = tuple(
        _to_signoff_record(s, grant_by_id, revoked_grant_ids) for s in signoffs
    )
    return DecisionAlternativeRecord(
        id=str(alt.id),
        decision_id=str(alt.decision_id),
        proposal_text=alt.proposal_text,
        lifecycle_status=ProposalLifecycle(lifecycle_str),
        rejection_summary=alt.rejection_summary,
        rejection_category=(
            alt.rejection_category
            if isinstance(alt.rejection_category, str) or alt.rejection_category is None
            else alt.rejection_category.value
            if alt.rejection_category
            else None
        ),
        embedding=None,  # vector retrieval handled by the retriever via injected embedder
        ruling_evidence_id=str(alt.ruling_evidence_id) if alt.ruling_evidence_id else None,
        ruling_authority=authority,
        ruling_actor_id=str(alt.ruling_actor_id) if alt.ruling_actor_id else None,
        project_context=thread.project_context,
        ruling_signoffs=signoff_records,
        anchors=anchor_records,
    )


def _to_decision_record(decision: Decision, thread: SourceThread) -> DecisionRecord:
    return DecisionRecord(
        id=str(decision.id),
        title=decision.title,
        context_summary=decision.context_summary,
        source_thread_url=thread.canonical_url,
        project_context=thread.project_context,
    )


class DatabaseDecisionStore:
    """``DecisionStore`` Protocol implementation backed by the SQLModel relational store.

    Reads ``decisions``, ``decision_alternatives``, ``evidence_spans``,
    ``code_anchors``, ``decision_alternative_ruling_signoffs``,
    ``contributor_project_role_grants`` (and their revocations) and
    ``source_threads`` to materialise the frozen dataclass records the
    watchdog gate consumes. No data is mutated; the audit log is written
    separately via :func:`persist_watchdog_audit_log`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def all_alternatives(self) -> list[DecisionAlternativeRecord]:
        rows = list(self._session.scalars(select(DecisionAlternative)).all())
        return [self._materialise_alternative(alt) for alt in rows]

    def get_alternative(self, alternative_id: str) -> DecisionAlternativeRecord | None:
        try:
            aid = uuid.UUID(alternative_id)
        except (ValueError, AttributeError, TypeError):
            return None
        alt = self._session.get(DecisionAlternative, aid)
        if alt is None:
            return None
        return self._materialise_alternative(alt)

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        try:
            did = uuid.UUID(decision_id)
        except (ValueError, AttributeError, TypeError):
            return None
        decision = self._session.get(Decision, did)
        if decision is None:
            return None
        thread = self._session.get(SourceThread, decision.source_thread_id)
        if thread is None:
            return None
        return _to_decision_record(decision, thread)

    def get_evidence_span(self, span_id: str) -> EvidenceSpanRecord | None:
        try:
            sid = uuid.UUID(span_id)
        except (ValueError, AttributeError, TypeError):
            return None
        span = self._session.get(EvidenceSpan, sid)
        if span is None or span.alternative_id is None:
            return None
        author, version, thread_url = self._load_evidence_context(span)
        return _to_evidence_record(span, author, version, thread_url)

    # ───────────────────────────────────────────────────────────────────────
    # Internal materialisation helpers
    # ───────────────────────────────────────────────────────────────────────

    def _materialise_alternative(
        self, alt: DecisionAlternative
    ) -> DecisionAlternativeRecord:
        decision = self._session.get(Decision, alt.decision_id)
        if decision is None:
            raise ValueError(
                f"decision_alternatives {alt.id} references missing decision {alt.decision_id}"
            )
        thread = self._session.get(SourceThread, decision.source_thread_id)
        if thread is None:
            raise ValueError(
                f"decision {decision.id} references missing source_thread {decision.source_thread_id}"
            )
        anchors = list(
            self._session.scalars(
                select(CodeAnchor).where(CodeAnchor.alternative_id == alt.id)
            ).all()
        )
        ruling_span: EvidenceSpan | None = None
        ruling_author: Contributor | None = None
        ruling_version: SourceMessageVersion | None = None
        if alt.ruling_evidence_id is not None:
            ruling_span = self._session.get(EvidenceSpan, alt.ruling_evidence_id)
            if ruling_span is not None:
                ruling_author, ruling_version, _ = self._load_evidence_context(ruling_span)
        signoffs = list(
            self._session.scalars(
                select(DecisionAlternativeRulingSignoff).where(
                    DecisionAlternativeRulingSignoff.alternative_id == alt.id
                )
            ).all()
        )
        grant_ids = {s.project_role_grant_id for s in signoffs}
        # The ruling actor's own grant is the authority for a BDFL ruling.
        # The PostgreSQL trigger (enforce_authoritative_alternative_rejection)
        # checks actor_role == 'BDFL' directly from the grant without requiring
        # a separate signoff row; mirror that here so the watchdog's quorum
        # check sees the BDFL as a sign-off when no explicit row exists.
        ruling_actor_grant: ContributorProjectRoleGrant | None = None
        if alt.ruling_actor_grant_id is not None:
            ruling_actor_grant = self._session.get(
                ContributorProjectRoleGrant, alt.ruling_actor_grant_id
            )
            if ruling_actor_grant is not None:
                grant_ids = grant_ids | {ruling_actor_grant.id}
        grants: list[ContributorProjectRoleGrant] = (
            list(
                self._session.scalars(
                    select(ContributorProjectRoleGrant).where(
                        ContributorProjectRoleGrant.id.in_(grant_ids)
                    )
                ).all()
            )
            if grant_ids
            else []
        )
        grant_by_id = {g.id: g for g in grants}
        revoked_grant_ids = self._revoked_grant_ids(frozenset(grant_ids))
        # Synthesize a sign-off from the ruling actor's grant when one is not
        # already present in the table. This is exactly the BDFL case: the
        # ruling_actor_grant IS the authority, so the watchdog's quorum check
        # must see it as an active sign-off.
        if (
            ruling_actor_grant is not None
            and alt.ruling_actor_id is not None
            and not any(
                s.contributor_id == alt.ruling_actor_id
                and s.project_role_grant_id == ruling_actor_grant.id
                for s in signoffs
            )
        ):
            from biscuit.models import DecisionAlternativeRulingSignoff as SignoffModel

            synthetic = SignoffModel(
                alternative_id=alt.id,
                contributor_id=alt.ruling_actor_id,
                project_role_grant_id=ruling_actor_grant.id,
                evidence_span_id=alt.ruling_evidence_id,
            )
            signoffs = [*signoffs, synthetic]
        return _to_alternative_record(
            alt=alt,
            decision=decision,
            thread=thread,
            anchors=anchors,
            ruling_span=ruling_span,
            ruling_author=ruling_author,
            ruling_version=ruling_version,
            signoffs=signoffs,
            grant_by_id=grant_by_id,
            revoked_grant_ids=revoked_grant_ids,
        )

    def _load_evidence_context(
        self, span: EvidenceSpan
    ) -> tuple[Contributor | None, SourceMessageVersion | None, str]:
        version: SourceMessageVersion | None = None
        author: Contributor | None = None
        thread_url = ""
        if span.source_message_version_id is not None:
            version = self._session.get(SourceMessageVersion, span.source_message_version_id)
            if version is not None:
                message = self._session.get(SourceMessage, version.message_id)
                if message is not None:
                    author = self._session.get(Contributor, message.author_id) if message.author_id else None
                    thread = self._session.get(SourceThread, message.thread_id)
                    if thread is not None:
                        thread_url = thread.canonical_url
        return author, version, thread_url

    def _revoked_grant_ids(
        self, grant_ids: frozenset[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        if not grant_ids:
            return frozenset()
        rows = list(self._session.scalars(
            select(ContributorProjectRoleRevocation.grant_id).where(
                ContributorProjectRoleRevocation.grant_id.in_(grant_ids)
            )
        ).all())
        return frozenset(rows)


# Static type check: DatabaseDecisionStore satisfies the DecisionStore Protocol.
_: DecisionStore = DatabaseDecisionStore.__new__(DatabaseDecisionStore)  # type: ignore[abstract]


def persist_watchdog_audit_log(
    session: Session,
    entry: AuditLogEntry,
) -> PrWatchdogAuditLog:
    """Write one ``AuthorizationResult`` audit entry to the append-only DB table.

    The trigger ``trg_pr_watchdog_audit_append_only`` (see
    ``db/migrations/triggers.sql``) rejects UPDATE/DELETE; this function only
    ever INSERTs.
    """
    verdict_str = (
        entry.authorization_verdict
        if isinstance(entry.authorization_verdict, str)
        else entry.authorization_verdict.value
    )
    action_str = (
        entry.action_taken
        if isinstance(entry.action_taken, str)
        else entry.action_taken.value
    )
    execution_mode_str = (
        entry.execution_mode
        if isinstance(entry.execution_mode, str)
        else entry.execution_mode.value
    )
    row = PrWatchdogAuditLog(
        repository_url=entry.repository_url,
        pr_number=entry.pr_number,
        commit_sha=entry.commit_sha,
        execution_mode=ExecutionMode(execution_mode_str),
        candidate_alternative_ids=list(entry.candidate_alternative_ids),
        evaluated_similarity=entry.evaluated_similarity,
        invariants_satisfied=entry.invariants_satisfied,
        authorization_verdict=PrWatchdogVerdict(verdict_str),
        action_taken=PrWatchdogAction(action_str),
        comment_payload=entry.comment_payload,
    )
    session.add(row)
    session.flush()
    return row


__all__ = [
    "DatabaseDecisionStore",
    "persist_watchdog_audit_log",
]
