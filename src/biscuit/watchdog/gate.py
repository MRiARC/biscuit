"""The 4-invariant Warning Authorization Gate and audit logging.

``WarningAuthorizationGate`` implements Step 2 of the PR watchdog pipeline
(docs/CODE_GROUNDING_AND_AST.md, section 7; docs/TWO_PILLAR_ARCHITECTURE.md,
section 6). Candidate retrieval only generates hypotheses; a public warning
is authorized **if and only if ALL FOUR invariants hold** for one candidate:

1. **Decision State** — ``decision_alternatives.lifecycle_status == REJECTED``.
   Disputed, unresolved or merely objected-to alternatives always abstain.
2. **Authoritative Ruling** — ``ruling_evidence_id`` resolves to a
   ``VERIFIED_EXACT`` ``RULING`` span linked to this exact alternative, and
   the ruling authority is a verified human quorum (BDFL; Core Maintainer
   consensus of the ruling author plus one distinct verified Core Maintainer;
   Working Group Chair plus one distinct verified member). A ``MODEL_INFERRED``
   authority never authorizes a warning.
3. **Alternative Code Anchor** — the PR diff touches an active (CURRENT or
   MIGRATED) ``code_anchor`` bound directly to THIS alternative, with
   unambiguous parent-aware lineage.
4. **AST Structural Equivalence** — the added code contains a masked
   normalized-AST subtree whose fingerprint equals the rejected construct's
   anchor fingerprint. File or symbol overlap alone is insufficient: a
   ternary-operator refactor in the same file must abstain.

Any failure (including parser errors, unsupported languages, ambiguous
lineage or missing evidence) causes **ABSTAIN**: a silent audit-log row and
zero developer spam.

Execution modes (docs/CODE_GROUNDING_AND_AST.md, section 8): the gate runs
in ``AUDIT_DRY_RUN`` by default; ``INTERACTIVE_HITL`` routes authorized
warnings to a Switch maintainer card; ``PRODUCTION_COMMENT`` publishes.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime

from biscuit.watchdog.formatter import PRCommentFormatter
from biscuit.watchdog.types import (
    ACTIVE_ANCHOR_STATES,
    BDFL_ROLE,
    CORE_MAINTAINER_ROLE,
    WG_CHAIR_ROLE,
    WG_MEMBER_ROLE,
    AuditLogEntry,
    AuthorizationResult,
    CandidateEvaluation,
    CandidateMatch,
    CodeAnchorRecord,
    DecisionAlternativeRecord,
    DecisionStore,
    EvidenceRole,
    ExecutionMode,
    InvariantCheck,
    InvariantKey,
    PRDiff,
    ProposalLifecycle,
    SignoffRecord,
    WatchdogAction,
    WatchdogVerdict,
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only in-memory sink for ``pr_watchdog_audit_log`` rows."""

    def __init__(self) -> None:
        self._entries: list[AuditLogEntry] = []

    def append(self, result: AuthorizationResult) -> AuditLogEntry:
        entry = result.to_audit_entry()
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> tuple[AuditLogEntry, ...]:
        return tuple(self._entries)


class WarningAuthorizationGate:
    """Evaluates the four non-negotiable invariants for PR diff candidates."""

    def __init__(
        self,
        store: DecisionStore,
        formatter: PRCommentFormatter | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._store = store
        self._formatter = formatter or PRCommentFormatter()
        self.audit_log = audit_log or AuditLog()

    def evaluate(
        self,
        pr_diff: PRDiff,
        candidates: list[CandidateMatch],
        repository_url: str = "",
        pr_number: int = 0,
        commit_sha: str = "",
        execution_mode: ExecutionMode = ExecutionMode.AUDIT_DRY_RUN,
        checked_at: str | None = None,
    ) -> AuthorizationResult:
        """Run all four invariants for every candidate and produce a verdict.

        All four invariants are evaluated (and recorded) for each candidate —
        even after one fails — so the audit trail shows exactly which proof
        was missing. The result is appended to the audit log before being
        returned; in every mode the watchdog's default action is a silent
        audit row, never an unapproved public comment.
        """
        checked_at = checked_at or _utc_now_iso()
        evaluations = [
            self._evaluate_candidate(pr_diff, match)
            for match in candidates
            if self._store.get_alternative(match.alternative_id) is not None
        ]

        authorized = [e for e in evaluations if e.authorized]
        verdict = self._overall_verdict(evaluations, authorized, pr_diff)

        action = self._action_for(verdict, execution_mode)
        comment_markdown: str | None = None
        comment_payload: dict | None = None
        hitl_card: dict | None = None

        if authorized:
            best = authorized[0]
            alternative = self._store.get_alternative(best.alternative_id)
            assert alternative is not None
            decision = self._store.get_decision(alternative.decision_id)
            span = (
                self._store.get_evidence_span(alternative.ruling_evidence_id)
                if alternative.ruling_evidence_id
                else None
            )
            anchor = self._touched_active_anchor(pr_diff, alternative)
            target_line = 1
            if anchor is not None:
                for f in pr_diff.files:
                    if (
                        f.path == anchor.current_path or f.path == anchor.historical_file_path
                    ) and f.hunks:
                        target_line = f.hunks[0].new_start
                        break
            comment_payload = self._formatter.build_comment_payload(
                alternative=alternative,
                decision=decision,
                anchor=anchor,
                ruling_span=span,
                execution_mode=execution_mode,
                similarity=best.similarity,
                pr_number=pr_number,
                repository_url=repository_url,
                commit_sha=commit_sha,
                line=target_line,
                side="RIGHT",
            )
            comment_markdown = comment_payload["body"]
            if execution_mode == ExecutionMode.INTERACTIVE_HITL:
                hitl_card = self._hitl_card_stub(
                    best, comment_payload, repository_url, pr_number, commit_sha
                )

        result = AuthorizationResult(
            repository_url=repository_url,
            pr_number=pr_number,
            commit_sha=commit_sha,
            checked_at=checked_at,
            execution_mode=execution_mode,
            verdict=verdict,
            action_taken=action,
            candidate_evaluations=tuple(evaluations),
            invariants_satisfied=bool(authorized),
            comment_markdown=comment_markdown,
            comment_payload=comment_payload,
            hitl_card_payload=hitl_card,
            authorized_alternative_ids=tuple(e.alternative_id for e in authorized),
        )
        self.audit_log.append(result)
        return result

    # ── invariant evaluation ────────────────────────────────────────────

    def _evaluate_candidate(self, pr_diff: PRDiff, match: CandidateMatch) -> CandidateEvaluation:
        alternative = self._store.get_alternative(match.alternative_id)
        assert alternative is not None
        checks = (
            self._check_decision_state(alternative),
            self._check_authoritative_ruling(alternative),
            self._check_alternative_code_anchor(pr_diff, alternative),
            self._check_ast_structural_equivalence(pr_diff, alternative),
        )
        return CandidateEvaluation(
            alternative_id=alternative.id,
            proposal_text=alternative.proposal_text,
            similarity=match.similarity,
            invariant_checks=checks,
            authorized=all(check.satisfied for check in checks),
            matched_anchor_ids=match.matched_anchor_ids,
        )

    @staticmethod
    def _check_decision_state(alternative: DecisionAlternativeRecord) -> InvariantCheck:
        satisfied = alternative.lifecycle_status == ProposalLifecycle.REJECTED
        detail = (
            "lifecycle_status is REJECTED"
            if satisfied
            else f"lifecycle_status is {alternative.lifecycle_status.value}, not REJECTED"
        )
        return InvariantCheck(InvariantKey.DECISION_STATE, satisfied, detail)

    def _check_authoritative_ruling(self, alternative: DecisionAlternativeRecord) -> InvariantCheck:
        key = InvariantKey.AUTHORITATIVE_RULING

        span = (
            self._store.get_evidence_span(alternative.ruling_evidence_id)
            if alternative.ruling_evidence_id
            else None
        )
        if span is None:
            return InvariantCheck(key, False, "no ruling evidence span is linked")
        if span.alternative_id != alternative.id:
            return InvariantCheck(
                key, False, "ruling evidence is linked to a different alternative"
            )
        if span.evidence_role != EvidenceRole.RULING:
            return InvariantCheck(
                key, False, f"ruling evidence role is {span.evidence_role.value}, not RULING"
            )
        if not span.is_verified_exact_ruling():
            return InvariantCheck(
                key,
                False,
                f"ruling evidence is {span.verification_status.value}, not VERIFIED_EXACT",
            )
        if alternative.ruling_actor_id is None:
            return InvariantCheck(key, False, "ruling actor is not identified")
        if span.author_contributor_id != alternative.ruling_actor_id:
            return InvariantCheck(
                key,
                False,
                f"ruling evidence author '{span.author_contributor_id}' does not match "
                f"authorized ruling actor '{alternative.ruling_actor_id}'",
            )
        if alternative.ruling_authority not in {
            "BDFL",
            "CORE_MAINTAINER_CONSENSUS",
            "WORKING_GROUP",
        }:
            return InvariantCheck(
                key,
                False,
                f"ruling authority {alternative.ruling_authority!r} is not a "
                "verified human authority; model inference is never authority",
            )
        quorum_failure = self._check_ruling_quorum(alternative)
        if quorum_failure is not None:
            return InvariantCheck(key, False, quorum_failure)
        return InvariantCheck(
            key,
            True,
            f"VERIFIED_EXACT RULING by {span.author_handle} under "
            f"{alternative.ruling_authority} authority with verified quorum",
        )

    @staticmethod
    def _check_ruling_quorum(
        alternative: DecisionAlternativeRecord,
    ) -> str | None:
        """Return a failure reason when the sign-off quorum is unmet, else None.

        Quorum rules (docs/STORAGE_AND_RETRIEVAL.md, Lifecycle Authority
        contract): BDFL needs one verified BDFL; Core Maintainer consensus
        needs the ruling author plus one other distinct verified Core
        Maintainer; Working Group needs the Chair plus one distinct verified
        member. Sign-offs must be active and scoped to the decision's project.
        """
        signoffs = alternative.ruling_signoffs
        actor_id = alternative.ruling_actor_id
        if actor_id is None:
            return "ruling actor is not identified"

        def active(role: str | None = None) -> list[SignoffRecord]:
            return [s for s in signoffs if s.is_active_for(alternative.project_context, role)]

        if alternative.ruling_authority == "BDFL":
            if not any(s.contributor_id == actor_id for s in active(BDFL_ROLE)):
                return "BDFL ruling requires this project's verified BDFL as ruling actor"
            return None

        if alternative.ruling_authority == "CORE_MAINTAINER_CONSENSUS":
            maintainers = active(CORE_MAINTAINER_ROLE)
            if not any(s.contributor_id == actor_id for s in maintainers):
                return "consensus ruling author must be a verified Core Maintainer"
            if not [s for s in maintainers if s.contributor_id != actor_id]:
                return (
                    "Core Maintainer consensus requires the ruling author plus one "
                    "other distinct verified Core Maintainer sign-off"
                )
            return None

        if alternative.ruling_authority == "WORKING_GROUP":
            chairs = active(WG_CHAIR_ROLE)
            if not any(s.contributor_id == actor_id for s in chairs):
                return "Working Group ruling requires a verified Chair as ruling actor"
            if not [s for s in active(WG_MEMBER_ROLE) if s.contributor_id != actor_id]:
                return (
                    "Working Group ruling requires the Chair plus one distinct "
                    "verified member sign-off"
                )
            return None

        return f"unknown ruling authority {alternative.ruling_authority!r}"

    def _check_alternative_code_anchor(
        self, pr_diff: PRDiff, alternative: DecisionAlternativeRecord
    ) -> InvariantCheck:
        key = InvariantKey.ALTERNATIVE_CODE_ANCHOR
        touched = self._touched_anchors(pr_diff, alternative)

        if not alternative.anchors:
            return InvariantCheck(key, False, "alternative has no code anchors")
        if not touched:
            return InvariantCheck(
                key, False, "PR diff does not touch any anchor of this alternative"
            )
        active = [a for a in touched if a.anchor_state in ACTIVE_ANCHOR_STATES]
        if not active:
            first = touched[0]
            if not first.lineage_resolved:
                detail = "touched anchor has ambiguous parent-aware lineage; watchdog abstains"
            else:
                detail = f"touched anchor state is {first.anchor_state.value}, not CURRENT/MIGRATED"
            return InvariantCheck(key, False, detail)
        unresolved = [a for a in active if not a.lineage_resolved]
        if unresolved:
            return InvariantCheck(
                key,
                False,
                "touched anchor has ambiguous parent-aware lineage; watchdog abstains",
            )
        return InvariantCheck(
            key,
            True,
            "PR diff touches active anchor(s) "
            + ", ".join(a.qualified_symbol_name for a in active),
        )

    def _check_ast_structural_equivalence(
        self, pr_diff: PRDiff, alternative: DecisionAlternativeRecord
    ) -> InvariantCheck:
        key = InvariantKey.AST_STRUCTURAL_EQUIVALENCE

        touched = self._touched_anchors(pr_diff, alternative)
        active_touched = [a for a in touched if a.is_active]
        if not active_touched:
            return InvariantCheck(key, False, "no active lineage-resolved anchor to verify against")

        fingerprints = {
            a.ast_structural_fingerprint
            for a in active_touched
            if a.ast_structural_fingerprint is not None
        }
        if len(fingerprints) != 1 or (len(active_touched) > 1 and len(fingerprints) > 1):
            return InvariantCheck(
                key,
                False,
                "ambiguous multiple differing anchor fingerprints; watchdog abstains",
            )
        if any(a.ast_structural_fingerprint is None for a in active_touched):
            return InvariantCheck(
                key, False, "anchor lacks a structural fingerprint; cannot verify"
            )

        anchor = active_touched[0]
        analysis = pr_diff.analysis_for(anchor.current_path) or pr_diff.analysis_for(
            anchor.historical_file_path
        )
        if analysis is None:
            return InvariantCheck(
                key, False, f"no AST analysis available for {anchor.current_path}"
            )
        if not analysis.parseable:
            if analysis.language != "python":
                return InvariantCheck(
                    key,
                    False,
                    f"structural verification unsupported for language "
                    f"'{analysis.language}'; watchdog abstains",
                )
            return InvariantCheck(
                key,
                False,
                "added fragment is unparseable; watchdog abstains rather than guess",
            )
        if anchor.ast_structural_fingerprint in analysis.subtree_fingerprints:
            return InvariantCheck(
                key,
                True,
                "masked-AST subtree fingerprint of the rejected construct is "
                "present in the added code",
            )
        return InvariantCheck(
            key,
            False,
            "added code is not structurally equivalent to the rejected construct "
            "(possible benign refactor or hard negative); file/symbol overlap is "
            "insufficient",
        )

    # ── verdict and mode mapping ────────────────────────────────────────

    def _overall_verdict(
        self,
        evaluations: list[CandidateEvaluation],
        authorized: list[CandidateEvaluation],
        pr_diff: PRDiff,
    ) -> WatchdogVerdict:
        if authorized:
            return WatchdogVerdict.WARNING_AUTHORIZED
        if not evaluations:
            return WatchdogVerdict.NO_REJECTED_MATCH

        # Cross-alternative association: a matched candidate's own anchors are
        # not touched while the diff touches anchors of other alternatives.
        anchor_missed = [
            e for e in evaluations if not e.check(InvariantKey.ALTERNATIVE_CODE_ANCHOR).satisfied
        ]
        if anchor_missed and self._diff_touches_other_alternatives(
            pr_diff, {e.alternative_id for e in anchor_missed}
        ):
            return WatchdogVerdict.ALTERNATIVE_MISMATCH

        rejected = [e for e in evaluations if e.check(InvariantKey.DECISION_STATE).satisfied]
        if not rejected:
            return WatchdogVerdict.NO_REJECTED_MATCH
        return WatchdogVerdict.ABSTAINED_UNCERTAIN

    def _diff_touches_other_alternatives(self, pr_diff: PRDiff, exclude_ids: set[str]) -> bool:
        touched_paths = pr_diff.touched_paths
        diff_symbols = pr_diff.all_symbols
        for alternative in self._store.all_alternatives():
            if alternative.id in exclude_ids:
                continue
            for anchor in alternative.anchors:
                if (
                    anchor.repository_url
                    and pr_diff.repository_url
                    and anchor.repository_url != pr_diff.repository_url
                ):
                    continue
                if (
                    anchor.current_path in touched_paths
                    or anchor.historical_file_path in touched_paths
                    or anchor.symbol_name in diff_symbols
                ):
                    return True
        return False

    @staticmethod
    def _action_for(verdict: WatchdogVerdict, execution_mode: ExecutionMode) -> WatchdogAction:
        if verdict != WatchdogVerdict.WARNING_AUTHORIZED:
            return WatchdogAction.LOGGED_ONLY
        if execution_mode == ExecutionMode.AUDIT_DRY_RUN:
            return WatchdogAction.LOGGED_ONLY
        if execution_mode == ExecutionMode.INTERACTIVE_HITL:
            return WatchdogAction.HITL_PENDING
        return WatchdogAction.COMMENT_AUTHORIZED

    def record_dispatch_result(
        self, result: AuthorizationResult, success: bool
    ) -> AuthorizationResult:
        """Record the actual external outcome of dispatching a HITL card."""
        new_action = WatchdogAction.HITL_CARD_POSTED if success else WatchdogAction.DISPATCH_FAILED
        updated = dataclasses.replace(result, action_taken=new_action)
        self.audit_log.append(updated)
        return updated

    def record_publication_result(
        self, result: AuthorizationResult, success: bool
    ) -> AuthorizationResult:
        """Record the actual external outcome of publishing a PR comment."""
        new_action = WatchdogAction.COMMENT_PUBLISHED if success else WatchdogAction.PUBLISH_FAILED
        updated = dataclasses.replace(result, action_taken=new_action)
        self.audit_log.append(updated)
        return updated

    # ── helpers ─────────────────────────────────────────────────────────

    def _touched_anchors(
        self, pr_diff: PRDiff, alternative: DecisionAlternativeRecord
    ) -> list[CodeAnchorRecord]:
        touched_paths = pr_diff.touched_paths
        diff_symbols = pr_diff.all_symbols
        return [
            a
            for a in alternative.anchors
            if (
                not (
                    a.repository_url
                    and pr_diff.repository_url
                    and a.repository_url != pr_diff.repository_url
                )
            )
            and (
                a.current_path in touched_paths
                or a.historical_file_path in touched_paths
                or a.symbol_name in diff_symbols
            )
        ]

    def _touched_active_anchor(
        self, pr_diff: PRDiff, alternative: DecisionAlternativeRecord
    ) -> CodeAnchorRecord | None:
        active = [a for a in self._touched_anchors(pr_diff, alternative) if a.is_active]
        return active[0] if active else None

    @staticmethod
    def _hitl_card_stub(
        evaluation: CandidateEvaluation,
        comment_payload: dict,
        repository_url: str,
        pr_number: int,
        commit_sha: str,
    ) -> dict:
        """Minimal authorization payload for the Switch HITL card; the full
        Block Kit rendering lives in ``biscuit.switch.cards``."""
        return {
            "card_kind": "PR_WARNING_APPROVAL",
            "alternative_id": evaluation.alternative_id,
            "proposal_text": evaluation.proposal_text,
            "repository_url": repository_url,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "comment_body_sha256": _sha256_text(comment_payload.get("body", "")),
            "comment_body": comment_payload.get("body"),
        }


__all__ = ["AuditLog", "WarningAuthorizationGate"]
