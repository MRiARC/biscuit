"""Offline regression tests: no model, PostgreSQL, Ollama, or network dependencies."""

from __future__ import annotations

import hashlib
import json

import pytest

from biscuit.evaluate.fixtures import (
    DEFAULT_FIXTURE_ROOT,
    canonical_thread_bytes,
    load_ground_truth,
    load_pr_authorization_oracle,
    load_pr_cases,
    load_pr_expected_alternatives,
    load_pr_labels,
    load_simulated_predictions,
    load_thread,
    predictor_input,
    thread_digest,
    verify_manifest,
    verify_thread,
)
from biscuit.evaluate.metrics import (
    alternative_disambiguation_rate,
    ast_grounding_precision,
    causal_rationale_alignment,
    cosine_similarity,
    exact_evidence_match,
    hashed_text_embedding,
    lifecycle_accuracy,
    pr_warning_metrics,
    proposal_recovery,
)
from biscuit.evaluate.runner import run_benchmark


def test_proposal_f1_penalizes_false_positive_and_missing() -> None:
    result = proposal_recovery(["P1", "P2", "fake"], ["P1", "P2", "P3"])
    assert result["precision"] == pytest.approx(2 / 3)
    assert result["recall"] == pytest.approx(2 / 3)
    assert result["f1"] == pytest.approx(2 / 3)
    assert result["fp"] == result["fn"] == 1


def test_proposal_empty_is_not_perfect() -> None:
    assert proposal_recovery([], ["P1"])["f1"] == 0.0
    assert proposal_recovery([], [])["f1"] is None


def test_multiclass_status_and_critical_false_rejection() -> None:
    truth = {"A": "REJECTED", "B": "DEFERRED", "C": "DISPUTED"}
    result = lifecycle_accuracy({"A": "REJECTED", "B": "REJECTED"}, truth)
    assert result["accuracy"] == pytest.approx(1 / 3)
    assert result["false_rejection_rate"] == pytest.approx(1 / 2)
    assert result["confusion_matrix"]["DISPUTED"]["MISSING"] == 1


def test_quote_single_character_difference_zero_emr() -> None:
    quote = "Ruling: rejected."
    raw = {"m1": quote}
    tampered = "Ruling: rejected!"
    span = {
        "message_id": "m1", "start_char": 0, "end_char": len(quote),
        "quote": tampered, "sha256": hashlib.sha256(tampered.encode()).hexdigest(),
    }
    result = exact_evidence_match([span], raw)
    assert result["emr"] == 0.0
    assert result["invalid_citation_rate"] == 1.0


def test_quote_hash_mismatch_also_zero_emr() -> None:
    quote = "verified ✓"
    assert exact_evidence_match(
        [{"message_id": "x", "start_char": 0, "end_char": len(quote),
          "quote": quote, "sha256": "0" * 64}],
        {"x": quote},
    )["emr"] == 0.0
    assert exact_evidence_match(
        [{"message_id": "x", "start_char": 0, "end_char": len(quote),
          "quote": quote, "sha256": hashlib.sha256(quote.encode()).hexdigest()}],
        {"x": quote},
    )["emr"] == 1.0


def test_anchor_precision_requires_specific_alternative_and_commit() -> None:
    truth = [{
        "proposal_id": "S1", "file_path": "Grammar/python.gram",
        "symbol_identifier": "", "ast_node_type": "unary_op", "historical_commit": "a",
    }]
    found = [dict(truth[0]), dict(truth[0], proposal_id="S2")]
    assert ast_grounding_precision(found, truth)["precision"] == 0.5
    assert ast_grounding_precision([dict(truth[0], historical_commit="b")], truth)[
        "precision"
    ] == 0.0


def test_cosine_similarity_with_supplied_embeddings() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
    assert causal_rationale_alignment({"S1": [1, 0]}, {"S1": [1, 0]})[
        "cra"
    ] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="Zero-length"):
        cosine_similarity([0, 0], [1, 1])


def test_20_cases_are_label_isolated_and_have_expected_distribution() -> None:
    cases = load_pr_cases()
    labels = load_pr_labels()
    assert len(cases) == len(labels) == 20
    assert len({case["case_id"] for case in cases}) == 20
    assert [list(labels.values()).count(v) for v in ("violation", "benign", "ambiguous")] == [
        5, 10, 5,
    ]
    for case in cases:
        assert "expected_warning" not in case
        assert "label" not in case
        assert "category" not in case
        assert case["files"][0]["diff"]


def test_pr_precision_no_false_positives_and_all_ambiguous_abstain() -> None:
    labels = load_pr_labels()
    only_violations = [case_id for case_id, label in labels.items() if label == "violation"]
    result = pr_warning_metrics(labels, only_violations)
    assert result["precision"] == 1.0
    assert result["benign_false_alarm_rate"] == 0.0
    assert result["ambiguous_abstention_rate"] == 1.0
    assert (result["tp"], result["fp"], result["fn"]) == (5, 0, 0)
    noisy = pr_warning_metrics(labels, only_violations + ["pr-06", "pr-16"])
    assert noisy["precision"] == pytest.approx(5 / 7)
    assert noisy["benign_false_alarm_rate"] == pytest.approx(0.1)
    assert noisy["ambiguous_abstention_rate"] == pytest.approx(0.8)


def test_never_credit_empty_warning_predictions_as_success() -> None:
    scores = pr_warning_metrics(load_pr_labels(), [])
    assert scores["precision"] == 0.0
    assert scores["recall"] == 0.0


def test_thread_checksum_detects_tampering_and_is_order_independent() -> None:
    source = load_thread("pep-505")
    assert verify_thread(source) == source["thread_sha256"]
    assert canonical_thread_bytes(source).startswith(b"{")
    assert thread_digest({**source, "messages": list(reversed(source["messages"]))}) == (
        source["thread_sha256"]
    )
    edited = json.loads(json.dumps(source))
    edited["messages"][0]["raw_text"] += "!"
    with pytest.raises(ValueError, match="Message SHA-256"):
        verify_thread(edited)
    edited = json.loads(json.dumps(source))
    edited["thread_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Thread SHA-256"):
        verify_thread(edited)


def test_predictor_receives_no_ground_truth_or_pr_labels() -> None:
    raw = load_thread("pep-505")
    data = predictor_input(raw)
    flattened = json.dumps(data).lower()
    assert "ground_truth_status" not in flattened
    assert "expected_warning" not in flattened
    assert "pr_labels" not in flattened
    assert len(data["pr_cases"]) == 20


def _synthetic_predictions() -> dict:
    """Independent recorded mock data: no oracle import or ground-truth copying."""
    return load_simulated_predictions("pep-505")


def test_runner_scores_supplied_synthetic_predictions_and_writes_json(tmp_path) -> None:
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps(_synthetic_predictions()), encoding="utf-8")
    destination = tmp_path / "scorecard.json"
    result = run_benchmark("pep-505", predictions_path=predictions, output_path=destination)
    assert result.dataset_kind == "SYNTHETIC_REPLAY_NOT_HISTORICAL"
    assert result.verdict == "PASSED"
    assert all(m.status == "PASSED" for m in result.metrics)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["verdict"] == "PASSED"
    assert payload["memory_profile"]["measurement_status"] == "NOT_MEASURED"


def test_runner_without_model_predictions_refuses_to_invent_scores(tmp_path) -> None:
    result = run_benchmark("pep-505", output_path=tmp_path / "scorecard.json")
    assert result.verdict == "NOT_MEASURED"
    assert all(m.value is None for m in result.metrics)


def test_predictor_not_given_oracle_in_benchmark_call() -> None:
    observed = {}

    def extractor(data: dict) -> dict:
        observed.update(data)
        return _synthetic_predictions()

    result = run_benchmark("pep-505", predictor=extractor)
    assert result.verdict == "PASSED"
    assert "proposals" not in observed
    assert "ground_truth" not in observed
    assert "pr_labels" not in observed


def test_invalid_benchmark_name_blocks_traversal() -> None:
    from biscuit.evaluate.fixtures import load_thread

    with pytest.raises(ValueError, match="Invalid benchmark"):
        load_thread("../private", DEFAULT_FIXTURE_ROOT)


def test_issue_21_thresholds_and_audit_rows() -> None:
    from biscuit.evaluate.metrics import pr_warning_metrics

    labels = load_pr_labels()
    ids = [name for name, label in labels.items() if label == "violation"]
    scored = pr_warning_metrics(labels, ids)
    assert len(scored["audit_rows"]) == 20
    assert all(row["correct"] for row in scored["audit_rows"])
    assert sum(row["action"] == "ABSTAIN" for row in scored["audit_rows"]) == 15
    outcome = run_benchmark("pep-505")
    targets = {metric.name: metric.target for metric in outcome.metrics}
    assert targets["Lifecycle state accuracy"] == 0.85
    assert targets["AST grounding precision"] == 0.80
    assert targets["PR warning precision"] == 0.95


def test_audit_database_stores_20_case_abstentions_idempotently(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "audit.sqlite3"
    predictions = _synthetic_predictions()
    first = run_benchmark("pep-505", predictor=lambda _: predictions, audit_db=db)
    assert first.verdict == "PASSED"
    run_benchmark("pep-505", predictor=lambda _: predictions, audit_db=db)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT ground_truth_class, action FROM pr_watchdog_audit_log"
        ).fetchall()
    assert len(rows) == 20
    assert sum(cls == "benign" and action == "ABSTAIN" for cls, action in rows) == 10
    assert sum(cls == "ambiguous" and action == "ABSTAIN" for cls, action in rows) == 5
    modified = {
        **predictions,
        "warned_pr_ids": [*predictions["warned_pr_ids"], "pr-06"],
        "warning_records": [
            *predictions["warning_records"],
            {"case_id": "pr-06", "alternative_id": "pep-505:S1",
             "authorization": "SIMULATED_VERIFIED_RULING"},
        ],
    }
    run_benchmark("pep-505", predictor=lambda _: modified, audit_db=db)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pr_watchdog_audit_log"
        ).fetchone()[0] == 40


def test_heldout_warning_wrong_rejected_alternative_is_false_positive() -> None:
    labels = load_pr_labels()
    expected = load_pr_expected_alternatives()
    good = load_simulated_predictions("pep-505")["warning_records"]
    bad = [*good]
    bad[0] = {**bad[0], "alternative_id": "pep-572:P4"}
    score = pr_warning_metrics(
        labels,
        [entry["case_id"] for entry in bad],
        warning_records=bad, expected_alternatives=expected,
        expected_rulings=load_pr_authorization_oracle(),
    )
    assert score["tp"] == 4 and score["fp"] == 1 and score["fn"] == 1
    assert score["precision"] == pytest.approx(4 / 5)
    assert score["audit_rows"][0]["correct"] is False


def test_alternative_disambiguation_requires_nonoverlapping_symbols() -> None:
    different = [
        {"proposal_id": "P1", "file_path": "file.py", "ast_node_type": "FunctionDef"},
        {"proposal_id": "P2", "file_path": "file.py", "ast_node_type": "ClassDef"},
    ]
    assert alternative_disambiguation_rate(different, ["P1", "P2"])["rate"] == 1.0
    collided = [dict(different[0]), dict(different[0], proposal_id="P2")]
    assert alternative_disambiguation_rate(collided, ["P1", "P2"])["rate"] == 0.0


def test_deterministic_text_embedding_is_local_and_rationale_distinguishing() -> None:
    same = hashed_text_embedding("Unsafe grammar collision")
    assert len(same) == 256
    assert same == hashed_text_embedding("Unsafe grammar collision")
    assert cosine_similarity(same, hashed_text_embedding("grammar collision unsafe")) == pytest.approx(1.0)
    assert cosine_similarity(same, hashed_text_embedding("different unrelated concepts")) < 0.82


@pytest.mark.parametrize("pep", ["pep-505", "pep-572", "pep-557"])
def test_three_synthetic_fixture_manifests_and_replay_are_valid(pep) -> None:
    source = load_thread(pep)
    manifest = verify_manifest(pep, source)
    assert manifest["thread_sha256"] == source["thread_sha256"]
    assert manifest["reviewer"] == "PENDING_INDEPENDENT_REVIEW"
    assert manifest["historical_data"] is False
    oracle = load_ground_truth(pep)
    for proposal in oracle["proposals"]:
        assert proposal["name"] and proposal["rationale_text"]
        if proposal["ground_truth_status"] == "REJECTED":
            ruling = proposal["authoritative_ruling"]
            text = next(
                m["raw_text"] for m in source["messages"]
                if m["message_id"] == ruling["source_message_id"]
            )
            assert text[ruling["start_char"]:ruling["end_char"]] == ruling["exact_quote"]
            assert hashlib.sha256(ruling["exact_quote"].encode()).hexdigest() == (
                ruling["content_sha256"]
            )
    evaluated = run_benchmark(pep, recorded_replay=True)
    assert evaluated.prediction_source == "RECORDED_SYNTHETIC_REPLAY_NOT_MODEL"
    assert evaluated.verdict == "PASSED"
    assert evaluated.counts["rationale"]["embedding_method"].endswith("NOT_SEMANTIC")


def test_wrong_ruling_source_or_digest_never_gets_warning_credit() -> None:
    data = load_simulated_predictions("pep-505")
    expected = load_pr_expected_alternatives()
    proofs = load_pr_authorization_oracle()
    corrupted = [*data["warning_records"]]
    corrupted[0] = {**corrupted[0], "ruling_quote_sha256": "0" * 64}
    scored = pr_warning_metrics(
        load_pr_labels(), data["warned_pr_ids"], warning_records=corrupted,
        expected_alternatives=expected, expected_rulings=proofs,
    )
    assert scored["tp"] == 4
    assert scored["fp"] == 1
    assert scored["fn"] == 1


def test_model_local_proposal_ids_are_matched_by_name() -> None:
    data = load_simulated_predictions("pep-505")
    for index, proposal in enumerate(data["proposals"]):
        old = proposal["proposal_id"]
        new = f"extract-{index}"
        proposal["proposal_id"] = new
        for anchor in proposal["code_anchors"]:
            assert anchor["proposal_id"] == old
            anchor["proposal_id"] = new
    evaluated = run_benchmark("pep-505", predictor=lambda _: data)
    assert evaluated.verdict == "PASSED"
    assert evaluated.counts["proposals"]["proposal_id_mapping"] == {
        "extract-0": "S1", "extract-1": "S2",
    }
