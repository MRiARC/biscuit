"""GitHub review comment formatting for authorized watchdog warnings.

``PRCommentFormatter`` renders the verifiable in-line PR review comment
described in docs/LIVING_DECISION_INTELLIGENCE.md section 8 and
docs/TWO_PILLAR_ARCHITECTURE.md section 6:

* cites the authoritative ruling with its exact quote, author, permalink and
  zero-based ``[start_char, end_char)`` offsets,
* links the decision's source thread (MADR context),
* names the alternative-specific code anchor that was touched,
* states the execution mode so readers can tell dry-run from production.

The formatter is only invoked after all four authorization invariants have
passed; it never decides authorization itself.
"""

from __future__ import annotations

from biscuit.watchdog.types import (
    CodeAnchorRecord,
    DecisionAlternativeRecord,
    DecisionRecord,
    EvidenceSpanRecord,
    ExecutionMode,
)

_COMMENT_TEMPLATE = """## Biscuit guardrail: previously rejected alternative detected

This pull request appears to re-introduce an alternative that was
authoritatively rejected in {decision_title}.

**Alternative:** {proposal_text}
**Status:** REJECTED — {authority_label}
**Rejection reason:** {rejection_summary}

**Authoritative ruling (verified exact span [{start_char}, {end_char})):**
> {exact_quote}
>
> — {author_handle}, {ruling_link}

**Decision record:** {decision_link}
**Code anchor touched:** `{anchor_symbol}` in `{anchor_path}` ({anchor_state})
**Retrieval similarity:** {similarity} (candidate signal only; not proof)

This warning was published only after all four authorization invariants
passed (rejected lifecycle, verified human ruling, alternative-specific code
anchor, AST structural equivalence). Generated in `{execution_mode}` mode.
"""


class PRCommentFormatter:
    """Formats GitHub review comments and their audit payloads."""

    def format_comment(
        self,
        alternative: DecisionAlternativeRecord,
        decision: DecisionRecord | None,
        anchor: CodeAnchorRecord | None,
        ruling_span: EvidenceSpanRecord | None,
        execution_mode: ExecutionMode,
        similarity: float | None = None,
        pr_number: int | None = None,
        repository_url: str | None = None,
        commit_sha: str | None = None,
    ) -> str:
        """Render the markdown body of the in-line PR review comment."""
        decision_title = decision.title if decision else "an architectural decision"
        if decision is not None and decision.source_thread_url:
            decision_link = f"[{decision.title}]({decision.source_thread_url})"
        else:
            decision_link = decision_title

        exact_quote = ruling_span.exact_quote if ruling_span else "No verified ruling quote."
        author_handle = ruling_span.author_handle if ruling_span else "unknown"
        start_char = ruling_span.start_char if ruling_span else 0
        end_char = ruling_span.end_char if ruling_span else 0
        if ruling_span is not None and ruling_span.permalink:
            ruling_link = f"[source]({ruling_span.permalink})"
        else:
            ruling_link = "source unavailable"

        anchor_symbol = anchor.qualified_symbol_name if anchor else "unknown"
        anchor_path = anchor.current_path if anchor else "unknown"
        anchor_state = anchor.anchor_state.value if anchor else "UNKNOWN"

        similarity_label = f"{similarity:.3f}" if similarity is not None else "not available"

        return _COMMENT_TEMPLATE.format(
            decision_title=decision_title,
            decision_link=decision_link,
            proposal_text=alternative.proposal_text,
            authority_label=self._authority_label(alternative),
            rejection_summary=alternative.rejection_summary or "No summary recorded.",
            exact_quote=exact_quote,
            author_handle=author_handle,
            ruling_link=ruling_link,
            start_char=start_char,
            end_char=end_char,
            anchor_symbol=anchor_symbol,
            anchor_path=anchor_path,
            anchor_state=anchor_state,
            similarity=similarity_label,
            execution_mode=execution_mode.value,
        )

    def build_comment_payload(
        self,
        alternative: DecisionAlternativeRecord,
        decision: DecisionRecord | None,
        anchor: CodeAnchorRecord | None,
        ruling_span: EvidenceSpanRecord | None,
        execution_mode: ExecutionMode,
        similarity: float | None = None,
        pr_number: int | None = None,
        repository_url: str | None = None,
        commit_sha: str | None = None,
        line: int = 1,
        side: str = "RIGHT",
    ) -> dict:
        """Build the JSON comment payload stored in ``pr_watchdog_audit_log``."""
        body = self.format_comment(
            alternative=alternative,
            decision=decision,
            anchor=anchor,
            ruling_span=ruling_span,
            execution_mode=execution_mode,
            similarity=similarity,
            pr_number=pr_number,
            repository_url=repository_url,
            commit_sha=commit_sha,
        )
        payload: dict = {
            "body": body,
            "repository_url": repository_url,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
            "commit_id": commit_sha,
            "path": anchor.current_path if anchor else "",
            "line": line,
            "side": side,
            "execution_mode": execution_mode.value,
            "alternative_id": alternative.id,
            "proposal_text": alternative.proposal_text,
            "lifecycle_status": alternative.lifecycle_status.value,
            "ruling_authority": alternative.ruling_authority,
        }
        if decision is not None:
            payload["decision_id"] = decision.id
            payload["decision_title"] = decision.title
            payload["decision_url"] = decision.source_thread_url
        if anchor is not None:
            payload["code_anchor"] = {
                "id": anchor.id,
                "qualified_symbol_name": anchor.qualified_symbol_name,
                "path": anchor.current_path,
                "anchor_state": anchor.anchor_state.value,
            }
        if ruling_span is not None:
            payload["ruling_evidence"] = {
                "id": ruling_span.id,
                "exact_quote": ruling_span.exact_quote,
                "author_handle": ruling_span.author_handle,
                "permalink": ruling_span.permalink,
                "start_char": ruling_span.start_char,
                "end_char": ruling_span.end_char,
                "verification_status": ruling_span.verification_status.value,
            }
        if similarity is not None:
            payload["retrieval_similarity"] = similarity
        return payload

    @staticmethod
    def _authority_label(alternative: DecisionAlternativeRecord) -> str:
        if alternative.ruling_authority is None:
            return "no recorded authority"
        if alternative.ruling_authority == "BDFL":
            return "BDFL ruling"
        if alternative.ruling_authority == "CORE_MAINTAINER_CONSENSUS":
            return "Core Maintainer consensus"
        if alternative.ruling_authority == "WORKING_GROUP":
            return "Working Group ruling"
        return f"unverified authority ({alternative.ruling_authority})"
