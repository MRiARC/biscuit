"""Slack Block Kit card construction for Switch HITL approvals.

``BlockKitBuilder`` renders the interactive maintainer cards described in
docs/LIVING_DECISION_INTELLIGENCE.md sections 1 and 4:

* **Living decision approval cards** — posted when a live consensus signal
  or explicit ``@biscuit record-decision`` produces a draft. Buttons are
  ``Approve Draft`` / ``Dismiss Draft``. Approving only publishes the draft;
  it never assigns an alternative lifecycle state (state de-conflation).
* **PR warning authorization cards** — posted in ``INTERACTIVE_HITL`` mode
  when the 4-invariant gate authorizes a warning. Buttons are
  ``Post to PR`` / ``Keep Silent``. The card embeds the exact PR, commit and
  alternative identifiers plus the pinned SHA-256 of the comment body so the
  approver approves one immutable payload.

Cards are produced as plain dicts (Slack Block Kit JSON) and are platform-
agnostic on Biscuit's side: Switch owns the actual delivery to Slack,
Discord, Teams or Mattermost.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from biscuit.switch.types import (
    APPROVAL_TTL_HOURS,
    HITLApprovalRequest,
    LivingDecisionDraft,
    SwitchActionType,
    compute_sha256_hex,
)

_MAX_TEXT = 2900


def _truncate(text: str, limit: int = _MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class BlockKitBuilder:
    """Builds Slack Block Kit payloads for Switch approval activities."""

    def build_living_decision_card(
        self,
        draft: LivingDecisionDraft,
        switch_session_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> dict:
        """Card asking a maintainer to approve or dismiss a living draft.

        The [Approve Draft] action only publishes the draft record
        (``MAINTAINER_APPROVED``); it does not resolve the decision or mark
        any alternative REJECTED.
        """
        expiry = expires_at or datetime.now(UTC) + timedelta(
            hours=APPROVAL_TTL_HOURS
        )
        header = f"Living decision draft — {draft.topic}"
        sections = [
            self._section_mrkdwn(f"*{header}*"),
            self._section_mrkdwn(
                f"*Proposed alternative:* {_truncate(draft.proposal_text)}"
            ),
            self._section_mrkdwn(
                f"*Initiated by:* {draft.initiating_author_handle} in "
                f"{draft.source_platform.value} `{draft.channel_id}` "
                f"(thread `{draft.thread_ts}`)"
            ),
        ]
        if draft.rejection_summary:
            sections.append(
                self._section_mrkdwn(f"*Concerns raised:* {_truncate(draft.rejection_summary)}")
            )
        if draft.evidence_quotes:
            quotes = "\n".join(
                f"• “{_truncate(quote, 300)}” — {author} [{start}:{end})"
                for author, quote, start, end in draft.evidence_quotes[:5]
            )
            sections.append(self._section_mrkdwn(f"*Evidence spans:*\n{quotes}"))
        sections.append(
            self._section_mrkdwn(
                "Approving publishes this draft only. Alternative outcomes "
                "(accept / reject / defer) require separate, explicitly "
                "recorded maintainer rulings."
            )
        )

        return {
            "blocks": [
                *sections,
                self._actions(
                    [
                        {"id": "approve_draft", "label": "Approve Draft", "style": "primary"},
                        {"id": "dismiss_draft", "label": "Dismiss Draft", "style": "danger"},
                    ]
                ),
                self._context(
                    f"Expires {expiry.isoformat()} • session "
                    f"{switch_session_id or draft.switch_session_id}"
                ),
            ]
        }

    def build_pr_warning_card(
        self,
        repository_url: str,
        pr_number: int,
        commit_sha: str,
        alternative_id: str,
        proposal_text: str,
        rejection_summary: str | None,
        ruling_quote: str | None,
        ruling_author: str | None,
        ruling_permalink: str | None,
        comment_body: str,
        switch_session_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> dict:
        """Card asking a maintainer to authorize or dismiss a PR warning.

        The card pins the exact approvable payload: repository, PR number,
        commit SHA, alternative id and the SHA-256 of the rendered comment
        body. A later callback that does not match these values is rejected
        by ``CallbackSecurity``.
        """
        expiry = expires_at or datetime.now(UTC) + timedelta(
            hours=APPROVAL_TTL_HOURS
        )
        comment_sha = compute_sha256_hex(comment_body)

        blocks = [
            self._section_mrkdwn("*PR conflict with a rejected alternative*"),
            self._section_mrkdwn(
                f"*Pull request:* <{repository_url}/pull/{pr_number}|#{pr_number}> "
                f"at commit `{commit_sha[:12]}`"
            ),
            self._section_mrkdwn(
                f"*Rejected alternative:* {_truncate(proposal_text)}"
            ),
        ]
        if rejection_summary:
            blocks.append(
                self._section_mrkdwn(f"*Rejection reason:* {_truncate(rejection_summary)}")
            )
        if ruling_quote:
            author_label = f" — {ruling_author}" if ruling_author else ""
            link = f" (<{ruling_permalink}|source>)" if ruling_permalink else ""
            blocks.append(
                self._section_mrkdwn(
                    f"*Authoritative ruling:*\n> {_truncate(ruling_quote, 500)}"
                    f"{author_label}{link}"
                )
            )
        blocks.extend(
            (
                self._section_mrkdwn(
                    f"*Approvable comment payload* (SHA-256 `{comment_sha}`):\n"
                    f"> {_truncate(comment_body, 700)}"
                ),
                self._actions(
                    [
                        {"id": "authorize_pr_comment", "label": "Post to PR", "style": "primary"},
                        {"id": "dismiss_pr_comment", "label": "Keep Silent", "style": "danger"},
                    ]
                ),
                self._context(
                    f"Expires {expiry.isoformat()} • alternative "
                    f"`{alternative_id[:8]}…`"
                    + (f" • session {switch_session_id}" if switch_session_id else "")
                ),
            )
        )
        return {"blocks": blocks}

    # ── HITL approval request assembly ──────────────────────────────────

    def build_approval_request(
        self,
        action_type: SwitchActionType,
        card_payload: dict,
        switch_approval_id: str | None = None,
        authorized_roles: tuple[str, ...] = ("BDFL", "Core Maintainer"),
        target_pr_number: int | None = None,
        target_repository: str | None = None,
        target_commit_sha: str | None = None,
        living_draft_id: str | None = None,
        decision_alternative_id: str | None = None,
    ) -> HITLApprovalRequest:
        """Assemble a ``hitl_approval_requests`` row around a rendered card.

        The request pins the canonical action payload hash, carries a fresh
        UUIDv4 idempotency key and the 72-hour expiry, and binds its targets
        per the DDL CHECK constraint (draft targets for living decisions; PR
        repository/number/commit for warning authorizations).
        """
        from biscuit.switch.types import new_idempotency_key

        if action_type == SwitchActionType.APPROVE_LIVING_DECISION:
            if living_draft_id is None:
                raise ValueError("living decision approval requires living_draft_id")
        else:
            if (
                decision_alternative_id is None
                or target_pr_number is None
                or not target_repository
                or not target_commit_sha
            ):
                raise ValueError(
                    "PR warning authorization requires decision_alternative_id, "
                    "target_pr_number, target_repository and target_commit_sha"
                )

        return HITLApprovalRequest(
            id=str(uuid.uuid4()),
            switch_approval_id=switch_approval_id
            or f"switch-appr-{uuid.uuid4().hex[:12]}",
            action_type=action_type,
            idempotency_key=new_idempotency_key(),
            card_rendered_payload=card_payload,
            authorized_roles=authorized_roles,
            target_pr_number=target_pr_number,
            target_repository=target_repository,
            target_commit_sha=target_commit_sha,
            living_draft_id=living_draft_id,
            decision_alternative_id=decision_alternative_id,
        )

    # ── Switch ApprovalOpen payload ─────────────────────────────────────

    def build_approval_open_payload(
        self,
        request: HITLApprovalRequest,
        session_id: str,
        turn_id: str = "biscuit-turn-1",
        expires_at: datetime | None = None,
    ) -> dict:
        """Map a HITL request onto Switch's ``POST /agent-sessions/{id}/approvals``.

        ``ApprovalOpen`` body at the pinned upstream commit: ``request_id``,
        ``turn_id``, ``kind`` ("approval"), ``title``, ``detail``, ``options``
        (id/label/decision) and ``expires_at``. Decision labels mirror the
        card buttons so the maintainer's selection maps back to a terminal
        ``HITLEventAction``.
        """
        options = self._options_for(request.action_type)
        expiry = expires_at or request.expires_at
        return {
            "request_id": request.switch_approval_id,
            "turn_id": turn_id,
            "kind": "approval",
            "title": self._title_for(request),
            "detail": self._detail_for(request),
            "options": [
                {"id": option_id, "label": label, "decision": decision}
                for option_id, label, decision in options
            ],
            "room_id": None,
            "thread_id": None,
            "expires_at": expiry.isoformat(),
        }

    @staticmethod
    def _options_for(action_type: SwitchActionType) -> list[tuple[str, str, str]]:
        if action_type == SwitchActionType.APPROVE_LIVING_DECISION:
            return [
                ("approve_draft", "Approve Draft", "approve"),
                ("dismiss_draft", "Dismiss Draft", "reject"),
            ]
        if action_type == SwitchActionType.AUTHORIZE_PR_WARNING:
            return [("authorize_pr_comment", "Post to PR", "approve")]
        return [("dismiss_pr_comment", "Keep Silent", "approve")]

    def _title_for(self, request: HITLApprovalRequest) -> str:
        if request.action_type == SwitchActionType.APPROVE_LIVING_DECISION:
            return "Biscuit: approve living decision draft"
        return "Biscuit: authorize PR warning comment"

    def _detail_for(self, request: HITLApprovalRequest) -> str:
        payload = request.card_rendered_payload
        blocks = payload.get("blocks", [])
        texts = [
            block.get("text", {}).get("text", "")
            for block in blocks
            if block.get("type") == "section"
        ]
        return _truncate("\n".join(t for t in texts if t), 2800)

    # ── Block Kit primitives ────────────────────────────────────────────

    @staticmethod
    def _section_mrkdwn(text: str) -> dict:
        return {"type": "section", "text": {"type": "mrkdwn", "text": text}}

    @staticmethod
    def _actions(options: list[dict]) -> dict:
        return {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": option["id"],
                    "text": {"type": "plain_text", "text": option["label"]},
                    "style": option.get("style", "default"),
                }
                for option in options
            ],
        }

    @staticmethod
    def _context(text: str) -> dict:
        return {
            "type": "context",
            "elements": [{"type": "plain_text", "text": _truncate(text, 1900)}],
        }
