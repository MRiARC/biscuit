"""Deterministic, fail-closed quantitative metrics; no model or network required."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def proposal_recovery(extracted: Sequence[str], expected: Sequence[str]) -> dict[str, Any]:
    """Exact ID-based set recovery: guessed alternatives lower precision."""
    found = set(extracted)
    truth = set(expected)
    matched = len(found & truth)
    precision = matched / len(found) if found else (0.0 if truth else None)
    recall = matched / len(truth) if truth else None
    if precision is None or recall is None:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": matched,
        "fp": len(found - truth),
        "fn": len(truth - found),
    }


def lifecycle_accuracy(
    predicted: Mapping[str, str], expected: Mapping[str, str]
) -> dict[str, Any]:
    """Unrecovered proposals count as incorrect, not as abstained successes."""
    correct = sum(predicted.get(key) == state for key, state in expected.items())
    non_rejected = [key for key, state in expected.items() if state != "REJECTED"]
    false_rejections = sum(predicted.get(key) == "REJECTED" for key in non_rejected)
    states = sorted(set(expected.values()) | set(predicted.values()) | {"MISSING"})
    confusion: dict[str, dict[str, int]] = {
        actual: {guess: 0 for guess in states} for actual in states if actual != "MISSING"
    }
    for key, actual in expected.items():
        guess = predicted.get(key, "MISSING")
        confusion[actual][guess] += 1
    return {
        "accuracy": _ratio(correct, len(expected)),
        "correct": correct,
        "total": len(expected),
        "false_rejections": false_rejections,
        "false_rejection_rate": _ratio(false_rejections, len(non_rejected)),
        "non_rejected": len(non_rejected),
        "confusion_matrix": confusion,
    }


def exact_evidence_match(
    spans: Sequence[Mapping[str, Any]], messages: Mapping[str, str]
) -> dict[str, Any]:
    """Both exact Python character offsets and UTF-8 quote digest must match."""
    valid = 0
    results: list[dict[str, Any]] = []
    for i, span in enumerate(spans):
        raw = messages.get(str(span.get("message_id", "")))
        start, end = span.get("start_char"), span.get("end_char")
        quote, supplied_hash = span.get("quote"), span.get("sha256")
        offsets_ok = (
            raw is not None
            and isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and 0 <= start < end <= len(raw)
        )
        exact = offsets_ok and isinstance(quote, str) and raw[start:end] == quote
        digest_ok = (
            isinstance(quote, str)
            and isinstance(supplied_hash, str)
            and hashlib.sha256(quote.encode("utf-8")).hexdigest() == supplied_hash
        )
        verified = bool(exact and digest_ok)
        valid += verified
        results.append({"index": i, "message_id": span.get("message_id"), "verified": verified})
    return {
        "emr": _ratio(valid, len(spans)),
        "invalid_citation_rate": _ratio(len(spans) - valid, len(spans)),
        "verified": valid,
        "total": len(spans),
        "checks": results,
    }


def _anchor_key(anchor: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(anchor.get("proposal_id", "")),
        str(anchor.get("file_path", "")),
        str(anchor.get("symbol_identifier", "")),
        str(anchor.get("ast_node_type", "")),
        str(anchor.get("historical_commit", "")),
    )


def ast_grounding_precision(
    predicted: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Alternative, historical commit, file AND symbol/AST identity must agree."""
    truth = {_anchor_key(anchor) for anchor in expected}
    matches = sum(_anchor_key(anchor) in truth for anchor in predicted)
    return {
        "precision": _ratio(matches, len(predicted)),
        "matched": matches,
        "total": len(predicted),
    }


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("Embeddings must have equal nonzero dimensions")
    if not all(math.isfinite(float(x)) for x in [*left, *right]):
        raise ValueError("Embeddings must be finite")
    left_norm = math.sqrt(sum(float(v) ** 2 for v in left))
    right_norm = math.sqrt(sum(float(v) ** 2 for v in right))
    if not left_norm or not right_norm:
        raise ValueError("Zero-length embedding vectors cannot be compared")
    result = sum(float(a) * float(b) for a, b in zip(left, right)) / (
        left_norm * right_norm
    )
    return max(-1.0, min(1.0, result))


def causal_rationale_alignment(
    predicted: Mapping[str, Sequence[float]],
    expected: Mapping[str, Sequence[float]],
) -> dict[str, Any]:
    """Compute cosine locally from provided embeddings; never invent model vectors."""
    similarities: dict[str, float] = {}
    for proposal_id, truth in expected.items():
        if proposal_id not in predicted:
            continue
        similarities[proposal_id] = cosine_similarity(predicted[proposal_id], truth)
    return {
        "cra": sum(similarities.values()) / len(similarities) if similarities else None,
        "evaluated": len(similarities),
        "expected": len(expected),
        "per_proposal": similarities,
    }


def pr_warning_metrics(
    labels: Mapping[str, str], warned_ids: Sequence[str]
) -> dict[str, Any]:
    """Labels are an evaluation-only input and must never reach the predictor."""
    if set(labels.values()) - {"violation", "benign", "ambiguous"}:
        raise ValueError("Unknown PR fixture class")
    if len(set(warned_ids)) != len(warned_ids):
        raise ValueError("Duplicate warning IDs")
    warned = set(warned_ids)
    if warned - set(labels):
        raise ValueError("Warnings reference unknown PR fixtures")
    violations = {k for k, v in labels.items() if v == "violation"}
    benign = {k for k, v in labels.items() if v == "benign"}
    ambiguous = {k for k, v in labels.items() if v == "ambiguous"}
    tp = len(warned & violations)
    fp = len(warned - violations)
    fn = len(violations - warned)
    audit_rows = [
        {
            "case_id": case_id,
            "ground_truth_class": label,
            "action": "WARN" if case_id in warned else "ABSTAIN",
            "correct": (case_id in warned) == (label == "violation"),
        }
        for case_id, label in sorted(labels.items())
    ]
    return {
        "precision": tp / (tp + fp) if tp + fp else (0.0 if violations else None),
        "recall": _ratio(tp, len(violations)),
        "benign_false_alarm_rate": _ratio(len(warned & benign), len(benign)),
        "ambiguous_abstention_rate": _ratio(len(ambiguous - warned), len(ambiguous)),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "benign_total": len(benign),
        "ambiguous_total": len(ambiguous),
        "total": len(labels),
        "audit_rows": audit_rows,
    }
