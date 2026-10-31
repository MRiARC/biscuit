"""Offline scorer: prediction input is isolated from the withheld scoring oracle."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .audit import write_pr_audit
from .fixtures import (
    DEFAULT_FIXTURE_ROOT,
    load_ground_truth,
    load_pr_authorization_oracle,
    load_pr_expected_alternatives,
    load_pr_labels,
    load_simulated_predictions,
    load_thread,
    predictor_input,
)
from .metrics import (
    alternative_disambiguation_rate,
    ast_grounding_precision,
    causal_rationale_alignment,
    exact_evidence_match,
    hashed_text_embedding,
    lifecycle_accuracy,
    pr_warning_metrics,
    proposal_recovery,
)
from .types import BenchmarkOutcome, MetricScore


def _metric(
    name: str, value: float | None, target: float, count: int, direction: str = "min"
) -> MetricScore:
    return MetricScore(name=name, value=value, target=target, sample_size=count, direction=direction)



def _normalize_proposal_name(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


def _align_proposals(
    recovered: list[dict[str, Any]], truth: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Match model-assigned IDs by unique, exact normalized proposal name/alias."""
    expected_ids = {p["proposal_id"] for p in truth}
    name_index: dict[str, set[str]] = {}
    for proposal in truth:
        for alias in [proposal.get("name", ""), *proposal.get("aliases", [])]:
            normalized = _normalize_proposal_name(alias)
            if normalized:
                name_index.setdefault(normalized, set()).add(proposal["proposal_id"])
    aligned: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for proposal in recovered:
        old_id = str(proposal["proposal_id"])
        if old_id in mapping:
            raise ValueError("Duplicate predicted proposal ID: " + old_id)
        if old_id in expected_ids:
            matched = old_id
        else:
            candidates = name_index.get(
                _normalize_proposal_name(str(proposal.get("name", ""))), set()
            )
            matched = next(iter(candidates)) if len(candidates) == 1 else f"UNMATCHED:{old_id}"
        if matched in used:
            raise ValueError("Multiple predicted alternatives mapped to the same ground truth")
        mapping[old_id] = matched
        used.add(matched)
        aligned.append({**proposal, "proposal_id": matched})
    return aligned, mapping


def run_benchmark(
    benchmark_id: str = "pep-505",
    *,
    predictions_path: Path | None = None,
    predictor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    fixtures_root: Path | None = None,
    output_path: Path | None = None,
    audit_db: Path | None = None,
    recorded_replay: bool = False,
    semantic_embedder: Callable[[str], Sequence[float]] | None = None,
) -> BenchmarkOutcome:
    """Score supplied predictions without passing labels or official PEP text to extraction.

    The included PEP-505 fixture is explicitly SYNTHETIC and is NOT the historic
    234-post dataset. Without predictions no scores are invented.
    """
    if sum([predictions_path is not None, predictor is not None, recorded_replay]) > 1:
        raise ValueError("Supply only one of predictions_path, predictor, or recorded_replay")
    root = fixtures_root or DEFAULT_FIXTURE_ROOT
    thread = load_thread(benchmark_id, root)
    safe_input = predictor_input(thread, root)

    predictions: dict[str, Any] | None = None
    source = "NOT_SUPPLIED"
    if recorded_replay:
        predictions = load_simulated_predictions(benchmark_id, root)
        source = "RECORDED_SYNTHETIC_REPLAY_NOT_MODEL"
    elif predictor is not None:
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
    expected_warning_alternatives = load_pr_expected_alternatives(root)
    expected_rulings = load_pr_authorization_oracle(root)
    if len(labels) != 20 or sorted(labels.values()).count("violation") != 5:
        raise ValueError("The held-out PR set must contain exactly 5 violations")
    if list(labels.values()).count("benign") != 10 or list(labels.values()).count("ambiguous") != 5:
        raise ValueError("The held-out PR set requires 10 benign and 5 ambiguous cases")
    if {case["case_id"] for case in safe_input["pr_cases"]} != set(labels):
        raise ValueError("PR cases and evaluation-only labels differ")

    gt_proposals = truth["proposals"]
    truth_states = {p["proposal_id"]: p["ground_truth_status"] for p in gt_proposals}
    truth_anchors = [anchor for p in gt_proposals for anchor in p.get("code_anchors", [])]
    if semantic_embedder is not None:
        # The withheld oracle's text is NEVER sent to an inference/embedding endpoint.
        # Independent, model-matched reference vectors must already be pinned in GT.
        model_id = str(getattr(semantic_embedder, "model", ""))
        truth_vectors = {
            p["proposal_id"]: p["rationale_embedding"]
            for p in gt_proposals
            if model_id and p.get("rationale_embedding_model") == model_id
            and "rationale_embedding" in p
        }
    else:
        truth_vectors = {
            p["proposal_id"]: (
                hashed_text_embedding(p["rationale_text"])
                if "rationale_text" in p else p["rationale_embedding"]
            )
            for p in gt_proposals
            if "rationale_text" in p or "rationale_embedding" in p
        }
    raw = {msg["message_id"]: msg["raw_text"] for msg in thread["messages"]}

    if predictions is None:
        metrics = (
            _metric("Proposal recovery F1", None, 0.85, 0),
            _metric("Lifecycle state accuracy", None, 0.85, 0),
            _metric("False rejection rate", None, 0.0, 0, "max"),
            _metric("Evidence exact match rate", None, 1.0, 0),
            _metric("Invalid citation rate", None, 0.0, 0, "max"),
            _metric("AST grounding precision", None, 0.80, 0),
            _metric("Alternative disambiguation rate", None, 1.0, 0),
            _metric("Causal rationale alignment", None, 0.82, 0),
            _metric("PR warning precision", None, 0.95, 0),
            _metric("PR warning recall", None, 1.0, 0),
            _metric("Benign false alarm rate", None, 0.0, 0, "max"),
            _metric("Ambiguous abstention rate", None, 1.0, 0),
        )
        counts: dict[str, Any] = {"raw_messages": len(raw), "held_out_prs": len(labels)}
    else:
        recovered = predictions.get("proposals", [])
        if not isinstance(recovered, list):
            raise ValueError("Predicted proposals must be a list")
        recovered, proposal_id_map = _align_proposals(recovered, gt_proposals)
        recovered_ids = [p["proposal_id"] for p in recovered]
        if len(recovered_ids) != len(set(recovered_ids)):
            raise ValueError("Duplicate predicted proposal IDs")
        recovered_states = {
            p["proposal_id"]: p["status"] for p in recovered if "status" in p
        }
        found_anchors = [
            {
                **anchor,
                "proposal_id": proposal_id_map.get(
                    str(anchor.get("proposal_id", "")),
                    str(anchor.get("proposal_id", "")),
                ),
            }
            for p in recovered for anchor in p.get("code_anchors", [])
        ]
        if semantic_embedder is not None:
            found_vectors = {
                p["proposal_id"]: list(semantic_embedder(p["rationale_text"]))
                for p in recovered
                if truth_vectors and "rationale_text" in p
            }
        else:
            found_vectors = {
                p["proposal_id"]: (
                    hashed_text_embedding(p["rationale_text"])
                    if "rationale_text" in p else p["rationale_embedding"]
                )
                for p in recovered
                if "rationale_text" in p or "rationale_embedding" in p
            }
        recovery = proposal_recovery(recovered_ids, list(truth_states))
        state = lifecycle_accuracy(recovered_states, truth_states)
        evidence = exact_evidence_match(predictions.get("evidence", []), raw)
        anchors = ast_grounding_precision(found_anchors, truth_anchors)
        disambiguation = alternative_disambiguation_rate(
            found_anchors, recovered_ids, expected_anchors=truth_anchors,
            expected_proposal_ids=list(truth_states),
        )
        rationale = causal_rationale_alignment(found_vectors, truth_vectors)
        # A bare list of PR IDs cannot establish the correct rejected alternative.
        # Scoring such a list alone would reward unauthenticated warnings.
        warning_records = predictions.get("warning_records", [])
        watchdog = pr_warning_metrics(
            labels,
            predictions.get("warned_pr_ids", []),
            warning_records=warning_records,
            expected_alternatives=expected_warning_alternatives,
            expected_rulings=expected_rulings,
        )
        if audit_db is not None:
            run_hash = hashlib.sha256(
                json.dumps(
                    predictions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            write_pr_audit(
                audit_db, benchmark_id=benchmark_id,
                run_sha256=run_hash, rows=watchdog["audit_rows"],
            )
        metrics = (
            _metric("Proposal recovery F1", recovery["f1"], 0.85, len(truth_states)),
            _metric("Lifecycle state accuracy", state["accuracy"], 0.85, state["total"]),
            _metric("False rejection rate", state["false_rejection_rate"], 0.0, state["non_rejected"], "max"),
            _metric("Evidence exact match rate", evidence["emr"], 1.0, evidence["total"]),
            _metric("Invalid citation rate", evidence["invalid_citation_rate"], 0.0, evidence["total"], "max"),
            _metric("AST grounding precision", anchors["precision"], 0.80, anchors["total"]),
            _metric("Alternative disambiguation rate", disambiguation["rate"], 1.0, disambiguation["total"]),
            _metric("Causal rationale alignment", rationale["cra"], 0.82, rationale["evaluated"]),
            _metric("PR warning precision", watchdog["precision"], 0.95, watchdog["total"]),
            _metric("PR warning recall", watchdog["recall"], 1.0, len(expected_warning_alternatives)),
            _metric("Benign false alarm rate", watchdog["benign_false_alarm_rate"], 0.0, watchdog["benign_total"], "max"),
            _metric("Ambiguous abstention rate", watchdog["ambiguous_abstention_rate"], 1.0, watchdog["ambiguous_total"]),
        )
        counts = {
            "raw_messages": len(raw),
            "held_out_prs": len(labels),
            "proposals": {**recovery, "proposal_id_mapping": proposal_id_map},
            "states": state,
            "evidence": evidence,
            "anchors": anchors,
            "alternative_disambiguation": disambiguation,
            "rationale": {
                **rationale, "embedding_method": (
                    "LOCAL_SEMANTIC_MODEL_PREDICTIONS_PRECOMPUTED_WITHHELD_GT"
                    if semantic_embedder is not None and truth_vectors
                    else "NOT_MEASURED_GT_MODEL_VECTORS_MISSING"
                    if semantic_embedder is not None
                    else "DETERMINISTIC_HASHED_TEXT_LEXICAL_PROXY_NOT_SEMANTIC"
                    if any("rationale_text" in p for p in gt_proposals)
                    else "EXTERNALLY_SUPPLIED_VECTORS"
                ), "embedding_model": (
                    str(getattr(semantic_embedder, "model", ""))
                    if semantic_embedder is not None else None
                )
            },
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
            "planning_estimates_gib": {
                "PostgreSQL_and_pgvector": 1.50,
                "Vector_index": 0.35,
                "Python_orchestrator": 0.25,
                "Embedding_model": 0.28,
                "Largest_local_model": 4.40,
                "KV_cache": 0.30,
                "Total_unmeasured_design_estimate": 7.08,
            },
        },
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return outcome
