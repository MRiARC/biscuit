"""Integration tests for the unified Switch + Biscuit directional intelligence architecture."""

from __future__ import annotations

from biscuit.extract.client import LLMClient
from biscuit.extract.pipeline import ExtractionPipeline
from biscuit.switch.cards import BlockKitBuilder
from biscuit.switch.server import FastMCPServer
from biscuit.watchdog.gate import WarningAuthorizationGate
from biscuit.watchdog.inspector import PRDiffInspector
from biscuit.watchdog.retriever import CandidateRetriever
from biscuit.watchdog.types import (
    AnchorState,
    CodeAnchorRecord,
    DecisionAlternativeRecord,
    DecisionRecord,
    EvidenceRole,
    EvidenceSpanRecord,
    ExecutionMode,
    InMemoryDecisionStore,
    ProposalLifecycle,
    SignoffRecord,
    VerificationStatus,
    WatchdogVerdict,
)


def _build_devlens_store() -> InMemoryDecisionStore:
    """Mock store seeded with DevLens ADR-001 (Babel parser rejection)."""
    decision = DecisionRecord(
        id="ADR-001",
        title="AST parsing architecture for repository file analysis",
        context_summary="Decision on AST parsing in serverless execution paths",
        source_thread_url="https://github.com/MRIARC-08/DevLens/issues/1",
        project_context="MRIARC-08/DevLens",
    )
    alternative = DecisionAlternativeRecord(
        id="alt-devlens-001",
        decision_id="ADR-001",
        proposal_text="Full Babel AST Parser with syntax-tree traversal",
        lifecycle_status=ProposalLifecycle.REJECTED,
        rejection_summary="Memory allocation conflicts with Vercel serverless execution limits.",
        rejection_category="Regex Line Parser",
        ruling_evidence_id="span-devlens-ruling",
        ruling_authority="CORE_MAINTAINER_CONSENSUS",
        ruling_actor_id="MRIARC-08",
        project_context="MRIARC-08/DevLens",
        ruling_signoffs=(
            SignoffRecord(
                contributor_id="MRIARC-08",
                role="Lead Maintainer",
                project_context="MRIARC-08/DevLens",
            ),
        ),
        anchors=(
            CodeAnchorRecord(
                id="anchor-devlens-1",
                alternative_id="alt-devlens-001",
                repository_url="https://github.com/MRIARC-08/DevLens",
                language="typescript",
                symbol_type="function",
                qualified_symbol_name="parser.ts.@babel/parser",
                historical_file_path="src/lib/pipeline/parser.ts",
                anchor_state=AnchorState.CURRENT,
                head_file_path="src/lib/pipeline/parser.ts",
                lineage_resolved=True,
            ),
        ),
    )
    span = EvidenceSpanRecord(
        id="span-devlens-ruling",
        alternative_id="alt-devlens-001",
        evidence_role=EvidenceRole.RULING,
        verification_status=VerificationStatus.VERIFIED_EXACT,
        exact_quote="We reject the full Babel AST parser alternative because its memory allocation model conflicts with Vercel serverless execution limits.",
        start_char=0,
        end_char=136,
        author_handle="MRIARC-08",
        author_contributor_id="MRIARC-08",
        permalink="https://github.com/MRIARC-08/DevLens/issues/1#issuecomment-6664661436",
    )
    store = InMemoryDecisionStore()
    store.add_decision(decision)
    store.add_alternative(alternative)
    store.add_evidence_span(span)
    return store


def test_devlens_typescript_violation_authorizes_warning():
    """True Positive: PR diff touches parser.ts and reintroduces @babel/parser."""
    violation_diff = """diff --git a/src/lib/pipeline/parser.ts b/src/lib/pipeline/parser.ts
--- a/src/lib/pipeline/parser.ts
+++ b/src/lib/pipeline/parser.ts
@@ -1,3 +1,5 @@
+import { parse } from "@babel/parser";
+
 export function parseSource(code: string) {
+    const ast = parse(code, { sourceType: "module" });
     return ast;
 }
"""
    store = _build_devlens_store()
    inspector = PRDiffInspector()
    pr_diff = inspector.parse_and_analyze(
        violation_diff, repository_url="https://github.com/MRIARC-08/DevLens"
    )
    retriever = CandidateRetriever(store)
    candidates = retriever.retrieve(pr_diff)
    gate = WarningAuthorizationGate(store)
    result = gate.evaluate(
        pr_diff,
        candidates,
        repository_url="https://github.com/MRIARC-08/DevLens",
        pr_number=101,
        commit_sha="a" * 40,
        execution_mode=ExecutionMode.AUDIT_DRY_RUN,
    )
    assert result.verdict == WatchdogVerdict.WARNING_AUTHORIZED
    assert "alt-devlens-001" in result.authorized_alternative_ids


def test_devlens_typescript_benign_refactor_abstains():
    """Hard Negative / Benign Refactor: Diff touches parser.ts but does not introduce @babel/parser."""
    benign_diff = """diff --git a/src/lib/pipeline/parser.ts b/src/lib/pipeline/parser.ts
--- a/src/lib/pipeline/parser.ts
+++ b/src/lib/pipeline/parser.ts
@@ -1,3 +1,5 @@
 export function parseSource(code: string) {
+    // Optimized line regex parsing under 50MB RAM
+    const lines = code.split("\\n");
     return lines;
 }
"""
    store = _build_devlens_store()
    inspector = PRDiffInspector()
    pr_diff = inspector.parse_and_analyze(
        benign_diff, repository_url="https://github.com/MRIARC-08/DevLens"
    )
    retriever = CandidateRetriever(store)
    candidates = retriever.retrieve(pr_diff)
    gate = WarningAuthorizationGate(store)
    result = gate.evaluate(
        pr_diff,
        candidates,
        repository_url="https://github.com/MRIARC-08/DevLens",
        pr_number=102,
        commit_sha="b" * 40,
        execution_mode=ExecutionMode.AUDIT_DRY_RUN,
    )
    assert result.verdict == WatchdogVerdict.ABSTAINED_UNCERTAIN


def test_single_lead_maintainer_quorum():
    """Project owner / lead maintainer satisfies single-maintainer quorum."""
    store = _build_devlens_store()
    alt = store.get_alternative("alt-devlens-001")
    assert alt is not None
    gate = WarningAuthorizationGate(store)
    err = gate._check_ruling_quorum(alt)
    assert err is None


def test_provider_aware_model_defaults():
    """Provider-aware defaults select Groq/OpenAI models when configured."""
    groq_client = LLMClient(
        base_url="https://api.groq.com/openai/v1",
        api_key="gsk-test",
    )
    pipeline_groq = ExtractionPipeline(client=groq_client)
    assert pipeline_groq.pass1.model == "llama-3.3-70b-versatile"
    assert pipeline_groq.pass3.model == "llama-3.3-70b-versatile"

    openai_client = LLMClient(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
    )
    pipeline_openai = ExtractionPipeline(client=openai_client)
    assert pipeline_openai.pass1.model == "gpt-4o-mini"


def test_fastmcp_directional_guidance_payload():
    """check_pr_diff returns structured directional guidance for the Switch reasoning agent."""
    store = _build_devlens_store()
    gate = WarningAuthorizationGate(store)
    retriever = CandidateRetriever(store)
    server = FastMCPServer(gate=gate, retriever=retriever)

    violation_diff = """diff --git a/src/lib/pipeline/parser.ts b/src/lib/pipeline/parser.ts
--- a/src/lib/pipeline/parser.ts
+++ b/src/lib/pipeline/parser.ts
@@ -1,2 +1,4 @@
+import { parse } from "@babel/parser";
 export function parseSource(code: string) { return parse(code); }
"""
    from biscuit.switch.types import CheckPRDiffInput

    params = CheckPRDiffInput(
        repository_url="https://github.com/MRIARC-08/DevLens",
        pr_number=101,
        diff_text=violation_diff,
        commit_sha="c" * 40,
    )
    response = server._check_pr_diff(params)
    assert response["authorization_verdict"] == "WARNING_AUTHORIZED"
    guidance = response["directional_guidance"]
    assert guidance is not None
    assert guidance["decision_id"] == "ADR-001"
    assert "@babel/parser" in guidance["prohibited_construct"]
    assert guidance["authoritative_ruling"]["author"] == "MRIARC-08"
    assert "Regex Line Parser" in guidance["accepted_alternative"]


def test_card_with_accepted_alternative_button():
    """Card renders Have Agent Apply Accepted Solution when alternative is provided."""
    builder = BlockKitBuilder()
    card = builder.build_pr_warning_card(
        repository_url="https://github.com/MRIARC-08/DevLens",
        pr_number=101,
        commit_sha="c" * 40,
        alternative_id="alt-devlens-001",
        proposal_text="Full Babel AST Parser",
        rejection_summary="Exceeds 1024MB RAM constraint",
        ruling_quote="We reject the full Babel AST parser",
        ruling_author="MRIARC-08",
        ruling_permalink="https://github.com/MRIARC-08/DevLens/issues/1#issuecomment-6664661436",
        comment_body="Warning comment",
        contradiction_analysis="PR introduces full Babel AST in serverless path",
        accepted_alternative="Regex Line Parser",
    )
    blocks_str = str(card["blocks"])
    assert "Have Agent Apply Accepted Solution" in blocks_str
    assert "Post to PR" in blocks_str
    assert "Keep Silent" in blocks_str
    assert "Contradiction analysis" in blocks_str
