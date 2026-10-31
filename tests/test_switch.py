"""Unit tests for the Switch FastMCP Agent Bridge (Stream 4, Issue #20).

Zero-dependency: FastMCP tool calls run against an in-memory transport and
mock decision records — no live Ollama models, Switch host, or GitHub
network access is required.

Conforms to the issue's required test suite:

* FastMCP tools ``query_decisions``, ``check_pr_diff`` and
  ``draft_living_decision`` (registered, callable, schema-correct),
* HMAC-SHA256 signature verification and replay prevention,
* Block Kit JSON card formatting,
* Switch activity client against the pinned 0.31.0 activity routes,
* HITL approval request integrity: idempotency keys, 72h TTL, immutable
  action payload hash, 409-on-duplicate semantics,
* State de-conflation: approving a living-decision draft card changes only
  ``draft_status``; it never assigns an alternative lifecycle state.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from test_watchdog import (
    HARD_NEGATIVE_TERNARY_DIFF,
    VIOLATION_DIFF,
    build_store,
)

from biscuit.switch import (
    APPROVAL_TTL_HOURS,
    SWITCH_UPSTREAM_COMMIT,
    ApprovalOutcome,
    BlockKitBuilder,
    CallbackSecurity,
    CheckPRDiffInput,
    DraftStatus,
    HITLApprovalRequest,
    HITLEventAction,
    HITLRequestStatus,
    LiveMessagePayload,
    LivingDecisionDraft,
    MockTransport,
    SourcePlatform,
    SwitchActionType,
    SwitchActivityClient,
    canonical_json,
    compute_sha256_hex,
    new_idempotency_key,
    sign_body,
)
from biscuit.switch.security import VerifiedProjectRoleGrant
from biscuit.switch.server import FastMCPServer
from biscuit.watchdog import (
    CandidateRetriever,
    WarningAuthorizationGate,
)

SECRET = "test-callback-secret"

LIVE_THREAD_MESSAGES = [
    LiveMessagePayload(
        external_message_id="m1",
        author_platform_id="U123",
        author_handle="dev.alpha",
        timestamp="2026-10-31T10:00:00Z",
        raw_content="We should reject the eager coalesce() built-in; it breaks short-circuiting.",
    ),
    LiveMessagePayload(
        external_message_id="m2",
        parent_message_id="m1",
        author_platform_id="U456",
        author_handle="dev.beta",
        timestamp="2026-10-31T10:02:00Z",
        raw_content="Agreed, consensus: eager evaluation is the fatal problem here.",
    ),
]

DRAFT = LivingDecisionDraft(
    id="ld-1",
    switch_session_id="sess-1",
    source_platform=SourcePlatform.SLACK,
    channel_id="C0123",
    thread_ts="1772.001",
    topic="Reject eager coalesce() built-in",
    proposal_text="Built-in coalesce(*args) with eager evaluation",
    initiating_author_handle="dev.alpha",
    rejection_summary="Eager evaluation defeats short-circuiting.",
    evidence_quotes=(
        (
            "dev.alpha",
            "We should reject the eager coalesce() built-in; it breaks short-circuiting.",
            0,
            84,
        ),
    ),
)


def make_server(**kwargs) -> FastMCPServer:
    store = build_store()
    return FastMCPServer(
        gate=WarningAuthorizationGate(store=store),
        retriever=CandidateRetriever(store=store),
        decision_summaries=[
            {
                "decision_id": "dec-1",
                "title": "PEP-505: None-aware operators",
                "project_context": "python/cpython",
                "top_alternative_text": "Built-in coalesce(*args) function",
                "top_alternative_status": "REJECTED",
                "ruling_authority": "CORE_MAINTAINER_CONSENSUS",
                "source_thread_url": "https://discuss.python.org/t/pep-505",
            }
        ],
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# FastMCP server & tool contracts
# ─────────────────────────────────────────────────────────────────────────────


class TestFastMCPServerTools:
    def test_server_exposes_exactly_the_three_tools(self):
        import asyncio

        server = make_server()
        tools = asyncio.run(_list_tools(server))
        assert sorted(t.name for t in tools) == [
            "check_pr_diff",
            "draft_living_decision",
            "query_decisions",
        ]

    def test_query_decisions_tool_call(self):
        import asyncio

        async def scenario():
            async with Client(make_server().mcp) as client:
                result = await client.call_tool(
                    "query_decisions",
                    {"params": {"query_text": "did we consider coalesce?", "top_k": 3}},
                )
                return json.loads(result.content[0].text)

        payload = asyncio.run(scenario())
        assert payload["query_text"] == "did we consider coalesce?"
        assert len(payload["results"]) == 1
        summary = payload["results"][0]
        assert summary["decision_id"] == "dec-1"
        assert summary["top_alternative_status"] == "REJECTED"
        assert summary["ruling_authority"] == "CORE_MAINTAINER_CONSENSUS"

    def test_query_decisions_filters_by_repository_context(self):
        server = make_server()
        payload = server._query_decisions(
            __import__(
                "biscuit.switch.types", fromlist=["QueryDecisionsInput"]
            ).QueryDecisionsInput(
                query_text="coalesce",
                repository_context="kubernetes/kubernetes",
            )
        )
        assert payload["results"] == []

    def test_check_pr_diff_tool_call_authorized_case(self):
        import asyncio

        async def scenario():
            async with Client(make_server().mcp) as client:
                result = await client.call_tool(
                    "check_pr_diff",
                    {
                        "params": {
                            "repository_url": "https://github.com/python/cpython",
                            "pr_number": 412,
                            "commit_sha": "a" * 40,
                            "diff_text": VIOLATION_DIFF,
                        }
                    },
                )
                return json.loads(result.content[0].text)

        payload = asyncio.run(scenario())
        assert payload["authorization_verdict"] == "WARNING_AUTHORIZED"
        assert payload["execution_mode"] == "AUDIT_DRY_RUN"
        assert payload["action_taken"] == "LOGGED_ONLY"
        assert payload["authorized_alternative_ids"] == ["alt-1"]
        assert payload["comment_markdown"] is not None
        invariants = payload["candidates"][0]["invariants"]
        assert all(invariants.values())
        assert payload["audit"]["pr_number"] == 412

    def test_check_pr_diff_hard_negative_abstains(self):
        server = make_server()
        payload = server._check_pr_diff(
            __import__("biscuit.switch.types", fromlist=["CheckPRDiffInput"]).CheckPRDiffInput(
                repository_url="https://github.com/python/cpython",
                pr_number=413,
                commit_sha="a" * 40,
                diff_text=HARD_NEGATIVE_TERNARY_DIFF,
            )
        )
        assert payload["authorization_verdict"] == "ABSTAINED_UNCERTAIN"
        assert payload["action_taken"] == "LOGGED_ONLY"
        assert not payload["authorized_alternative_ids"]

    def test_check_pr_diff_rejects_unknown_execution_mode(self):
        server = make_server()
        payload = server._check_pr_diff(
            __import__("biscuit.switch.types", fromlist=["CheckPRDiffInput"]).CheckPRDiffInput(
                repository_url="https://github.com/python/cpython",
                pr_number=1,
                commit_sha="a" * 40,
                diff_text=VIOLATION_DIFF,
            ),
            execution_mode="MAKE_LOUD_NOISES",
        )
        assert payload["execution_mode"] == "AUDIT_DRY_RUN"

    def test_draft_living_decision_tool_call(self):
        import asyncio

        async def scenario():
            async with Client(make_server().mcp) as client:
                result = await client.call_tool(
                    "draft_living_decision",
                    {
                        "params": {
                            "switch_session_id": "sess-1",
                            "source_platform": "slack",
                            "channel_id": "C0123",
                            "thread_ts": "1772.001",
                            "initiating_author_handle": "dev.alpha",
                            "messages": [
                                {
                                    "external_message_id": m.external_message_id,
                                    "author_platform_id": m.author_platform_id,
                                    "author_handle": m.author_handle,
                                    "timestamp": m.timestamp,
                                    "raw_content": m.raw_content,
                                }
                                for m in LIVE_THREAD_MESSAGES
                            ],
                        }
                    },
                )
                return json.loads(result.content[0].text)

        payload = asyncio.run(scenario())
        assert payload["draft_status"] == "DRAFT_PENDING_APPROVAL"
        assert payload["initiating_author_handle"] == "dev.alpha"
        assert payload["draft_id"].startswith("ld-")
        spans = payload["evidence_spans"]
        assert spans, "decision-bearing messages must produce evidence spans"
        first = spans[0]
        assert first["verification_status"] == "UNVERIFIED"
        assert first["content_hash"] == compute_sha256_hex(first["exact_quote"])
        assert first["end_char"] - first["start_char"] == len(first["exact_quote"])
        # offsets index the exact raw content
        message = LIVE_THREAD_MESSAGES[0]
        assert message.raw_content[first["start_char"] : first["end_char"]] == first["exact_quote"]

    def test_draft_living_decision_rejects_unknown_platform(self):
        server = make_server()
        payload = server._draft_living_decision(
            __import__(
                "biscuit.switch.types", fromlist=["DraftLivingDecisionInput"]
            ).DraftLivingDecisionInput(
                switch_session_id="sess-1",
                source_platform="irc",
                channel_id="C1",
                thread_ts="1.1",
                initiating_author_handle="dev",
                messages=LIVE_THREAD_MESSAGES,
            )
        )
        assert "error" in payload

    def test_drafting_never_assigns_lifecycle_states(self):
        server = make_server()
        payload = server._draft_living_decision(
            __import__(
                "biscuit.switch.types", fromlist=["DraftLivingDecisionInput"]
            ).DraftLivingDecisionInput(
                switch_session_id="sess-1",
                source_platform="slack",
                channel_id="C0123",
                thread_ts="1772.001",
                initiating_author_handle="dev.alpha",
                messages=LIVE_THREAD_MESSAGES,
            )
        )
        # The draft result carries no lifecycle vocabulary at all: draft
        # publication approval is separate from alternative outcome rulings.
        dumped = json.dumps(payload)
        for forbidden in ("REJECTED", "ACCEPTED", "ruling_authority", "lifecycle"):
            assert forbidden not in dumped


async def _list_tools(server: FastMCPServer):
    async with Client(server.mcp) as client:
        return await client.list_tools()


# ─────────────────────────────────────────────────────────────────────────────
# Block Kit card formatting
# ─────────────────────────────────────────────────────────────────────────────


class TestBlockKitBuilder:
    def test_living_decision_card_structure(self):
        card = BlockKitBuilder().build_living_decision_card(DRAFT)
        assert "blocks" in card
        kinds = [b["type"] for b in card["blocks"]]
        assert kinds.count("section") >= 3
        assert "actions" in kinds
        assert "context" in kinds
        # approve/dismiss actions
        actions = next(b for b in card["blocks"] if b["type"] == "actions")
        labels = [e["text"]["text"] for e in actions["elements"]]
        assert labels == ["Approve Draft", "Edit Rationale", "Dismiss Draft"]
        # de-conflation notice
        all_text = json.dumps(card)
        assert "Alternative outcomes" in all_text
        assert "PEP" not in all_text  # sanity: no unrelated injection

    def test_living_decision_card_cites_evidence_quotes(self):
        card = BlockKitBuilder().build_living_decision_card(DRAFT)
        assert "We should reject the eager coalesce()" in json.dumps(card)

    def test_pr_warning_card_pins_the_approvable_payload(self):
        card = BlockKitBuilder().build_pr_warning_card(
            repository_url="https://github.com/python/cpython",
            pr_number=412,
            commit_sha="a" * 40,
            alternative_id="alt-1",
            proposal_text="Built-in coalesce(*args) function",
            rejection_summary="Eager argument evaluation defeats short-circuiting.",
            ruling_quote="coalesce evaluates eagerly.",
            ruling_author="markshannon",
            ruling_permalink="https://discuss.python.org/t/pep-505/post-112",
            comment_body="Warning: rejected alternative re-introduced.",
        )
        dumped = json.dumps(card)
        assert compute_sha256_hex("Warning: rejected alternative re-introduced.") in dumped
        assert "/pull/412" in dumped
        assert "Post to PR" in dumped
        assert "Keep Silent" in dumped
        assert "markshannon" in dumped
        assert "post-112" in dumped

    def test_build_approval_request_for_pr_warning(self):
        builder = BlockKitBuilder()
        card = builder.build_pr_warning_card(
            repository_url="https://github.com/python/cpython",
            pr_number=412,
            commit_sha="a" * 40,
            alternative_id="alt-1",
            proposal_text="coalesce()",
            rejection_summary=None,
            ruling_quote=None,
            ruling_author=None,
            ruling_permalink=None,
            comment_body="body",
        )
        request = builder.build_approval_request(
            action_type=SwitchActionType.AUTHORIZE_PR_WARNING,
            card_payload=card,
            target_pr_number=412,
            target_repository="https://github.com/python/cpython",
            target_commit_sha="a" * 40,
            decision_alternative_id="alt-1",
        )
        assert request.status == HITLRequestStatus.PENDING
        assert request.action_payload_sha256 == compute_sha256_hex(canonical_json(card))
        assert request.idempotency_key != new_idempotency_key()  # unique per request
        assert request.expires_at - request.created_at == timedelta(hours=APPROVAL_TTL_HOURS)

    def test_build_approval_request_validates_target_shape(self):
        builder = BlockKitBuilder()
        with pytest.raises(ValueError):
            builder.build_approval_request(
                action_type=SwitchActionType.AUTHORIZE_PR_WARNING,
                card_payload={"blocks": []},
            )
        with pytest.raises(ValueError):
            builder.build_approval_request(
                action_type=SwitchActionType.APPROVE_LIVING_DECISION,
                card_payload={"blocks": []},
            )

    def test_approval_open_payload_matches_switch_contract(self):
        builder = BlockKitBuilder()
        request = builder.build_approval_request(
            action_type=SwitchActionType.APPROVE_LIVING_DECISION,
            card_payload=builder.build_living_decision_card(DRAFT),
            living_draft_id="ld-1",
        )
        payload = builder.build_approval_open_payload(request, session_id="sess-1")
        assert payload["kind"] == "approval"
        assert payload["request_id"] == request.switch_approval_id
        assert payload["options"] == [
            {"id": "approve_draft", "label": "Approve Draft", "decision": "accept"},
            {"id": "edit_rationale", "label": "Edit Rationale", "decision": "cancel"},
            {"id": "dismiss_draft", "label": "Dismiss Draft", "decision": "decline"},
        ]
        assert payload["questions"] == []
        assert "expires_at" in payload
        assert "turn_id" in payload


# ─────────────────────────────────────────────────────────────────────────────
# HMAC-SHA256 callback security & replay prevention
# ─────────────────────────────────────────────────────────────────────────────


_LATEST_CALLBACK_REQUEST: HITLApprovalRequest | None = None


def callback_body(action: str, request_id: str, key: str | None = None) -> bytes:
    request = _LATEST_CALLBACK_REQUEST
    assert request is not None, "create a request before constructing a callback"
    payload = {
        "request_id": request_id,
        "action": action,
        "idempotency_key": key,
        "action_payload_sha256": request.action_payload_sha256,
        "actor_contributor_id": "c2",
        "actor_platform_id": "U999",
        "actor_source_platform": "slack",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def _test_role_grants(
    source_platform: str, platform_id: str, contributor_id: str, project: str
):
    if (
        source_platform == "slack"
        and platform_id == "U999"
        and contributor_id == "c2"
        and project == "https://github.com/python/cpython"
    ):
        return [
            VerifiedProjectRoleGrant(
                contributor_id="c2",
                source_platform="slack",
                platform_id="U999",
                project_context=project,
                role="Core Maintainer",
            )
        ]
    return []


def make_security(**kwargs) -> CallbackSecurity:
    defaults = {
        "secret": SECRET,
        "role_grant_resolver": _test_role_grants,
        "replay_db_path": ":memory:",
        "allow_volatile_replay_for_tests": True,
    }
    defaults.update(kwargs)
    return CallbackSecurity(**defaults)


def make_request(**overrides) -> HITLApprovalRequest:
    defaults: dict = {
        "id": "hitl-1",
        "switch_approval_id": "switch-appr-1",
        "action_type": SwitchActionType.AUTHORIZE_PR_WARNING,
        "idempotency_key": new_idempotency_key(),
        "card_rendered_payload": {"blocks": [{"type": "section", "text": {"text": "x"}}]},
        "target_pr_number": 412,
        "target_repository": "https://github.com/python/cpython",
        "target_commit_sha": "a" * 40,
        "decision_alternative_id": "alt-1",
    }
    defaults.update(overrides)
    global _LATEST_CALLBACK_REQUEST
    _LATEST_CALLBACK_REQUEST = HITLApprovalRequest(**defaults)
    return _LATEST_CALLBACK_REQUEST


class TestCallbackSecurity:
    def test_valid_signature_is_accepted(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        valid, reason, body_hash = security.verify(
            request, body, sign_body(SECRET, body), request.idempotency_key
        )
        assert valid, reason
        assert body_hash == compute_sha256_hex(body.decode("utf-8"))

    def test_missing_or_malformed_signature_rejected(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        for bad in (None, "sha256=zz", "sha256=" + "0" * 63, "deadbeef"):
            valid, reason, _ = security.verify(request, body, bad, request.idempotency_key)
            assert not valid
            assert "signature" in reason.lower()

    def test_wrong_secret_signature_rejected(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        valid, reason, _ = security.verify(
            request, body, sign_body("other-secret", body), request.idempotency_key
        )
        assert not valid
        assert "does not match" in reason

    def test_tampered_body_rejected(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        signature = sign_body(SECRET, body)
        tampered = callback_body(
            "DISMISS_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        valid, reason, _ = security.verify(request, tampered, signature, request.idempotency_key)
        assert not valid
        assert "does not match" in reason

    def test_replayed_callback_is_rejected_409(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        signature = sign_body(SECRET, body)
        first, _, _ = security.verify(request, body, signature, request.idempotency_key)
        assert first
        second, reason, _ = security.verify(request, body, signature, request.idempotency_key)
        assert not second
        assert "replay" in reason.lower()

    def test_finalized_request_rejects_further_callbacks(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        signature = sign_body(SECRET, body)
        finalized, reason = security.finalize(
            request,
            HITLEventAction.AUTHORIZE_PR_COMMENT,
            body,
            signature,
            actor_platform_id="U999",
            actor_source_platform="slack",
            actor_contributor_id="c2",
            idempotency_key=request.idempotency_key,
        )
        assert finalized is not None, reason
        assert request.status == HITLRequestStatus.DECIDED

        replay, reason = security.finalize(
            request,
            HITLEventAction.AUTHORIZE_PR_COMMENT,
            body,
            signature,
            actor_platform_id="U999",
            actor_source_platform="slack",
            actor_contributor_id="c2",
            idempotency_key=request.idempotency_key,
        )
        assert replay is None
        assert "409" in reason or "DECIDED" in reason

    def test_expired_request_rejected(self):
        security = make_security()
        request = make_request(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        valid, reason, _ = security.verify(
            request, body, sign_body(SECRET, body), request.idempotency_key
        )
        assert not valid
        assert "expired" in reason

    def test_idempotency_key_mismatch_rejected(self):
        security = make_security()
        request = make_request()
        body = callback_body("AUTHORIZE_PR_COMMENT", request.switch_approval_id, "not-a-uuid")
        valid, reason, _ = security.verify(request, body, sign_body(SECRET, body), "not-a-uuid")
        assert not valid
        assert "UUIDv4" in reason

    def test_action_type_compatibility_enforced(self):
        security = make_security()
        request = make_request()  # AUTHORIZE_PR_WARNING request
        body = callback_body("APPROVE_DRAFT", request.switch_approval_id, request.idempotency_key)
        result, reason = security.finalize(
            request,
            HITLEventAction.APPROVE_DRAFT,
            body,
            sign_body(SECRET, body),
            actor_platform_id="U999",
            actor_source_platform="slack",
            actor_contributor_id="c2",
            idempotency_key=request.idempotency_key,
        )
        assert result is None
        assert "not valid for request type" in reason

    def test_expiration_sweep_marks_pending_requests(self):
        security = make_security()
        stale = make_request(expires_at=datetime.now(UTC) - timedelta(hours=1))
        fresh = make_request()
        expired = security.expired_requests([stale, fresh])
        assert expired == [stale]
        assert stale.status == HITLRequestStatus.EXPIRED
        assert fresh.status == HITLRequestStatus.PENDING


# ─────────────────────────────────────────────────────────────────────────────
# Switch activity client (pinned 0.31.0 routes)
# ─────────────────────────────────────────────────────────────────────────────


class TestSwitchActivityClient:
    def _client(self, transport: MockTransport) -> SwitchActivityClient:
        return SwitchActivityClient(
            transport=transport, base_url="http://switch.local:8787", api_key="key-1"
        )

    def test_open_approval_posts_to_pinned_route(self):
        transport = MockTransport()
        client = self._client(transport)
        payload = {"request_id": "r1", "kind": "approval", "options": []}
        import asyncio

        asyncio.run(client.open_approval("sess-1", payload))
        request = transport.requests[0]
        assert request["url"].endswith("/agent-sessions/sess-1/approvals")
        assert request["method"] == "POST"
        assert request["json"] == payload
        assert request["headers"]["Authorization"] == "Bearer key-1"
        assert request["headers"]["X-Biscuit-Switch-Upstream-Commit"] == SWITCH_UPSTREAM_COMMIT

    def test_close_and_delivered_routes(self):
        import asyncio

        transport = MockTransport()
        client = self._client(transport)
        asyncio.run(client.close_approval("sess-1", "r1"))
        asyncio.run(client.mark_delivered("sess-1", "r1"))
        close_url = transport.requests[0]["url"]
        delivered_url = transport.requests[1]["url"]
        assert close_url.endswith("/agent-sessions/sess-1/approvals/r1/close")
        assert delivered_url.endswith("/agent-sessions/sess-1/approvals/r1/delivered")

    def test_poll_outcomes_parses_approval_views(self):
        import asyncio

        transport = MockTransport(
            responses={
                ("GET", "http://switch.local:8787/agent-sessions/approvals/outcomes"): [
                    {
                        "session_id": "sess-1",
                        "request_id": "r1",
                        "state": "answered",
                        "answeredBy": "U999",
                        "answeredAt": "2026-10-31T12:00:00Z",
                        "expiresAt": "2026-11-03T12:00:00Z",
                        "answer": {"optionId": "authorize_pr_comment"},
                    }
                ]
            }
        )
        client = self._client(transport)
        outcomes = asyncio.run(client.poll_outcomes())
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.request_id == "r1"
        assert outcome.answered_by == "U999"
        assert outcome.selected_option_id == "authorize_pr_comment"

    def test_dispatch_approval_opens_and_acknowledges(self):
        import asyncio

        transport = MockTransport(
            responses={
                (
                    "POST",
                    "http://switch.local:8787/agent-sessions/sess-1/approvals",
                ): {"request_id": "r1", "state": "pending"},
            }
        )
        client = self._client(transport)
        view = asyncio.run(
            client.dispatch_approval(
                "sess-1",
                make_request(),
                {"request_id": "r1", "kind": "approval", "options": []},
            )
        )
        assert view["request_id"] == "r1"
        urls = [r["url"] for r in transport.requests]
        assert urls[0].endswith("/agent-sessions/sess-1/approvals")
        assert urls[1].endswith("/agent-sessions/sess-1/approvals/r1/delivered")

    def test_http_error_propagates(self):
        import asyncio

        class FailingTransport(MockTransport):
            async def post(self, url, json=None, **kwargs):
                from biscuit.switch.activity_client import _MockResponse

                return _MockResponse({}, status_code=500)

        client = self._client(FailingTransport())
        with pytest.raises(RuntimeError):
            asyncio.run(client.open_approval("sess-1", {}))


# ─────────────────────────────────────────────────────────────────────────────
# HITL state de-conflation (Approve Draft ≠ REJECTED alternative)
# ─────────────────────────────────────────────────────────────────────────────


class TestStateDeconflation:
    def test_draft_approval_changes_only_draft_status(self):
        """Invariant: [Approve Draft] sets MAINTAINER_APPROVED and nothing else."""
        draft = DRAFT
        approved = replace(draft, draft_status=DraftStatus.MAINTAINER_APPROVED)
        assert approved.draft_status == DraftStatus.MAINTAINER_APPROVED
        assert approved.proposal_text == draft.proposal_text
        assert approved.id == draft.id

        # The living-decision state machine has no REJECTED state at all:
        # rejection is a separate alternative-level ruling.
        assert "REJECTED" not in {status.value for status in DraftStatus}

    def test_server_draft_flow_keeps_drafts_pending(self):
        server = make_server()
        payload = server._draft_living_decision(
            __import__(
                "biscuit.switch.types", fromlist=["DraftLivingDecisionInput"]
            ).DraftLivingDecisionInput(
                switch_session_id="sess-1",
                source_platform="slack",
                channel_id="C0123",
                thread_ts="1772.001",
                initiating_author_handle="dev.alpha",
                messages=LIVE_THREAD_MESSAGES,
            )
        )
        draft = server.living_drafts[payload["draft_id"]]
        assert draft.draft_status == DraftStatus.DRAFT_PENDING_APPROVAL

    def test_pinned_upstream_commit_constant(self):
        assert SWITCH_UPSTREAM_COMMIT == "7e57cd433a922bc91a00150c106e6ef1f15b1057"

    def test_approval_ttl_is_72_hours(self):
        assert APPROVAL_TTL_HOURS == 72


# ─────────────────────────────────────────────────────────────────────────────
# Canonical JSON / hashing contracts
# ─────────────────────────────────────────────────────────────────────────────


class TestCanonicalSerialization:
    def test_canonical_json_is_key_order_independent(self):
        a = {"b": 1, "a": {"y": 2, "x": 3}}
        b = {"a": {"x": 3, "y": 2}, "b": 1}
        assert canonical_json(a) == canonical_json(b)
        assert json.loads(canonical_json(a)) == json.loads(canonical_json(b))

    def test_action_payload_hash_is_stable_across_key_order(self):
        card_a = {"blocks": [{"type": "section", "text": {"text": "hi"}}]}
        card_b = {"blocks": [{"text": {"text": "hi"}, "type": "section"}]}
        request_a = make_request(card_rendered_payload=card_a)
        request_b = make_request(card_rendered_payload=card_b)
        assert request_a.action_payload_sha256 == request_b.action_payload_sha256


# ─────────────────────────────────────────────────────────────────────────────
# Upstream Switch schema & response contracts
# ─────────────────────────────────────────────────────────────────────────────


class TestSwitchUpstreamSchemaContract:
    def test_approval_outcome_from_json_string_answer(self):
        """Upstream Switch 0.31.0 returns answer as a string containing option ID."""
        payload = {
            "sessionId": "sess-42",
            "requestId": "req-99",
            "state": "answered",
            "answeredBy": "U12345",
            "answeredAt": "2026-10-31T15:00:00Z",
            "expiresAt": "2026-11-03T15:00:00Z",
            "answer": "authorize_pr_comment",
        }
        outcome = ApprovalOutcome.from_json(payload)
        assert outcome.session_id == "sess-42"
        assert outcome.request_id == "req-99"
        assert outcome.selected_option_id == "authorize_pr_comment"
        assert outcome.answered_by == "U12345"

    def test_approval_outcome_from_json_dict_answer(self):
        """Tolerates structured object answer from earlier test harnesses."""
        payload = {
            "session_id": "sess-42",
            "request_id": "req-99",
            "state": "answered",
            "answer": {"optionId": "edit_rationale"},
        }
        outcome = ApprovalOutcome.from_json(payload)
        assert outcome.session_id == "sess-42"
        assert outcome.request_id == "req-99"
        assert outcome.selected_option_id == "edit_rationale"

    def test_approval_open_payload_includes_empty_questions_list(self):
        builder = BlockKitBuilder()
        request = builder.build_approval_request(
            action_type=SwitchActionType.AUTHORIZE_PR_WARNING,
            card_payload=builder.build_pr_warning_card(
                repository_url="https://github.com/python/cpython",
                pr_number=101,
                commit_sha="a" * 40,
                alternative_id="alt-1",
                proposal_text="proposal",
                rejection_summary="rejected",
                ruling_quote="quote",
                ruling_author="author",
                ruling_permalink="https://permalink",
                comment_body="comment body",
            ),
            target_pr_number=101,
            target_repository="https://github.com/python/cpython",
            target_commit_sha="a" * 40,
            decision_alternative_id="alt-1",
        )
        payload = builder.build_approval_open_payload(request, session_id="sess-1")
        assert "questions" in payload
        assert payload["questions"] == []
        option_ids = [opt["id"] for opt in payload["options"]]
        assert option_ids == ["authorize_pr_comment", "dismiss_pr_comment"]


# ─────────────────────────────────────────────────────────────────────────────
# Negative security & role authorization tests
# ─────────────────────────────────────────────────────────────────────────────


class TestCallbackSecurityNegative:
    def test_missing_caller_idempotency_key_is_rejected(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        sig = sign_body(SECRET, body)
        valid, reason, _ = security.verify(request, body, sig, idempotency_key=None)
        assert not valid
        assert "idempotency key is missing" in reason

    def test_mismatched_idempotency_key_is_rejected(self):
        security = make_security()
        request = make_request()
        other_key = new_idempotency_key()
        body = callback_body("AUTHORIZE_PR_COMMENT", request.switch_approval_id, other_key)
        sig = sign_body(SECRET, body)
        valid, reason, _ = security.verify(request, body, sig, idempotency_key=other_key)
        assert not valid
        assert "idempotency key does not match" in reason

    def test_mismatched_signed_request_id_claim_is_rejected(self):
        security = make_security()
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", "different-request-id", request.idempotency_key
        )
        sig = sign_body(SECRET, body)
        valid, reason, _ = security.verify(
            request, body, sig, idempotency_key=request.idempotency_key
        )
        assert not valid
        assert "does not match approval request" in reason

    def test_stale_callback_timestamp_rejected(self):
        security = make_security()
        request = make_request()
        stale_time = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
        payload = {
            "request_id": request.switch_approval_id,
            "action": "AUTHORIZE_PR_COMMENT",
            "idempotency_key": request.idempotency_key,
            "timestamp": stale_time,
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        sig = sign_body(SECRET, body)
        valid, reason, _ = security.verify(
            request, body, sig, idempotency_key=request.idempotency_key
        )
        assert not valid
        assert "freshness window" in reason

    def test_fresh_callback_timestamp_accepted(self):
        security = make_security()
        request = make_request()
        fresh_time = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        payload = json.loads(
            callback_body("AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key)
        )
        payload["timestamp"] = fresh_time
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        sig = sign_body(SECRET, body)
        valid, reason, _ = security.verify(
            request, body, sig, idempotency_key=request.idempotency_key
        )
        assert valid, reason

    def test_unauthorized_actor_roles_rejected(self):
        security = make_security()
        request = make_request(authorized_roles=("Core Maintainer", "BDFL"))
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        sig = sign_body(SECRET, body)
        result, reason = security.finalize(
            request,
            HITLEventAction.AUTHORIZE_PR_COMMENT,
            body,
            sig,
            actor_platform_id="U999",
            actor_source_platform="slack",
            actor_contributor_id="c2",
            idempotency_key=request.idempotency_key,
            verified_actor_roles=("Triage", "Contributor"),
        )
        assert result is None
        assert "does not hold an authorized maintainer role" in reason

    def test_authorized_actor_roles_accepted(self):
        security = make_security()
        request = make_request(authorized_roles=("Core Maintainer", "BDFL"))
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        sig = sign_body(SECRET, body)
        result, reason = security.finalize(
            request,
            HITLEventAction.AUTHORIZE_PR_COMMENT,
            body,
            sig,
            actor_platform_id="U999",
            actor_source_platform="slack",
            actor_contributor_id="c2",
            idempotency_key=request.idempotency_key,
            verified_actor_roles=("Core Maintainer",),
        )
        assert result is not None, reason
        assert request.status == HITLRequestStatus.DECIDED

    def test_edit_rationale_action_on_living_decision_card(self):
        security = make_security()
        request = make_request(
            action_type=SwitchActionType.APPROVE_LIVING_DECISION,
            authorized_roles=("Core Maintainer",),
        )
        body = callback_body("EDIT_RATIONALE", request.switch_approval_id, request.idempotency_key)
        sig = sign_body(SECRET, body)
        result, reason = security.finalize(
            request,
            HITLEventAction.EDIT_RATIONALE,
            body,
            sig,
            actor_platform_id="U999",
            actor_source_platform="slack",
            actor_contributor_id="c2",
            idempotency_key=request.idempotency_key,
            verified_actor_roles=("Core Maintainer",),
        )
        assert result is not None, reason
        assert request.status == HITLRequestStatus.DECIDED
        assert request.idempotency_key in security.consumed_keys

    def test_dismiss_warning_action_on_pr_warning_card(self):
        security = make_security()
        request = make_request(
            action_type=SwitchActionType.AUTHORIZE_PR_WARNING,
            authorized_roles=("Core Maintainer",),
        )
        body = callback_body(
            "DISMISS_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        sig = sign_body(SECRET, body)
        result, reason = security.finalize(
            request,
            HITLEventAction.DISMISS_PR_COMMENT,
            body,
            sig,
            actor_platform_id="U999",
            actor_source_platform="slack",
            actor_contributor_id="c2",
            idempotency_key=request.idempotency_key,
            verified_actor_roles=("Core Maintainer",),
        )
        assert result is not None, reason
        assert request.status == HITLRequestStatus.DECIDED
        assert request.idempotency_key in security.consumed_keys


# ─────────────────────────────────────────────────────────────────────────────
# Commit SHA validation & transcript persistence across restarts
# ─────────────────────────────────────────────────────────────────────────────


class TestCommitSHABindingAndPersistence:
    def test_check_pr_diff_rejects_missing_or_synthetic_commit_sha(self):
        server = make_server()
        with pytest.raises(ValueError, match="verified PR commit SHA"):
            server._check_pr_diff(
                CheckPRDiffInput(
                    repository_url="https://github.com/python/cpython",
                    pr_number=1,
                    diff_text=VIOLATION_DIFF,
                    commit_sha="",
                )
            )

        with pytest.raises(ValueError, match="verified PR commit SHA"):
            server._check_pr_diff(
                CheckPRDiffInput(
                    repository_url="https://github.com/python/cpython",
                    pr_number=1,
                    diff_text=VIOLATION_DIFF,
                    commit_sha="0" * 40,
                )
            )

    def test_transcript_store_persistence_and_restart_durability(self, tmp_path):
        from biscuit.switch.persistence import TranscriptStore

        db_path = str(tmp_path / "transcript.db")
        store = TranscriptStore(db_path=db_path)

        thread_id = store.record_thread(
            session_id="sess-durability",
            platform="slack",
            channel_id="C9876",
            thread_ts="1899.001",
            initiating_author_handle="dev.charlie",
        )

        msg = LiveMessagePayload(
            external_message_id="msg-1",
            author_platform_id="U987",
            author_handle="dev.charlie",
            timestamp="2026-10-31T12:00:00Z",
            raw_content="We should reject the eager coalesce() built-in; consensus agrees.",
        )
        _, v1_id = store.record_message(thread_id, msg)

        # Message edit appends version 2
        msg_edited = LiveMessagePayload(
            external_message_id="msg-1",
            author_platform_id="U987",
            author_handle="dev.charlie",
            timestamp="2026-10-31T12:05:00Z",
            raw_content="We should reject the eager coalesce() built-in; consensus strongly agrees.",
        )
        _, _v2_id = store.record_message(thread_id, msg_edited)

        versions = store.get_message_versions("msg-1")
        assert len(versions) == 2
        assert versions[0]["version_num"] == 1
        assert versions[0]["is_current"] == 0
        assert versions[1]["version_num"] == 2
        assert versions[1]["is_current"] == 1
        assert versions[0]["raw_content"] == msg.raw_content
        assert versions[1]["raw_content"] == msg_edited.raw_content

        quote = "reject the eager coalesce() built-in"
        start_char = msg.raw_content.index(quote)
        end_char = start_char + len(quote)
        span_id = "span-durability-1"
        evidence_spans = [
            {
                "id": span_id,
                "source_message_version_id": v1_id,
                "exact_quote": quote,
                "start_char": start_char,
                "end_char": end_char,
                "content_hash": compute_sha256_hex(quote),
                "verification_status": "UNVERIFIED",
            }
        ]

        draft = LivingDecisionDraft(
            id="ld-durability",
            switch_session_id="sess-durability",
            source_platform=SourcePlatform.SLACK,
            channel_id="C9876",
            thread_ts="1899.001",
            topic="Reject eager coalesce",
            proposal_text="Reject eager coalesce",
            initiating_author_handle="dev.charlie",
        )
        store.save_draft(draft, thread_id, evidence_spans)

        # Verify exact code-point slice and UTF-8 hash
        verified = store.verify_span(span_id)
        assert verified

        # Close and reopen store from disk (restart simulation)
        store.close()

        reopened_store = TranscriptStore(db_path=db_path)
        saved = reopened_store.get_draft("ld-durability")
        assert saved is not None
        assert saved["topic"] == "Reject eager coalesce"
        assert len(saved["evidence_spans"]) == 1
        saved_span = saved["evidence_spans"][0]
        assert saved_span["verification_status"] == "VERIFIED_EXACT"
        assert saved_span["exact_quote"] == quote
        assert saved_span["start_char"] == start_char
        assert saved_span["end_char"] == end_char

        reopened_versions = reopened_store.get_message_versions("msg-1")
        assert len(reopened_versions) == 2
        reopened_store.close()


# ---------------------------------------------------------------------------
# Regression coverage for pinned Switch schema and fail-closed callbacks
# ---------------------------------------------------------------------------


def test_switch_options_use_pinned_decision_literals_and_23h_expiry():
    builder = BlockKitBuilder()
    request = builder.build_approval_request(
        action_type=SwitchActionType.APPROVE_LIVING_DECISION,
        card_payload=builder.build_living_decision_card(DRAFT),
        living_draft_id="ld-1",
    )
    payload = builder.build_approval_open_payload(request, session_id="sess-1")
    assert {o["decision"] for o in payload["options"]} <= {
        "accept", "acceptForSession", "decline", "cancel"
    }
    assert payload["questions"] == []
    expires = datetime.fromisoformat(payload["expires_at"])
    assert timedelta(hours=22) < expires - datetime.now(UTC) < timedelta(hours=24)


def test_default_slack_button_has_no_invalid_style_field():
    builder = BlockKitBuilder()
    card = builder.build_living_decision_card(DRAFT)
    actions = next(b for b in card["blocks"] if b["type"] == "actions")
    buttons = {b["action_id"]: b for b in actions["elements"]}
    assert "style" not in buttons["edit_rationale"]
    assert buttons["approve_draft"]["style"] == "primary"
    assert buttons["dismiss_draft"]["style"] == "danger"


def test_callback_rejects_missing_timestamp_or_signed_payload_claim():
    security = make_security()
    request = make_request()
    original = json.loads(callback_body(
        "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
    ))
    for missing in ("timestamp", "action_payload_sha256", "actor_contributor_id"):
        payload = dict(original)
        payload.pop(missing)
        body = json.dumps(payload, sort_keys=True).encode()
        valid, _, _ = security.verify(
            request, body, sign_body(SECRET, body), request.idempotency_key
        )
        assert not valid, missing


def test_callback_requires_trusted_project_role_resolver():
    security = make_security(role_grant_resolver=None)
    request = make_request()
    body = callback_body(
        "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
    )
    result, reason = security.finalize(
        request, HITLEventAction.AUTHORIZE_PR_COMMENT, body,
        sign_body(SECRET, body), "U999", "slack", "c2",
        idempotency_key=request.idempotency_key,
    )
    assert result is None
    assert "role resolver" in reason


def test_callback_rejects_revoked_and_cross_project_grants():
    for project in ("https://github.com/another/repository", "https://github.com/python/cpython"):
        def invalid_grants(source, platform, actor, target):
            return [VerifiedProjectRoleGrant(
                contributor_id=actor, source_platform=source, platform_id=platform,
                project_context=project, role="Core Maintainer", revoked=True,
            )]
        security = make_security(role_grant_resolver=invalid_grants)
        request = make_request()
        body = callback_body(
            "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
        )
        result, reason = security.finalize(
            request, HITLEventAction.AUTHORIZE_PR_COMMENT, body,
            sign_body(SECRET, body), "U999", "slack", "c2",
            idempotency_key=request.idempotency_key,
        )
        assert result is None
        assert "does not hold" in reason


def test_durable_replay_ledger_rejects_after_restart(tmp_path):
    db = str(tmp_path / "switch-replay.sqlite3")
    security = make_security(replay_db_path=db)
    request = make_request()
    body = callback_body(
        "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
    )
    args = (
        request, HITLEventAction.AUTHORIZE_PR_COMMENT, body,
        sign_body(SECRET, body), "U999", "slack", "c2",
    )
    result, reason = security.finalize(
        *args, idempotency_key=request.idempotency_key
    )
    assert result is not None, reason
    security.close()

    restarted = make_security(replay_db_path=db)
    pending_copy = replace(request, status=HITLRequestStatus.PENDING)
    result, reason = restarted.finalize(
        pending_copy, HITLEventAction.AUTHORIZE_PR_COMMENT,
        body, sign_body(SECRET, body), "U999", "slack", "c2",
        idempotency_key=request.idempotency_key,
    )
    assert result is None
    assert "replayed" in reason
    restarted.close()


def test_no_durable_ledger_means_no_finalization():
    security = CallbackSecurity(secret=SECRET, role_grant_resolver=_test_role_grants)
    request = make_request()
    body = callback_body(
        "AUTHORIZE_PR_COMMENT", request.switch_approval_id, request.idempotency_key
    )
    result, reason = security.finalize(
        request, HITLEventAction.AUTHORIZE_PR_COMMENT, body,
        sign_body(SECRET, body), "U999", "slack", "c2",
        idempotency_key=request.idempotency_key,
    )
    assert result is None
    assert "replay ledger" in reason
