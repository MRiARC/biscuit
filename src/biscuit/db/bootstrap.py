"""Project role and provenanced-decision bootstrapping for the authoritative store.

The PR Diff Watchdog's 4-invariant authorization gate fails closed in a fresh
environment (``tp=0, fp=0, fn=5``) unless backed by:

1. Real ``ContributorProjectRoleGrant`` rows (BDFL / Core Maintainer /
   Working Group Chair) scoped to the decision's project, and
2. Provenanced decision records — ``source_threads`` → ``source_messages``
   → ``source_message_versions`` → ``evidence_spans`` →
   ``decision_alternatives`` with ``lifecycle_status = REJECTED`` plus
   ``ruling_evidence_id`` / ``ruling_authority`` / ``ruling_actor_id`` /
   ``ruling_actor_grant_id`` set, and the matching
   ``decision_alternative_ruling_signoffs``.

This module provides the bootstrap entrypoints that the ``biscuit db``
CLI command group wires up. All writes are transactional: a failure
rolls the entire import back so the store never lands in a half-seeded
state. The PostgreSQL triggers in ``triggers.sql`` enforce the same
provenance and quorum invariants server-side; on SQLite the same
invariants are enforced by the SQLModel CHECK constraints, so the
bootstrap path is exercisable in CI without a live PostgreSQL.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biscuit.models import (
    Contributor,
    ContributorProjectRole,
    ContributorProjectRoleGrant,
    ContributorProjectRoleRevocation,
    Decision,
    DecisionAlternative,
    DecisionAlternativeRulingSignoff,
    EvidenceRole,
    EvidenceSpan,
    EvidenceStance,
    MessageEditState,
    PlatformType,
    ProposalLifecycle,
    RulingAuthority,
    SourceMessage,
    SourceMessageVersion,
    SourceThread,
    VerificationStatus,
)


def upsert_contributor(
    session: Session,
    *,
    source_platform: str,
    platform_user_id: str,
    canonical_name: str,
    author_handle: str,
    email_prefix: str | None = None,
) -> Contributor:
    """Insert or update a Contributor by (source_platform, platform_user_id)."""
    existing = session.exec(
        select(Contributor).where(
            Contributor.source_platform == source_platform,
            Contributor.platform_user_id == platform_user_id,
        )
    ).first()
    if existing is not None:
        return existing
    contributor = Contributor(
        source_platform=source_platform,
        platform_user_id=platform_user_id,
        canonical_name=canonical_name,
        author_handle=author_handle,
        email_prefix=email_prefix,
    )
    session.add(contributor)
    session.flush()
    return contributor


def grant_project_role(
    session: Session,
    *,
    contributor: Contributor,
    project_context: str,
    role: ContributorProjectRole,
    verification_source_url: str,
    verified_by: Contributor,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> ContributorProjectRoleGrant:
    """Create an append-only project-scoped role grant for a contributor.

    The grant is the authority token the watchdog gate's invariant 2
    inspects: a REJECTED alternative's ruling_actor_grant_id must resolve
    to one of these grants, scoped to the decision's project, with an
    active validity window and no matching revocation.
    """
    if isinstance(role, ContributorProjectRole):
        role_value = role.value
    else:
        role_value = str(role)
    now = datetime.now(UTC)
    grant = ContributorProjectRoleGrant(
        contributor_id=contributor.id,
        project_context=project_context,
        role=role_value,
        verification_source_url=verification_source_url,
        verified_by_id=verified_by.id,
        verified_at=now,
        valid_from=valid_from or now,
        valid_until=valid_until,
    )
    session.add(grant)
    session.flush()
    return grant


def revoke_project_role(
    session: Session,
    *,
    grant: ContributorProjectRoleGrant,
    revoked_by: Contributor,
    reason: str,
) -> ContributorProjectRoleRevocation:
    """Record an append-only revocation of a role grant."""
    if not reason or not reason.strip():
        raise ValueError("Revocation reason must not be empty")
    revocation = ContributorProjectRoleRevocation(
        grant_id=grant.id,
        revoked_by_id=revoked_by.id,
        reason=reason.strip(),
    )
    session.add(revocation)
    session.flush()
    return revocation


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def import_provenanced_decision(
    session: Session,
    *,
    project_context: str,
    thread: dict[str, Any],
    decision: dict[str, Any],
    alternative: dict[str, Any],
    ruling: dict[str, Any],
    proposer: Contributor,
    ruling_actor: Contributor,
    ruling_actor_grant: ContributorProjectRoleGrant,
    signoffs: list[tuple[Contributor, ContributorProjectRoleGrant, dict[str, Any]]],
    bootstrap_actors: dict[str, Contributor] | None = None,
) -> tuple[Decision, DecisionAlternative, EvidenceSpan]:
    """Import one fully-provenanced rejected decision in a single transaction.

    The caller supplies every required piece of evidence as kwargs so the
    function never invents provenance. On PostgreSQL the triggers in
    ``triggers.sql`` enforce the VERIFIED_EXACT + quorum contract server-side;
    on SQLite the SQLModel CHECK constraints enforce the structural shape.

    ``thread`` shape::

        {
            "source_platform": "DISCOURSE",
            "external_thread_id": "1234",
            "title": "PEP 505: ?-aware default-aware operators",
            "canonical_url": "https://discuss.python.org/t/pep-505/505",
        }

    ``decision`` shape::

        {"title": "...", "context_summary": "..."}

    ``alternative`` shape::

        {"proposal_text": "x.y?.z", "rejection_summary": "...", "rejection_category": "SYNTAX_AMBIGUITY"}

    ``ruling`` shape::

        {
            "raw_content": "<the exact source message text>",
            "exact_quote": "<substring of raw_content>",
            "start_char": <int>, "end_char": <int>,
            "author_handle": "gvanrossum",
            "permalink": "https://...",
        }

    ``signoffs`` is a list of ``(contributor, grant, evidence_dict)`` tuples
    where ``evidence_dict`` has the same shape as ``ruling`` (each sign-off
    must cite its own VERIFIED_EXACT RULING span for the same alternative).
    """
    platform_str = thread["source_platform"]
    if isinstance(platform_str, PlatformType):
        platform_str = platform_str.value
    source_thread = SourceThread(
        source_platform=platform_str,
        project_context=project_context,
        external_thread_id=str(thread["external_thread_id"]),
        title=str(thread["title"]),
        canonical_url=str(thread["canonical_url"]),
    )
    session.add(source_thread)
    session.flush()

    original_timestamp = _parse_dt(ruling.get("original_timestamp")) or datetime.now(UTC)
    message = SourceMessage(
        thread_id=source_thread.id,
        external_message_id=str(ruling.get("external_message_id", source_thread.external_thread_id)),
        author_id=proposer.id,
        original_timestamp=original_timestamp,
        canonical_permalink=str(ruling.get("permalink", thread["canonical_url"])),
    )
    session.add(message)
    session.flush()

    raw_content = str(ruling["raw_content"])
    version = SourceMessageVersion(
        message_id=message.id,
        version_number=1,
        raw_content=raw_content,
        content_hash=_sha256_hex(raw_content),
        edit_state=MessageEditState.ORIGINAL.value,
        is_current=True,
    )
    session.add(version)
    session.flush()

    decision_obj = Decision(
        source_thread_id=source_thread.id,
        title=str(decision["title"]),
        context_summary=str(decision["context_summary"]),
        resolution_status="RESOLVED",
        resolution_type="REJECTED",
    )
    session.add(decision_obj)
    session.flush()

    authority_str = alternative.get("ruling_authority", "CORE_MAINTAINER_CONSENSUS")
    if isinstance(authority_str, RulingAuthority):
        authority_str = authority_str.value
    rejection_category = alternative.get("rejection_category")

    # Insert as PROPOSED first: chk_rejected_requires_ruling prevents inserting
    # REJECTED without ruling_evidence_id, and the evidence span requires a
    # non-null alternative_id (chk_evidence_target_exactly_one). On PostgreSQL
    # the audited transition_alternative_lifecycle() function moves it to
    # REJECTED after the ruling metadata is wired; on SQLite we UPDATE directly
    # since the CHECK constraints are satisfied once ruling_evidence_id is set.
    alt = DecisionAlternative(
        decision_id=decision_obj.id,
        proposal_text=str(alternative["proposal_text"]),
        lifecycle_status=ProposalLifecycle.PROPOSED.value,
        rejection_category=rejection_category,
        rejection_summary=alternative.get("rejection_summary"),
        proposed_by_id=proposer.id,
    )
    session.add(alt)
    session.flush()

    ruling_span = _create_evidence_span(
        session,
        version=version,
        message=message,
        alternative_placeholder=alt.id,
        ruling=ruling,
    )
    session.flush()

    alt.lifecycle_status = ProposalLifecycle.REJECTED.value
    alt.ruling_evidence_id = ruling_span.id
    alt.ruling_authority = authority_str
    alt.ruling_actor_id = ruling_actor.id
    alt.ruling_actor_grant_id = ruling_actor_grant.id
    session.flush()

    for contributor, grant, evidence in signoffs:
        signoff_span = _create_evidence_span(
            session,
            version=version,
            message=message,
            alternative_placeholder=alt.id,
            ruling=evidence,
        )
        signoff = DecisionAlternativeRulingSignoff(
            alternative_id=alt.id,
            contributor_id=contributor.id,
            project_role_grant_id=grant.id,
            evidence_span_id=signoff_span.id,
        )
        session.add(signoff)

    session.flush()
    return decision_obj, alt, ruling_span


def _create_evidence_span(
    session: Session,
    *,
    version: SourceMessageVersion,
    message: SourceMessage,
    alternative_placeholder: uuid.UUID | None,
    ruling: dict[str, Any],
) -> EvidenceSpan:
    """Create one VERIFIED_EXACT RULING evidence span bound to a version.

    Verifies the substring + SHA-256 contract the hard way so SQLite
    (which lacks the ``verify_evidence_span_exactness`` trigger) still
    cannot store a mismatched span.
    """
    raw_content = version.raw_content
    exact_quote = str(ruling["exact_quote"])
    start_char = int(ruling["start_char"])
    end_char = int(ruling["end_char"])
    if start_char < 0 or end_char <= start_char or end_char > len(raw_content):
        raise ValueError("Evidence offsets exceed raw_content bounds")
    actual = raw_content[start_char:end_char]
    if actual != exact_quote:
        raise ValueError(
            "Evidence exact_quote does not equal raw_content[start_char:end_char]"
        )
    expected_hash = _sha256_hex(exact_quote)
    if expected_hash != _sha256_hex(actual):
        raise ValueError("Evidence SHA-256 mismatch")
    span = EvidenceSpan(
        source_message_version_id=version.id,
        alternative_id=alternative_placeholder,
        exact_quote=exact_quote,
        start_char=start_char,
        end_char=end_char,
        content_hash=expected_hash,
        evidence_role=EvidenceRole.RULING.value,
        stance=EvidenceStance.OBJECT.value,
        verification_status=VerificationStatus.VERIFIED_EXACT.value,
    )
    session.add(span)
    session.flush()
    return span


def load_bootstrap_fixture(
    session: Session, fixture_path: Path
) -> dict[str, Any]:
    """Load a JSON bootstrap fixture (project roles + provenanced decisions).

    The fixture format is documented in
    ``tests/fixtures/bootstrap/README.md``. Returns a summary dict
    ``{"contributors": N, "grants": N, "decisions": N}`` for CLI reporting.
    """
    import json

    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("Bootstrap fixture must be a JSON object")
    summary: dict[str, Any] = {"contributors": 0, "grants": 0, "decisions": 0}
    contributor_cache: dict[str, Contributor] = {}

    def _contributor(handle: str) -> Contributor:
        if handle in contributor_cache:
            return contributor_cache[handle]
        spec = data.get("contributors", {}).get(handle)
        if spec is None:
            raise ValueError(f"Bootstrap fixture references unknown contributor '{handle}'")
        c = upsert_contributor(
            session,
            source_platform=spec["source_platform"],
            platform_user_id=spec["platform_user_id"],
            canonical_name=spec["canonical_name"],
            author_handle=spec["author_handle"],
            email_prefix=spec.get("email_prefix"),
        )
        contributor_cache[handle] = c
        summary["contributors"] += 1
        return c

    for grant_spec in data.get("role_grants", []):
        contributor = _contributor(grant_spec["contributor"])
        verifier = _contributor(grant_spec["verified_by"])
        grant_project_role(
            session,
            contributor=contributor,
            project_context=grant_spec["project_context"],
            role=ContributorProjectRole(grant_spec["role"]),
            verification_source_url=grant_spec["verification_source_url"],
            verified_by=verifier,
            valid_from=_parse_dt(grant_spec.get("valid_from")),
            valid_until=_parse_dt(grant_spec.get("valid_until")),
        )
        summary["grants"] += 1

    for dec_spec in data.get("decisions", []):
        project_context = dec_spec["project_context"]
        proposer = _contributor(dec_spec["proposer"])
        ruling_actor = _contributor(dec_spec["ruling_actor"])
        ruling_actor_grant_id = dec_spec["ruling_actor_grant"]
        grant = session.exec(
            select(ContributorProjectRoleGrant).where(
                ContributorProjectRoleGrant.id == uuid.UUID(ruling_actor_grant_id)
            )
        ).first()
        if grant is None:
            raise ValueError(
                f"ruling_actor_grant {ruling_actor_grant_id} not found; "
                "create the role grant before importing the decision"
            )
        signoff_specs = []
        for s in dec_spec.get("signoffs", []):
            s_contributor = _contributor(s["contributor"])
            s_grant = session.exec(
                select(ContributorProjectRoleGrant).where(
                    ContributorProjectRoleGrant.id == uuid.UUID(s["grant"])
                )
            ).first()
            if s_grant is None:
                raise ValueError(f"signoff grant {s['grant']} not found")
            signoff_specs.append((s_contributor, s_grant, s["evidence"]))
        import_provenanced_decision(
            session,
            project_context=project_context,
            thread=dec_spec["thread"],
            decision=dec_spec["decision"],
            alternative=dec_spec["alternative"],
            ruling=dec_spec["ruling"],
            proposer=proposer,
            ruling_actor=ruling_actor,
            ruling_actor_grant=grant,
            signoffs=signoff_specs,
            bootstrap_actors=contributor_cache,
        )
        summary["decisions"] += 1

    return summary


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


__all__ = [
    "grant_project_role",
    "import_provenanced_decision",
    "load_bootstrap_fixture",
    "revoke_project_role",
    "upsert_contributor",
]
