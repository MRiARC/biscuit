"""Offline scorer: prediction input is isolated from the withheld scoring oracle."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .fixtures import (
    DEFAULT_FIXTURE_ROOT,
    load_ground_truth,
    load_pr_labels,
    load_thread,
    predictor_input,
)
from .metrics import (
    ast_grounding_precision,
    causal_rationale_alignment,
    exact_evidence_match,
    lifecycle_accuracy,
    pr_warning_metrics,
    proposal_recovery,
)
from .types import BenchmarkOutcome, MetricScore


def _metric(
    name: str, value: float | None, target: float, count: int, direction: str = "min"
) -> MetricScore:
    return MetricScore(name=name, value=value, target=target, sample_size=count, direction=direction)


def run_benchmark(
    benchmark_id: str = "pep-505",
    *,
    predictions_path: Path | None = None,
    predictor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    fixtures_root: Path | None = None,
    output_path: Path | None = None,
) -> BenchmarkOutcome:
    """Score supplied predictions without passing labels or official PEP text to extraction.

    The included PEP-505 fixture is explicitly SYNTHETIC and is NOT the historic
    234-post dataset. Without predictions no scores are invented.
    """
    if predictions_path is not None and predictor is not None:
        raise ValueError("Supply predictions_path OR predictor, not both")
    root = fixtures_root or DEFAULT_FIXTURE_ROOT
    thread = load_thread(benchmark_id, root)
    safe_input = predictor_input(thread, root)

    predictions: dict[str, Any] | None = None
    source = "NOT_SUPPLIED"
    if predictor is not None:
        predictions = predictor(safe_input)
        source = "PREDICTOR"
    elif predictions_path is not None:
        predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
        source = "SUPPLIED_JSON"
    if predictions is not None and not isinstance(predictions, dict):
        raise ValueError("Predictions must be a JSON object")

    # The scoring-only oracle is deliberately loaded AFTER predictor invocation.
    # It must never be passed to the predictor, prompt builder, or crawler.
    truth = load_ground_truth(benchmark_id, root)
    labels = load_pr_labels(root)
    if len(labels) != 20 or sorted(labels.values()).count("violation") != 5:
        raise ValueError("The held-out PR set must contain exactly 5 violations")
    if list(labels.values()).count("benign") != 10 or list(labels.values()).count("ambiguous") != 5:
        raise ValueError("The held-out PR set requires 10 benign and 5 ambiguous cases")
    if {case["case_id"] for case in safe_input["pr_cases"]} != set(labels):
        raise ValueError("PR cases and evaluation-only labels differ")

    gt_proposals = truth["proposals"]
    truth_states = {p["proposal_id"]: p["ground_truth_status"] for p in gt_proposals}
    truth_anchors = [anchor for p in gt_proposals for anchor in p.get("code_anchors", [])]
    truth_vectors = {
        p["proposal_id"]: p["rationale_embedding"]
        for p in gt_proposals
        if "rationale_embedding" in p
    }
    raw = {msg["message_id"]: msg["raw_text"] for msg in thread["messages"]}

    if predictions is None:
        metrics = (
            _metric("Proposal recovery F1", None, 0.85, 0),
            _metric("Lifecycle state accuracy", None, 0.80, 0),
            _metric("False rejection rate", None, 0.0, 0, "max"),
            _metric("Evidence exact match rate", None, 1.0, 0),
            _metric("Invalid citation rate", None, 0.0, 0, "max"),
            _metric("AST grounding precision", None, 0.75, 0),
            _metric("Causal rationale alignment", None, 0.82, 0),
            _metric("PR warning precision", None, 0.90, 0),
            _metric("Benign false alarm rate", None, 0.0, 0, "max"),
            _metric("Ambiguous abstention rate", None, 1.0, 0),
        )
        counts: dict[str, Any] = {"raw_messages": len(raw), "held_out_prs": len(labels)}
    else:
        recovered = predictions.get("proposals", [])
        if not isinstance(recovered, list):
            raise ValueError("Predicted proposals must be a list")
        recovered_ids = [p["proposal_id"] for p in recovered]
        if len(recovered_ids) != len(set(recovered_ids)):
            raise ValueError("Duplicate predicted proposal IDs")
        recovered_states = {
            p["proposal_id"]: p["status"] for p in recovered if "status" in p
        }
        found_anchors = [
            anchor for p in recovered for anchor in p.get("code_anchors", [])
        ]
        found_vectors = {
            p["proposal_id"]: p["rationale_embedding"]
            for p in recovered
            if "rationale_embedding" in p
        }
        recovery = proposal_recovery(recovered_ids, list(truth_states))
        state = lifecycle_accuracy(recovered_states, truth_states)
        evidence = exact_evidence_match(predictions.get("evidence", []), raw)
        anchors = ast_grounding_precision(found_anchors, truth_anchors)
        rationale = causal_rationale_alignment(found_vectors, truth_vectors)
        watchdog = pr_warning_metrics(labels, predictions.get("warned_pr_ids", []))
        metrics = (
            _metric("Proposal recovery F1", recovery["f1"], 0.85, len(truth_states)),
            _metric("Lifecycle state accuracy", state["accuracy"], 0.80, state["total"]),
            _metric("False rejection rate", state["false_rejection_rate"], 0.0, state["non_rejected"], "max"),
            _metric("Evidence exact match rate", evidence["emr"], 1.0, evidence["total"]),
            _metric("Invalid citation rate", evidence["invalid_citation_rate"], 0.0, evidence["total"], "max"),
            _metric("AST grounding precision", anchors["precision"], 0.75, anchors["total"]),
            _metric("Causal rationale alignment", rationale["cra"], 0.82, rationale["evaluated"]),
            _metric("PR warning precision", watchdog["precision"], 0.90, watchdog["total"]),
            _metric("Benign false alarm rate", watchdog["benign_false_alarm_rate"], 0.0, watchdog["benign_total"], "max"),
            _metric("Ambiguous abstention rate", watchdog["ambiguous_abstention_rate"], 1.0, watchdog["ambiguous_total"]),
        )
        counts = {
            "raw_messages": len(raw),
            "held_out_prs": len(labels),
            "proposals": recovery,
            "states": state,
            "evidence": evidence,
            "anchors": anchors,
            "rationale": rationale,
            "watchdog": watchdog,
        }
    outcome = BenchmarkOutcome(
        benchmark_id=benchmark_id,
        dataset_kind=truth.get("dataset_kind", "UNKNOWN"),
        prediction_source=source,
        thread_sha256=thread["thread_sha256"],
        metrics=metrics,
        counts=counts,
        memory_profile={
            "measurement_status": "NOT_MEASURED",
            "peak_host_used_mib": None,
            "postgres_rss_mib": None,
            "model_rss_mib": None,
            "kv_cache_mib": None,
        },
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return outcome
