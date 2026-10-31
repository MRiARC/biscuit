"""Unit tests for the PR Diff Watchdog & 4-Invariant Authorization Gate.

Conforms to Issue #20 (Stream 4) requirements:
* zero-dependency testing against synthetic diff fixtures, mock decision
  records and local FastMCP tool calls (no live Ollama models, no GitHub
  network access),
* candidate retrieval plus hard-negative filtering (a ternary-operator
  refactor must abstain),
* Invariant 2 blocks the warning when authority is MODEL_INFERRED,
* all four invariants evaluated and recorded per candidate,
* AUDIT_DRY_RUN default: authorized warnings are logged, never published.

The fixture mirrors the PEP 505 evaluation scenario
(docs/EVALUATION_AND_BENCHMARKS.md, section 2): the built-in ``coalesce()``
alternative was REJECTED by Core Maintainer consensus (ruling span post-112
by markshannon) and is anchored in ``Lib/builtins.py``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from biscuit.watchdog import (
    CANDIDATE_COSINE_THRESHOLD,
    AuditLog,
    CandidateMatch,
    CandidateRetriever,
    CodeAnchorRecord,
    DecisionAlternativeRecord,
    DecisionRecord,
    EvidenceRole,
    EvidenceSpanRecord,
    ExecutionMode,
    InMemoryDecisionStore,
    InvariantKey,
    PRCommentFormatter,
    PRDiffInspector,
    ProposalLifecycle,
    SignoffRecord,
    VerificationStatus,
    WarningAuthorizationGate,
    WatchdogAction,
    WatchdogVerdict,
    compute_sha256,
    structural_fingerprint,
)
from biscuit.watchdog.types import AnchorState

REPO = "https://github.com/python/cpython"

RULING_QUOTE = (
    "coalesce(a, expensive_call()) evaluates arguments before invocation, "
    "breaking the primary use case."
)

VIOLATION_DIFF = """diff --git a/Lib/builtins.py b/Lib/builtins.py
--- a/Lib/builtins.py
+++ b/Lib/builtins.py
@@ -10,4 +10,9 @@ def len(obj):
+def coalesce_impl(first, second):
+    if first is None:
+        return second
+    return coalesce(a, b)
"""

HARD_NEGATIVE_TERNARY_DIFF = """diff --git a/Lib/builtins.py b/Lib/builtins.py
--- a/Lib/builtins.py
+++ b/Lib/builtins.py
@@ -10,4 +10,7 @@ def len(obj):
+def pick(first, second):
+    return first if first is not None else second
+"""

UNRELATED_FILE_DIFF = """diff --git a/Lib/os.py b/Lib/os.py
--- a/Lib/os.py
+++ b/Lib/os.py
@@ -1,3 +1,5 @@
+import sys
+print(sys.platform)
"""


def build_store(
    lifecycle: ProposalLifecycle = ProposalLifecycle.REJECTED,
    ruling_authority: str | None = "CORE_MAINTAINER_CONSENSUS",
    ruling_verification: VerificationStatus = VerificationStatus.VERIFIED_EXACT,
    ruling_role: EvidenceRole = EvidenceRole.RULING,
    anchor_state: AnchorState = AnchorState.CURRENT,
    lineage_resolved: bool = True,
    anchor_fingerprint: str | None = None,
    signoffs: tuple[SignoffRecord, ...] | None = None,
    with_anchor: bool = True,
) -> InMemoryDecisionStore:
    """Assemble the mock decision knowledge base for one scenario."""
    fingerprint = (
        anchor_fingerprint
        if anchor_fingerprint is not None
        else structural_fingerprint("coalesce(a, b)")
    )
    anchor = CodeAnchorRecord(
        id="anchor-1",
        alternative_id="alt-1",
        repository_url=REPO,
        language="python",
        symbol_type="function",
        qualified_symbol_name="cpython.Builtins.coalesce",
        historical_file_path="Lib/builtins.py",
        anchor_state=anchor_state,
        head_file_path="Lib/builtins.py",
        ast_structural_fingerprint=fingerprint,
        lineage_resolved=lineage_resolved,
    )
    ruling = EvidenceSpanRecord(
        id="span-1",
        alternative_id="alt-1",
        evidence_role=ruling_role,
        verification_status=ruling_verification,
        exact_quote=RULING_QUOTE,
        start_char=0,
        end_char=len(RULING_QUOTE),
        author_handle="markshannon",
        author_contributor_id="c2",
        permalink="https://discuss.python.org/t/pep-505-none-aware-operators/post-112",
    )
    alternative = DecisionAlternativeRecord(
        id="alt-1",
        decision_id="dec-1",
        proposal_text="Built-in coalesce(*args) function",
        lifecycle_status=lifecycle,
        rejection_summary="Eager argument evaluation defeats short-circuiting semantics.",
        rejection_category="PERFORMANCE",
        ruling_evidence_id="span-1",
        ruling_authority=ruling_authority,
        ruling_actor_id="c2",
        ruling_signoffs=signoffs
        if signoffs is not None
        else (
            SignoffRecord(
                contributor_id="c2",
                role="Core Maintainer",
                project_context="python/cpython",
            ),
            SignoffRecord(
                contributor_id="c3",
                role="Core Maintainer",
                project_context="python/cpython",
            ),
        ),
        anchors=(anchor,) if with_anchor else (),
    )
    decision = DecisionRecord(
        id="dec-1",
        title="PEP-505: None-aware operators",
        context_summary="Debate over none-aware operator syntax and built-ins.",
        source_thread_url="https://discuss.python.org/t/pep-505-none-aware-operators",
    )
    return InMemoryDecisionStore(
        decisions=[decision], alternatives=[alternative], evidence_spans=[ruling]
    )


def make_pipeline(store: InMemoryDecisionStore):
    inspector = PRDiffInspector()
    retriever = CandidateRetriever(store=store)
    gate = WarningAuthorizationGate(store=store)
    return inspector, retriever, gate


def run_check(store: InMemoryDecisionStore, diff_text: str, **kwargs):
    inspector, retriever, gate = make_pipeline(store)
    pr_diff = inspector.parse_and_analyze(diff_text)
    candidates = retriever.retrieve(pr_diff)
    return gate.evaluate(pr_diff, candidates, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Inspector: unified diff parsing
# ─────────────────────────────────────────────────────────────────────────────


class TestPRDiffInspector:
    def test_parses_files_hunks_and_lines(self):
        pr_diff = PRDiffInspector().parse(VIOLATION_DIFF)
        assert len(pr_diff.files) == 1
        file = pr_diff.files[0]
        assert file.path == "Lib/builtins.py"
        assert file.old_path == "Lib/builtins.py"
        assert len(file.hunks) == 1
        hunk = file.hunks[0]
        assert hunk.new_start == 10
        assert hunk.old_start == 10
        added_stripped = [line.strip() for line in hunk.added_lines]
        assert "return coalesce(a, b)" in added_stripped
        assert "def coalesce_impl(first, second):" in added_stripped
        assert "def len(obj):" in hunk.section_heading

    def test_parses_multiple_files_and_renames(self):
        diff = """diff --git a/Lib/old.py b/Lib/new.py
similarity index 90%
rename from Lib/old.py
rename to Lib/new.py
--- a/Lib/old.py
+++ b/Lib/new.py
@@ -1,2 +1,3 @@
 ctx
-old
+new
+added
diff --git a/setup.py b/setup.py
new file mode 100644
--- /dev/null
+++ b/setup.py
@@ -0,0 +1,2 @@
+from setuptools import setup
+setup()
"""
        pr_diff = PRDiffInspector().parse(diff)
        assert [f.path for f in pr_diff.files] == ["Lib/new.py", "setup.py"]
        renamed, created = pr_diff.files
        assert renamed.is_rename and renamed.old_path == "Lib/old.py"
        assert renamed.removed_lines == ("old",)
        assert created.is_new_file and created.old_path is None
        assert created.added_lines == ("from setuptools import setup", "setup()")

    def test_no_newline_marker_ignored(self):
        diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,2 @@
 hello
+world
\\ No newline at end of file
"""
        pr_diff = PRDiffInspector().parse(diff)
        assert pr_diff.files[0].added_lines == ("world",)

    def test_deleted_file_has_no_added_lines(self):
        diff = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line1
-line2
"""
        pr_diff = PRDiffInspector().parse(diff)
        assert pr_diff.files[0].is_deleted_file
        assert pr_diff.files[0].added_lines == ()

    def test_empty_and_headerless_diffs_raise(self):
        with pytest.raises(ValueError):
            PRDiffInspector().parse("")
        with pytest.raises(ValueError):
            PRDiffInspector().parse("random text without headers")

    def test_touched_paths_and_added_text(self):
        pr_diff = PRDiffInspector().parse_and_analyze(VIOLATION_DIFF)
        assert pr_diff.touched_paths == frozenset({"Lib/builtins.py"})
        assert "coalesce(a, b)" in pr_diff.added_text


# ─────────────────────────────────────────────────────────────────────────────
# Structural fingerprints (Tier 3 masked-AST hashing)
# ─────────────────────────────────────────────────────────────────────────────


class TestStructuralFingerprints:
    def test_fingerprint_is_stable_across_variable_renames(self):
        assert structural_fingerprint("coalesce(a, b)") == structural_fingerprint(
            "coalesce(x, y)"
        )

    def test_fingerprint_is_stable_across_formatting(self):
        assert structural_fingerprint("coalesce( a , b )") == structural_fingerprint(
            "coalesce(a,b)"
        )

    def test_fingerprint_distinguishes_constructs(self):
        assert structural_fingerprint("coalesce(a, b)") != structural_fingerprint(
            "pick(a, b)"
        )

    def test_fingerprint_matches_subtree_anywhere_in_fragment(self):
        fragment = (
            "def coalesce_impl(first, second):\n"
            "    if first is None:\n"
            "        return second\n"
            "    return coalesce(a, b)\n"
        )
        anchor_fp = structural_fingerprint("coalesce(a, b)")
        pr_diff = PRDiffInspector().parse_and_analyze(
            "diff --git a/Lib/builtins.py b/Lib/builtins.py\n"
            "--- a/Lib/builtins.py\n+++ b/Lib/builtins.py\n@@ -1,0 +1,4 @@\n"
            + "".join(f"+{line}\n" for line in fragment.splitlines())
        )
        analysis = pr_diff.analysis_for("Lib/builtins.py")
        assert analysis is not None and analysis.parseable
        assert anchor_fp in analysis.subtree_fingerprints

    def test_unparseable_fragment_is_reported_not_guessed(self):
        diff = (
            "diff --git a/Lib/broken.py b/Lib/broken.py\n"
            "--- a/Lib/broken.py\n+++ b/Lib/broken.py\n@@ -1,0 +1,2 @@\n"
            "+def broken(:\n+    pass\n"
        )
        analysis = PRDiffInspector().parse_and_analyze(diff).analysis_for("Lib/broken.py")
        assert analysis is not None and not analysis.parseable

    def test_non_python_file_is_not_structurally_verified(self):
        diff = (
            "diff --git a/Parser/parser.c b/Parser/parser.c\n"
            "--- a/Parser/parser.c\n+++ b/Parser/parser.c\n@@ -1,0 +1,1 @@\n"
            "+int coalesce(void) { return 0; }\n"
        )
        analysis = PRDiffInspector().parse_and_analyze(diff).analysis_for("Parser/parser.c")
        assert analysis is not None
        assert analysis.language == "c"
        assert not analysis.parseable


# ─────────────────────────────────────────────────────────────────────────────
# Candidate retrieval (signal only)
# ─────────────────────────────────────────────────────────────────────────────


class TestCandidateRetriever:
    def test_anchor_path_hit_produces_candidate(self):
        store = build_store()
        pr_diff = PRDiffInspector().parse_and_analyze(VIOLATION_DIFF)
        matches = CandidateRetriever(store=store).retrieve(pr_diff)
        assert [m.alternative_id for m in matches] == ["alt-1"]
        assert "anchor:path" in matches[0].signals

    def test_unrelated_file_produces_no_candidates(self):
        store = build_store()
        pr_diff = PRDiffInspector().parse_and_analyze(UNRELATED_FILE_DIFF)
        assert CandidateRetriever(store=store).retrieve(pr_diff) == []

    def test_vector_similarity_is_retrieval_only(self):
        # A high cosine score retrieves a candidate …
        store = build_store()
        store._alternatives["alt-1"] = replace(
            store._alternatives["alt-1"], embedding=(1.0, 0.0, 0.0)
        )
        pr_diff = PRDiffInspector().parse_and_analyze(UNRELATED_FILE_DIFF)
        retriever = CandidateRetriever(store=store, embedder=lambda text: [1.0, 0.0, 0.0])
        matches = retriever.retrieve(pr_diff)
        assert matches, "vector hit alone should retrieve a candidate"
        assert matches[0].similarity == pytest.approx(1.0)
        assert any(s.startswith("vector:") for s in matches[0].signals)

        # … but must NOT authorize a warning: the diff touches neither the
        # alternative's anchor nor the rejected construct, so invariants 3
        # and 4 fail regardless of the similarity score.
        gate = WarningAuthorizationGate(store=store)
        result = gate.evaluate(pr_diff, matches, repository_url=REPO, pr_number=1)
        assert result.verdict != WatchdogVerdict.WARNING_AUTHORIZED
        evaluation = result.candidate_evaluations[0]
        assert not evaluation.check(InvariantKey.ALTERNATIVE_CODE_ANCHOR).satisfied
        assert not evaluation.check(InvariantKey.AST_STRUCTURAL_EQUIVALENCE).satisfied

    def test_similarity_below_threshold_not_retrieved(self):
        store = build_store()
        store._alternatives["alt-1"] = replace(
            store._alternatives["alt-1"], embedding=(0.0, 1.0, 0.0)
        )
        pr_diff = PRDiffInspector().parse_and_analyze(UNRELATED_FILE_DIFF)
        retriever = CandidateRetriever(store=store, embedder=lambda text: [1.0, 0.0, 0.0])
        assert retriever.retrieve(pr_diff) == []

    def test_threshold_constant_is_the_documented_value(self):
        assert CANDIDATE_COSINE_THRESHOLD == 0.82


# ─────────────────────────────────────────────────────────────────────────────
# The 4-invariant Warning Authorization Gate
# ─────────────────────────────────────────────────────────────────────────────


class TestWarningAuthorizationGate:
    def test_all_four_invariants_authorize_violation(self):
        result = run_check(
            build_store(),
            VIOLATION_DIFF,
            repository_url=REPO,
            pr_number=412,
            commit_sha="a" * 40,
        )
        assert result.verdict == WatchdogVerdict.WARNING_AUTHORIZED
        assert result.invariants_satisfied is True
        evaluation = result.candidate_evaluations[0]
        assert evaluation.authorized
        for key in InvariantKey:
            assert evaluation.check(key).satisfied, key

    def test_hard_negative_ternary_refactor_abstains(self):
        """Required scenario: ternary-operator refactor must correctly abstain."""
        result = run_check(build_store(), HARD_NEGATIVE_TERNARY_DIFF)
        assert result.verdict == WatchdogVerdict.ABSTAINED_UNCERTAIN
        evaluation = result.candidate_evaluations[0]
        assert evaluation.check(InvariantKey.DECISION_STATE).satisfied
        assert evaluation.check(InvariantKey.AUTHORITATIVE_RULING).satisfied
        assert evaluation.check(InvariantKey.ALTERNATIVE_CODE_ANCHOR).satisfied
        # The added code is a conditional expression, not the rejected call.
        assert not evaluation.check(InvariantKey.AST_STRUCTURAL_EQUIVALENCE).satisfied

    def test_invariant_1_blocks_non_rejected_lifecycle(self):
        for lifecycle in (
            ProposalLifecycle.UNDER_DISCUSSION,
            ProposalLifecycle.DISPUTED,
            ProposalLifecycle.UNRESOLVED,
            ProposalLifecycle.PROPOSED,
        ):
            result = run_check(build_store(lifecycle=lifecycle), VIOLATION_DIFF)
            assert result.verdict != WatchdogVerdict.WARNING_AUTHORIZED, lifecycle
            check = result.candidate_evaluations[0].check(InvariantKey.DECISION_STATE)
            assert not check.satisfied

    def test_invariant_2_blocks_model_inferred_authority(self):
        """Required scenario: MODEL_INFERRED authority must block the warning."""
        result = run_check(
            build_store(ruling_authority="MODEL_INFERRED"), VIOLATION_DIFF
        )
        assert result.verdict != WatchdogVerdict.WARNING_AUTHORIZED
        check = result.candidate_evaluations[0].check(InvariantKey.AUTHORITATIVE_RULING)
        assert not check.satisfied
        assert "model inference is never authority" in check.detail

    def test_invariant_2_blocks_unverified_ruling(self):
        result = run_check(
            build_store(ruling_verification=VerificationStatus.UNVERIFIED),
            VIOLATION_DIFF,
        )
        check = result.candidate_evaluations[0].check(InvariantKey.AUTHORITATIVE_RULING)
        assert not check.satisfied
        assert "UNVERIFIED" in check.detail

    def test_invariant_2_blocks_non_ruling_evidence_role(self):
        result = run_check(
            build_store(ruling_role=EvidenceRole.OBJECTION), VIOLATION_DIFF
        )
        check = result.candidate_evaluations[0].check(InvariantKey.AUTHORITATIVE_RULING)
        assert not check.satisfied

    def test_invariant_2_quorum_rules(self):
        # Consensus without a second distinct maintainer sign-off fails.
        single = (
            SignoffRecord(
                contributor_id="c2",
                role="Core Maintainer",
                project_context="python/cpython",
            ),
        )
        result = run_check(build_store(signoffs=single), VIOLATION_DIFF)
        check = result.candidate_evaluations[0].check(InvariantKey.AUTHORITATIVE_RULING)
        assert not check.satisfied
        assert "distinct" in check.detail

        # A sign-off from another project does not count.
        cross_project = (
            SignoffRecord(
                contributor_id="c2",
                role="Core Maintainer",
                project_context="python/cpython",
            ),
            SignoffRecord(
                contributor_id="c9",
                role="Core Maintainer",
                project_context="kubernetes/kubernetes",
            ),
        )
        result = run_check(build_store(signoffs=cross_project), VIOLATION_DIFF)
        assert not result.candidate_evaluations[0].check(
            InvariantKey.AUTHORITATIVE_RULING
        ).satisfied

        # A revoked sign-off does not count.
        revoked = (
            SignoffRecord(
                contributor_id="c2",
                role="Core Maintainer",
                project_context="python/cpython",
            ),
            SignoffRecord(
                contributor_id="c3",
                role="Core Maintainer",
                project_context="python/cpython",
                active=False,
            ),
        )
        result = run_check(build_store(signoffs=revoked), VIOLATION_DIFF)
        assert not result.candidate_evaluations[0].check(
            InvariantKey.AUTHORITATIVE_RULING
        ).satisfied

    def test_invariant_2_bdfl_ruling_succeeds_with_single_verified_bdfl(self):
        store = build_store(
            ruling_authority="BDFL",
            signoffs=(
                SignoffRecord(
                    contributor_id="c2", role="BDFL", project_context="python/cpython"
                ),
            ),
        )
        result = run_check(store, VIOLATION_DIFF)
        assert result.verdict == WatchdogVerdict.WARNING_AUTHORIZED

    def test_invariant_2_working_group_requires_chair_plus_member(self):
        chair_only = (
            SignoffRecord(
                contributor_id="c2",
                role="Working Group Chair",
                project_context="python/cpython",
            ),
        )
        result = run_check(
            build_store(ruling_authority="WORKING_GROUP", signoffs=chair_only),
            VIOLATION_DIFF,
        )
        check = result.candidate_evaluations[0].check(InvariantKey.AUTHORITATIVE_RULING)
        assert not check.satisfied

        chair_and_member = chair_only + (
            SignoffRecord(
                contributor_id="c4",
                role="Working Group Member",
                project_context="python/cpython",
            ),
        )
        result = run_check(
            build_store(ruling_authority="WORKING_GROUP", signoffs=chair_and_member),
            VIOLATION_DIFF,
        )
        assert result.verdict == WatchdogVerdict.WARNING_AUTHORIZED

    @pytest.mark.parametrize(
        "anchor_state",
        [AnchorState.HISTORICAL, AnchorState.SPLIT, AnchorState.AMBIGUOUS, AnchorState.DELETED],
    )
    def test_invariant_3_blocks_inactive_anchor_states(self, anchor_state):
        result = run_check(build_store(anchor_state=anchor_state), VIOLATION_DIFF)
        check = result.candidate_evaluations[0].check(InvariantKey.ALTERNATIVE_CODE_ANCHOR)
        assert not check.satisfied

    def test_invariant_3_blocks_ambiguous_lineage(self):
        result = run_check(build_store(lineage_resolved=False), VIOLATION_DIFF)
        check = result.candidate_evaluations[0].check(InvariantKey.ALTERNATIVE_CODE_ANCHOR)
        assert not check.satisfied
        assert "ambiguous" in check.detail

    def test_invariant_3_blocks_missing_anchor(self):
        """No anchor → retrieval finds nothing → NO_REJECTED_MATCH, not a warning."""
        result = run_check(build_store(with_anchor=False), VIOLATION_DIFF)
        assert result.verdict == WatchdogVerdict.NO_REJECTED_MATCH
        assert result.candidate_evaluations == ()

    def test_invariant_4_blocks_when_anchor_fingerprint_differs(self):
        result = run_check(
            build_store(anchor_fingerprint=structural_fingerprint("zip(a, b)")),
            VIOLATION_DIFF,
        )
        check = result.candidate_evaluations[0].check(InvariantKey.AST_STRUCTURAL_EQUIVALENCE)
        assert not check.satisfied

    def test_renamed_variables_still_detected(self):
        diff = (
            "diff --git a/Lib/builtins.py b/Lib/builtins.py\n"
            "--- a/Lib/builtins.py\n+++ b/Lib/builtins.py\n@@ -1,0 +1,2 @@\n"
            "+def run(x, y):\n"
            "+    return coalesce(x, y)\n"
        )
        result = run_check(build_store(), diff)
        assert result.verdict == WatchdogVerdict.WARNING_AUTHORIZED

    def test_unrelated_diff_has_no_candidates(self):
        result = run_check(build_store(), UNRELATED_FILE_DIFF)
        assert result.verdict == WatchdogVerdict.NO_REJECTED_MATCH
        assert result.candidate_evaluations == ()

    def test_cross_alternative_association_yields_mismatch(self):
        """Semantic candidate (vector-only) whose anchors are untouched while
        the diff touches another alternative's anchor: cross-alternative
        association must not warn (docs/CODE_GROUNDING_AND_AST.md, section 1)."""
        store = InMemoryDecisionStore()
        store.add_alternative(
            DecisionAlternativeRecord(
                id="alt-grammar",
                decision_id="dec-1",
                proposal_text="Unary ? operator",
                lifecycle_status=ProposalLifecycle.REJECTED,
                ruling_authority="BDFL",
                anchors=(
                    CodeAnchorRecord(
                        id="a-grammar",
                        alternative_id="alt-grammar",
                        repository_url=REPO,
                        language="python",
                        symbol_type="grammar_rule",
                        qualified_symbol_name="cpython.Grammar.unary_qmark",
                        historical_file_path="Grammar/python.gram",
                        anchor_state=AnchorState.CURRENT,
                    ),
                ),
            )
        )
        store.add_alternative(
            DecisionAlternativeRecord(
                id="alt-coalesce",
                decision_id="dec-1",
                proposal_text="coalesce()",
                lifecycle_status=ProposalLifecycle.REJECTED,
                ruling_authority="BDFL",
                anchors=(
                    CodeAnchorRecord(
                        id="a-bltin",
                        alternative_id="alt-coalesce",
                        repository_url=REPO,
                        language="python",
                        symbol_type="function",
                        qualified_symbol_name="cpython.Builtins.coalesce",
                        historical_file_path="Python/bltinmodule.c",
                        anchor_state=AnchorState.CURRENT,
                        ast_structural_fingerprint=structural_fingerprint("coalesce(a, b)"),
                    ),
                ),
            )
        )
        diff = (
            "diff --git a/Python/bltinmodule.c b/Python/bltinmodule.c\n"
            "--- a/Python/bltinmodule.c\n+++ b/Python/bltinmodule.c\n@@ -1,0 +1,2 @@\n"
            "+def helper(x):\n"
            "+    return zip(x)\n"
        )
        pr_diff = PRDiffInspector().parse_and_analyze(diff)
        gate = WarningAuthorizationGate(store=store)
        vector_only = CandidateMatch(
            alternative_id="alt-grammar",
            similarity=0.9,
            matched_anchor_ids=(),
            matched_symbols=(),
            signals=("vector:cosine>=0.82",),
        )
        result = gate.evaluate(pr_diff, [vector_only], repository_url=REPO, pr_number=1)
        assert result.verdict == WatchdogVerdict.ALTERNATIVE_MISMATCH
        assert not result.candidate_evaluations[0].check(
            InvariantKey.ALTERNATIVE_CODE_ANCHOR
        ).satisfied

    def test_failed_invariants_are_all_recorded(self):
        """Every invariant is evaluated even after one fails (audit trail)."""
        result = run_check(
            build_store(ruling_authority="MODEL_INFERRED", anchor_state=AnchorState.SPLIT),
            VIOLATION_DIFF,
        )
        evaluation = result.candidate_evaluations[0]
        assert len(evaluation.invariant_checks) == 4
        assert len(evaluation.failed_invariants) == 2
        assert not evaluation.authorized


# ─────────────────────────────────────────────────────────────────────────────
# Execution modes & audit logging
# ─────────────────────────────────────────────────────────────────────────────


class TestExecutionModesAndAudit:
    def test_dry_run_is_the_default_and_only_logs(self):
        result = run_check(
            build_store(), VIOLATION_DIFF, repository_url=REPO, pr_number=412
        )
        assert result.execution_mode == ExecutionMode.AUDIT_DRY_RUN
        assert result.action_taken == WatchdogAction.LOGGED_ONLY
        assert result.comment_markdown is not None  # rendered but not published

    def test_interactive_hitl_posts_card_and_never_publishes(self):
        result = run_check(
            build_store(),
            VIOLATION_DIFF,
            repository_url=REPO,
            pr_number=412,
            execution_mode=ExecutionMode.INTERACTIVE_HITL,
        )
        assert result.action_taken == WatchdogAction.HITL_CARD_POSTED
        assert result.hitl_card_payload is not None
        assert result.hitl_card_payload["pr_number"] == 412
        assert result.hitl_card_payload["card_kind"] == "PR_WARNING_APPROVAL"
        assert len(result.hitl_card_payload["comment_body_sha256"]) == 64

    def test_production_comment_mode_publishes(self):
        result = run_check(
            build_store(),
            VIOLATION_DIFF,
            execution_mode=ExecutionMode.PRODUCTION_COMMENT,
        )
        assert result.action_taken == WatchdogAction.COMMENT_PUBLISHED

    def test_abstained_results_log_only_in_every_mode(self):
        for mode in ExecutionMode:
            result = run_check(
                build_store(),
                HARD_NEGATIVE_TERNARY_DIFF,
                execution_mode=mode,
            )
            assert result.action_taken == WatchdogAction.LOGGED_ONLY

    def test_audit_log_is_append_only_and_complete(self):
        store = build_store()
        inspector, retriever, gate = make_pipeline(store)
        gate.evaluate(
            inspector.parse_and_analyze(VIOLATION_DIFF),
            retriever.retrieve(inspector.parse_and_analyze(VIOLATION_DIFF)),
            repository_url=REPO,
            pr_number=412,
            commit_sha="f" * 40,
        )
        gate.evaluate(
            inspector.parse_and_analyze(HARD_NEGATIVE_TERNARY_DIFF),
            retriever.retrieve(inspector.parse_and_analyze(HARD_NEGATIVE_TERNARY_DIFF)),
            repository_url=REPO,
            pr_number=413,
            commit_sha="e" * 40,
        )
        entries = gate.audit_log.entries
        assert len(entries) == 2
        first, second = entries
        assert first.authorization_verdict == WatchdogVerdict.WARNING_AUTHORIZED
        assert second.authorization_verdict == WatchdogVerdict.ABSTAINED_UNCERTAIN
        assert first.candidate_alternative_ids == ("alt-1",)
        assert first.commit_sha == "f" * 40
        payload = first.to_json_dict()
        assert payload["execution_mode"] == "AUDIT_DRY_RUN"
        assert payload["authorization_verdict"] == "WARNING_AUTHORIZED"
        assert payload["action_taken"] == "LOGGED_ONLY"

    def test_shared_audit_log_collects_across_gates(self):
        log = AuditLog()
        for _ in range(2):
            store = build_store()
            gate = WarningAuthorizationGate(store=store, audit_log=log)
            pr_diff = PRDiffInspector().parse_and_analyze(VIOLATION_DIFF)
            gate.evaluate(
                pr_diff, CandidateRetriever(store=store).retrieve(pr_diff)
            )
        assert len(log.entries) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Comment formatter
# ─────────────────────────────────────────────────────────────────────────────


class TestPRCommentFormatter:
    def test_comment_contains_verifiable_citation(self):
        result = run_check(
            build_store(), VIOLATION_DIFF, repository_url=REPO, pr_number=412
        )
        markdown = result.comment_markdown
        assert markdown is not None
        assert RULING_QUOTE in markdown
        assert "markshannon" in markdown
        assert "post-112" in markdown  # permalink
        assert f"[0, {len(RULING_QUOTE)})" in markdown  # zero-based offsets
        assert "PEP-505" in markdown
        assert "REJECTED" in markdown
        assert "Lib/builtins.py" in markdown
        assert "AUDIT_DRY_RUN" in markdown

    def test_comment_payload_is_json_serializable_and_pinned(self):
        result = run_check(
            build_store(), VIOLATION_DIFF, repository_url=REPO, pr_number=412
        )
        payload = result.comment_payload
        assert payload is not None
        assert payload["pr_number"] == 412
        assert payload["ruling_evidence"]["verification_status"] == "VERIFIED_EXACT"
        assert payload["ruling_evidence"]["start_char"] == 0
        assert payload["ruling_evidence"]["end_char"] == len(RULING_QUOTE)
        assert payload["code_anchor"]["path"] == "Lib/builtins.py"
        assert payload["lifecycle_status"] == "REJECTED"

    def test_hashes_follow_the_documented_convention(self):
        import json

        digest = compute_sha256(RULING_QUOTE)
        assert len(digest) == 64
        assert digest == compute_sha256(RULING_QUOTE)

        result = run_check(
            build_store(), VIOLATION_DIFF, repository_url=REPO, pr_number=412
        )
        assert result.comment_payload is not None
        assert json.dumps(result.comment_payload)  # fully JSON serializable

    def test_formatter_handles_missing_context(self):
        store = build_store()
        alternative = store.get_alternative("alt-1")
        formatter = PRCommentFormatter()
        markdown = formatter.format_comment(
            alternative=alternative,
            decision=None,
            anchor=None,
            ruling_span=None,
            execution_mode=ExecutionMode.AUDIT_DRY_RUN,
        )
        assert "No verified ruling quote." in markdown
        assert "no recorded authority" not in markdown  # authority exists here


# ─────────────────────────────────────────────────────────────────────────────
# Guardrail precision scenario (docs/EVALUATION_AND_BENCHMARKS.md, Metric 6)
# ─────────────────────────────────────────────────────────────────────────────


class TestWarningPrecisionScenario:
    """Held-out PR suite in miniature: violations must warn, benign refactors
    and ambiguous edits must abstain — 0% false alarms, 100% abstention."""

    def test_violation_benign_ambiguous_mix(self):
        violations = [VIOLATION_DIFF]
        benign = [
            HARD_NEGATIVE_TERNARY_DIFF,
            # formatting-only change in the anchored file
            (
                "diff --git a/Lib/builtins.py b/Lib/builtins.py\n"
                "--- a/Lib/builtins.py\n+++ b/Lib/builtins.py\n@@ -1,0 +1,1 @@\n"
                "+x = (1, 2)\n"
            ),
            # unrelated symbol added in the anchored file
            (
                "diff --git a/Lib/builtins.py b/Lib/builtins.py\n"
                "--- a/Lib/builtins.py\n+++ b/Lib/builtins.py\n@@ -1,0 +1,1 @@\n"
                "+def unrelated(a):\n    return a\n"
            ),
        ]
        ambiguous = [
            # disputed lifecycle: no rejected alternative to match at all
            (build_store(lifecycle=ProposalLifecycle.DISPUTED), WatchdogVerdict.NO_REJECTED_MATCH),
            # model-inferred authority: never a ruling
            (
                build_store(ruling_authority="MODEL_INFERRED"),
                WatchdogVerdict.ABSTAINED_UNCERTAIN,
            ),
            # unverified ruling span: never authority
            (
                build_store(ruling_verification=VerificationStatus.UNVERIFIED),
                WatchdogVerdict.ABSTAINED_UNCERTAIN,
            ),
        ]

        for diff in violations:
            result = run_check(build_store(), diff)
            assert result.verdict == WatchdogVerdict.WARNING_AUTHORIZED

        for diff in benign:
            result = run_check(build_store(), diff)
            assert result.verdict != WatchdogVerdict.WARNING_AUTHORIZED, diff
            assert result.action_taken == WatchdogAction.LOGGED_ONLY

        for store, expected_verdict in ambiguous:
            result = run_check(store, VIOLATION_DIFF)
            assert result.verdict == expected_verdict
            assert result.action_taken == WatchdogAction.LOGGED_ONLY
