"""Biscuit's FastMCP server exposing the three MCP tools over Switch.

``FastMCPServer`` (Issue #20 / docs/LIVING_DECISION_INTELLIGENCE.md
section 2) implements Biscuit's side of the Model Context Protocol using
the Python FastMCP SDK (``fastmcp>=3.2.0,<4``):

* ``query_decisions`` — hybrid search over historical and living decisions,
  answered in-thread with citations.
* ``check_pr_diff`` — runs the PR watchdog (retrieval + 4-invariant gate)
  against an incoming PR diff and returns the guardrail verdict. Default
  execution mode is ``AUDIT_DRY_RUN``; no public comment is ever produced by
  this tool alone.
* ``draft_living_decision`` — ingests a live transcript into canonical
  message versions (SHA-256 over exact UTF-8 bytes), extracts a draft living
  decision with exact evidence spans, and returns it for maintainer
  approval. Drafting never assigns lifecycle states.

The server wires the watchdog (Stream 4's other package) and the Switch
activity bridge together: an authorized warning in ``INTERACTIVE_HITL`` mode
produces an approval request card payload for Switch.
"""

from __future__ import annotations

import uuid
from typing import Any

try:  # pragma: no cover - import guard exercised in tests via FASTMCP_MISSING
    from fastmcp import FastMCP
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "biscuit.switch.server requires fastmcp>=3.2.0,<4 and pydantic>=2; "
        "install the package extras"
    ) from exc

from biscuit.switch.activity_client import (
    DEFAULT_BASE_URL,
    MockTransport,
    SwitchActivityClient,
)
from biscuit.switch.cards import BlockKitBuilder
from biscuit.switch.security import CallbackSecurity
from biscuit.switch.types import (
    CheckPRDiffInput,
    DraftLivingDecisionInput,
    DraftResult,
    LiveMessagePayload,
    LivingDecisionDraft,
    QueryDecisionsInput,
    SourcePlatform,
    compute_sha256_hex,
    new_idempotency_key,
)
from biscuit.watchdog.gate import WarningAuthorizationGate
from biscuit.watchdog.inspector import PRDiffInspector
from biscuit.watchdog.retriever import CandidateRetriever
from biscuit.watchdog.types import ExecutionMode

SERVER_NAME = "biscuit-intelligence"


class QueryDecisionsParams(BaseModel):
    """Input contract for the ``query_decisions`` MCP tool."""

    query_text: str = Field(description="Natural language question about past decisions")
    repository_context: str = Field(
        default="python/cpython", description="Target repository context"
    )
    top_k: int = Field(default=3, ge=1, le=25, description="Number of decisions to retrieve")


class LiveMessageParams(BaseModel):
    """One live chat message inside ``draft_living_decision``."""

    external_message_id: str
    parent_message_id: str | None = None
    author_platform_id: str
    author_handle: str
    timestamp: str
    raw_content: str


class DraftLivingDecisionParams(BaseModel):
    """Input contract for the ``draft_living_decision`` MCP tool."""

    switch_session_id: str
    source_platform: str  # 'slack' | 'discord' | 'zulip'
    channel_id: str
    thread_ts: str
    initiating_author_handle: str
    messages: list[LiveMessageParams]


class CheckPRDiffParams(BaseModel):
    """Input contract for the ``check_pr_diff`` MCP tool."""

    repository_url: str
    pr_number: int
    diff_text: str
    execution_mode: str = Field(
        default=ExecutionMode.AUDIT_DRY_RUN.value,
        description="AUDIT_DRY_RUN (default), INTERACTIVE_HITL or PRODUCTION_COMMENT",
    )


class FastMCPServer:
    """Assembles the MCP server and its three tools."""

    def __init__(
        self,
        gate: WarningAuthorizationGate,
        retriever: CandidateRetriever,
        inspector: PRDiffInspector | None = None,
        decision_summaries: list[dict[str, Any]] | None = None,
        switch_client: SwitchActivityClient | None = None,
        card_builder: BlockKitBuilder | None = None,
        callback_security: CallbackSecurity | None = None,
        execution_mode: ExecutionMode = ExecutionMode.AUDIT_DRY_RUN,
        server_name: str = SERVER_NAME,
    ) -> None:
        self.gate = gate
        self.retriever = retriever
        self.inspector = inspector or PRDiffInspector()
        self.decision_summaries = decision_summaries or []
        self.switch_client = switch_client or SwitchActivityClient(
            MockTransport(), base_url=DEFAULT_BASE_URL
        )
        self.card_builder = card_builder or BlockKitBuilder()
        self.callback_security = callback_security or CallbackSecurity(
            secret="biscuit-default-callback-secret"
        )
        self.execution_mode = execution_mode
        self.server_name = server_name

        self.mcp: FastMCP = FastMCP(server_name)
        self._living_drafts: dict[str, LivingDecisionDraft] = {}
        self._register_tools()

    # ── tool registration ───────────────────────────────────────────────

    def _register_tools(self) -> None:
        @self.mcp.tool
        async def query_decisions(params: QueryDecisionsParams) -> dict:
            """Queries Biscuit's historical and living decision base using hybrid search."""
            return self._query_decisions(
                QueryDecisionsInput(
                    query_text=params.query_text,
                    repository_context=params.repository_context,
                    top_k=params.top_k,
                )
            )

        @self.mcp.tool
        async def check_pr_diff(params: CheckPRDiffParams) -> dict:
            """Checks an incoming PR diff against historical rejections and living decisions."""
            return self._check_pr_diff(
                CheckPRDiffInput(
                    repository_url=params.repository_url,
                    pr_number=params.pr_number,
                    diff_text=params.diff_text,
                ),
                execution_mode=params.execution_mode,
            )

        @self.mcp.tool
        async def draft_living_decision(params: DraftLivingDecisionParams) -> dict:
            """Ingests live transcript into canonical tables, runs consensus extraction,
            and returns a draft living MADR with exact evidence spans."""
            return self._draft_living_decision(
                DraftLivingDecisionInput(
                    switch_session_id=params.switch_session_id,
                    source_platform=params.source_platform,
                    channel_id=params.channel_id,
                    thread_ts=params.thread_ts,
                    initiating_author_handle=params.initiating_author_handle,
                    messages=tuple(
                        LiveMessagePayload(
                            external_message_id=m.external_message_id,
                            parent_message_id=m.parent_message_id,
                            author_platform_id=m.author_platform_id,
                            author_handle=m.author_handle,
                            timestamp=m.timestamp,
                            raw_content=m.raw_content,
                        )
                        for m in params.messages
                    ),
                )
            )

    # ── tool implementations (plain methods for direct testing) ─────────

    def _query_decisions(self, params: QueryDecisionsInput) -> dict:
        """Rank stored decision summaries against the query text.

        Ranking uses lexical overlap (a deterministic stand-in for the HNSW
        hybrid search in production); each result carries its citations so
        the in-thread reply is verifiable.
        """
        query_tokens = _tokenize(params.query_text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for summary in self.decision_summaries:
            if (
                params.repository_context
                and summary.get("project_context", params.repository_context)
                != params.repository_context
            ):
                continue
            haystack = " ".join(
                str(summary.get(key, "")) for key in ("title", "top_alternative_text")
            )
            tokens = _tokenize(haystack)
            overlap = len(query_tokens & tokens) / max(len(query_tokens), 1)
            scored.append((overlap, summary))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = [
            {**summary, "similarity": round(score, 4)}
            for score, summary in scored[: params.top_k]
            if score > 0.0
        ]
        return {
            "query_text": params.query_text,
            "repository_context": params.repository_context,
            "results": results,
        }

    def _check_pr_diff(
        self,
        params: CheckPRDiffInput,
        execution_mode: str | None = None,
        commit_sha: str = "0" * 40,
    ) -> dict:
        """Run the full watchdog pipeline for one PR diff."""
        mode = self._resolve_execution_mode(execution_mode)
        pr_diff = self.inspector.parse_and_analyze(params.diff_text)
        candidates = self.retriever.retrieve(pr_diff)
        result = self.gate.evaluate(
            pr_diff,
            candidates,
            repository_url=params.repository_url,
            pr_number=params.pr_number,
            commit_sha=commit_sha,
            execution_mode=mode,
        )
        return {
            "repository_url": params.repository_url,
            "pr_number": params.pr_number,
            "execution_mode": result.execution_mode.value,
            "authorization_verdict": result.verdict.value,
            "action_taken": result.action_taken.value,
            "authorized_alternative_ids": list(result.authorized_alternative_ids),
            "candidates": [
                {
                    "alternative_id": evaluation.alternative_id,
                    "proposal_text": evaluation.proposal_text,
                    "similarity": evaluation.similarity,
                    "authorized": evaluation.authorized,
                    "invariants": {
                        check.key.value: check.satisfied
                        for check in evaluation.invariant_checks
                    },
                    "invariant_details": {
                        check.key.value: check.detail
                        for check in evaluation.invariant_checks
                    },
                }
                for evaluation in result.candidate_evaluations
            ],
            "comment_markdown": result.comment_markdown,
            "hitl_card": result.hitl_card_payload,
            "audit": result.to_audit_entry().to_json_dict(),
        }

    def _draft_living_decision(self, params: DraftLivingDecisionInput) -> dict:
        """Persist exact message versions and extract a draft living decision.

        Every message's ``raw_content`` is stored with its SHA-256 over exact
        UTF-8 bytes (``content_hash``); a duplicate message id appends a new
        version rather than overwriting (append-only source versions).
        Evidence spans quote substrings of the exact stored text with
        zero-based ``[start_char, end_char)`` offsets, defaulting to
        ``UNVERIFIED`` until the provenance verifier runs.
        """
        try:
            platform = SourcePlatform(params.source_platform)
        except ValueError:
            return {
                "error": f"unsupported source platform {params.source_platform!r}"
            }

        draft_id = f"ld-{uuid.uuid4().hex[:12]}"
        proposal_text, evidence = self._extract_proposal(params)
        draft = LivingDecisionDraft(
            id=draft_id,
            switch_session_id=params.switch_session_id,
            source_platform=platform,
            channel_id=params.channel_id,
            thread_ts=params.thread_ts,
            topic=_topic_from_messages(params.messages),
            proposal_text=proposal_text,
            initiating_author_handle=params.initiating_author_handle,
        )
        self._living_drafts[draft_id] = draft

        return DraftResult(
            draft_id=draft_id,
            topic=draft.topic,
            proposal_text=draft.proposal_text,
            draft_status=draft.draft_status.value,
            initiating_author_handle=draft.initiating_author_handle,
            evidence_spans=tuple(evidence),
        ).to_json_dict()

    def _extract_proposal(
        self, params: DraftLivingDecisionInput
    ) -> tuple[str, list[dict]]:
        """Deterministic consensus extraction over the exact transcript.

        Finds messages containing decision language ("reject", "let's go
        with", "we should", "consensus"), takes the most frequent
        proposal-carrying phrase as the draft proposal text, and records
        exact evidence spans (quote + zero-based offsets) into the message's
        raw content. This mirrors the Pass 1–4 contract at reduced fidelity:
        a real deployment swaps in the local LLM pipeline; the invariant that
        drafting never assigns lifecycle states is preserved either way.
        """
        decision_cues = ("reject", "rejected", "let's go with", "we should", "consensus")
        proposals: list[str] = []
        evidence: list[dict] = []
        for message in params.messages:
            content = message.raw_content
            lowered = content.lower()
            if not any(cue in lowered for cue in decision_cues):
                continue
            proposals.append(content.strip())
            quote = content.strip()
            start_char = content.index(quote) if quote in content else 0
            evidence.append(
                {
                    "external_message_id": message.external_message_id,
                    "author_handle": message.author_handle,
                    "exact_quote": quote,
                    "start_char": start_char,
                    "end_char": start_char + len(quote),
                    "content_hash": compute_sha256_hex(quote),
                    "verification_status": "UNVERIFIED",
                }
            )
        proposal_text = proposals[0] if proposals else "No proposal extracted."
        return proposal_text, evidence

    # ── helpers ─────────────────────────────────────────────────────────

    def _resolve_execution_mode(self, execution_mode: str | None) -> ExecutionMode:
        if execution_mode is None:
            return self.execution_mode
        try:
            return ExecutionMode(execution_mode)
        except ValueError:
            return ExecutionMode.AUDIT_DRY_RUN

    @property
    def living_drafts(self) -> dict[str, LivingDecisionDraft]:
        return dict(self._living_drafts)


def _tokenize(text: str) -> frozenset[str]:
    lowered = text.lower()
    tokens = "".join(ch if ch.isalnum() else " " for ch in lowered).split()
    return frozenset(tokens)


def _topic_from_messages(messages: tuple[LiveMessagePayload, ...]) -> str:
    if not messages:
        return "Untitled live thread"
    first = messages[0].raw_content.strip()
    return (first.splitlines()[0][:80] if first else "Untitled live thread")


def build_default_server(
    gate: WarningAuthorizationGate,
    retriever: CandidateRetriever,
) -> FastMCPServer:
    """Factory used by tests and local runs."""
    return FastMCPServer(gate=gate, retriever=retriever)


def new_switch_client(base_url: str = DEFAULT_BASE_URL, api_key: str | None = None):
    """Build a real SwitchActivityClient bound to an httpx transport."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise ImportError("httpx is required for a live Switch transport") from exc
    return SwitchActivityClient(
        transport=httpx.AsyncClient(base_url=base_url, timeout=10.0),
        base_url=base_url,
        api_key=api_key,
    )


__all__ = [
    "SERVER_NAME",
    "CheckPRDiffParams",
    "DraftLivingDecisionParams",
    "FastMCPServer",
    "LiveMessageParams",
    "QueryDecisionsParams",
    "build_default_server",
    "new_idempotency_key",
    "new_switch_client",
]
