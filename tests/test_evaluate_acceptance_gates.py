"""Integration-level acceptance gates: complete CRA and trusted watchdog proof."""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy

from biscuit.evaluate.fixtures import (
    DEFAULT_FIXTURE_ROOT,
    load_pr_authorization_oracle,
    load_pr_expected_alternatives,
    load_pr_labels,
    load_simulated_predictions,
    load_thread,
)
from biscuit.evaluate.metrics import causal_rationale_alignment, pr_warning_metrics
from biscuit.evaluate.runner import run_benchmark


def _metric(outcome, name: str):
    return next(metric for metric in outcome.metrics if metric.name == name)


def _trusted_ruling():
    """Synthetic source records stand in for independently verified storage."""
    raw = next(
        row["raw_text"] for row in load_thread("pep-505")["messages"]
        if row["message_id"] == "post-s1"
    )
    alternative = "pep-505:S1"
    version_id = "source-version-0001"
    evidence = {
        "source_message_id": "post-s1",
        "source_message_version_id": version_id,
        "alternative_id": alternative,
        "author_handle": "human-author",
        "raw_text": raw,
        "source_content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "start_char": 0,
        "end_char": len(raw),
        "quote_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "review_status": "HUMAN_VERIFIED",
        "evidence_role": "RULING",
        "is_edited": False,
    }
    warning = {
        "case_id": "pr-01",
        "alternative_id": alternative,
        "authorization": "VERIFIED_HUMAN_RULING",
        "ruling_source_message_id": "post-s1",
        "ruling_source_message_version_id": version_id,
        "ruling_quote_sha256": evidence["quote_sha256"],
    }
    return warning, evidence


def _score_warning(
    warning: dict, proofs: list[dict], roles: dict[str, str],
):
    return pr_warning_metrics(
        load_pr_labels(), [warning["case_id"]], warning_records=[warning],
        expected_alternatives=load_pr_expected_alternatives(),
        expected_rulings=load_pr_authorization_oracle(),
        trusted_ruling_proofs={"pep-505:S1": proofs},
        verified_project_roles=roles,
        allow_simulated_authorization=False,
    )


def test_cra_reports_missing_vectors_without_rewarding_partial_coverage() -> None:
    result = causal_rationale_alignment(
        {"A": [1.0, 0.0]},
        {"A": [1.0, 0.0], "B": [0.0, 1.0]},
    )
    assert result["cra"] == 1.0
    assert result["coverage"] == 0.5
    assert result["missing_proposal_ids"] == ["B"]


def test_high_cra_on_single_recovered_proposal_fails_benchmark() -> None:
    prediction = deepcopy(load_simulated_predictions("pep-505"))
    del prediction["proposals"][1]["rationale_text"]
    outcome = run_benchmark("pep-505", predictor=lambda _: prediction)
    assert _metric(outcome, "Causal rationale alignment").status == "PASSED"
    assert _metric(outcome, "Causal rationale coverage").value == 0.5
    assert _metric(outcome, "Causal rationale coverage").status == "FAILED"
    assert outcome.verdict == "FAILED"


def test_partial_semantic_reference_vectors_do_not_overstate_coverage(tmp_path) -> None:
    root = tmp_path / "fixtures"
    shutil.copytree(DEFAULT_FIXTURE_ROOT, root)
    ground_truth = root / "pep-505" / "ground_truth.json"
    data = json.loads(ground_truth.read_text(encoding="utf-8"))
    data["proposals"][0]["rationale_embedding_model"] = "test-embed"
    data["proposals"][0]["rationale_embedding"] = [1.0, 0.0]
    ground_truth.write_text(json.dumps(data), encoding="utf-8")

    class Embed:
        model = "test-embed"

        def __call__(self, text: str):
            return [1.0, 0.0]

    outcome = run_benchmark(
        "pep-505", fixtures_root=root, recorded_replay=True,
        semantic_embedder=Embed(),
    )
    assert _metric(outcome, "Causal rationale coverage").value == 0.5
    assert outcome.verdict == "FAILED"


def test_production_warning_requires_independent_exact_verified_bdfl_ruling() -> None:
    warning, ruling = _trusted_ruling()
    approved = _score_warning(warning, [ruling], {"human-author": "bdfl"})
    assert (approved["tp"], approved["fp"], approved["fn"]) == (1, 0, 4)
    assert approved["audit_rows"][0]["reason_code"] == "AUTHORIZED_REJECTED_PATTERN"


def test_production_claims_fail_without_trusted_roles_and_evidence() -> None:
    warning, ruling = _trusted_ruling()
    for proofs, roles in [
        ([], {"human-author": "bdfl"}),
        ([ruling], {}),
        ([ruling], {"human-author": "contributor"}),
        ([{**ruling, "review_status": "MODEL_INFERRED"}], {"human-author": "bdfl"}),
        ([{**ruling, "is_edited": True}], {"human-author": "bdfl"}),
        ([{**ruling, "start_char": 1}], {"human-author": "bdfl"}),
        ([{**ruling, "source_content_sha256": "0" * 64}], {"human-author": "bdfl"}),
        ([{**ruling, "alternative_id": "another-alternative"}],
         {"human-author": "bdfl"}),
    ]:
        result = _score_warning(warning, proofs, roles)
        assert (result["tp"], result["fp"]) == (0, 1)


def test_production_wrong_version_digest_and_alternative_never_pass() -> None:
    warning, ruling = _trusted_ruling()
    for changed in [
        {"ruling_source_message_version_id": "wrong-version"},
        {"ruling_quote_sha256": "0" * 64},
        {"alternative_id": "pep-572:P1"},
        {"ruling_source_message_id": "different-post"},
    ]:
        result = _score_warning({**warning, **changed}, [ruling], {
            "human-author": "bdfl",
        })
        assert result["tp"] == 0


def test_two_independently_approved_maintainers_needed_for_consensus() -> None:
    warning, ruling = _trusted_ruling()
    member = {
        **ruling,
        "source_message_id": "post-s2",
        "source_message_version_id": "source-version-0002",
        "author_handle": "second-author",
        "evidence_role": "SIGNOFF",
    }
    roles = {"human-author": "maintainer", "second-author": "maintainer"}
    assert _score_warning(warning, [ruling], roles)["tp"] == 0
    assert _score_warning(warning, [ruling, member], roles)["tp"] == 1
    # Reusing the ruling author's identity cannot constitute consensus.
    assert _score_warning(
        warning, [ruling, {**member, "author_handle": "human-author"}], roles,
    )["tp"] == 0


def test_predicted_auth_string_alone_is_not_production_proof() -> None:
    prediction = deepcopy(load_simulated_predictions("pep-505"))
    prediction.pop("fixture_kind")
    prediction["warning_records"][0]["authorization"] = "VERIFIED_HUMAN_RULING"
    result = run_benchmark("pep-505", predictor=lambda _: prediction)
    assert result.prediction_source == "PREDICTOR"
    assert result.counts["watchdog"]["tp"] == 0
    assert result.counts["watchdog"]["fp"] == 5
    assert result.verdict == "FAILED"


def test_runner_passes_scorer_side_trusted_proofs_without_leaking_to_predictor() -> None:
    warning, proof = _trusted_ruling()
    prediction = deepcopy(load_simulated_predictions("pep-505"))
    prediction.pop("fixture_kind")
    prediction["warned_pr_ids"] = ["pr-01"]
    prediction["warning_records"] = [warning]

    def predictor(source):
        assert "trusted_ruling_proofs" not in source
        assert "verified_project_roles" not in source
        return prediction

    outcome = run_benchmark(
        "pep-505", predictor=predictor,
        trusted_ruling_proofs={"pep-505:S1": [proof]},
        verified_project_roles={"human-author": "bdfl"},
    )
    assert outcome.prediction_source == "PREDICTOR"
    assert outcome.counts["watchdog"]["tp"] == 1
    assert outcome.counts["watchdog"]["fp"] == 0
    assert outcome.counts["watchdog"]["fn"] == 4
    assert outcome.verdict == "FAILED"  # 4/5 violations remain unhandled
