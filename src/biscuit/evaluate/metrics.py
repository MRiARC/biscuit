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



def alternative_disambiguation_rate(
    anchors: Sequence[Mapping[str, Any]],
    proposal_ids: Sequence[str],
) -> dict[str, Any]:
    """Fraction of alternatives with nonempty, distinct file/AST/symbol anchors."""
    identifiers = list(dict.fromkeys(proposal_ids))
    locations: dict[str, set[tuple[str, str, str]]] = {
        pid: set() for pid in identifiers
    }
    for anchor in anchors:
        pid = str(anchor.get("proposal_id", ""))
        if pid not in locations:
            continue
        location = (
            str(anchor.get("file_path", anchor.get("file_pattern", ""))),
            str(anchor.get("symbol_identifier", "")),
            str(anchor.get("ast_node_type", "")),
        )
        if location[0] and (location[1] or location[2]):
            locations[pid].add(location)
    distinguishable = sum(
        bool(location_set)
        and all(
            location_set.isdisjoint(other_set)
            for other_id, other_set in locations.items() if other_id != pid
        )
        for pid, location_set in locations.items()
    )
    return {
        "rate": _ratio(distinguishable, len(identifiers)),
        "distinguishable": distinguishable,
        "total": len(identifiers),
    }


def hashed_text_embedding(text: str, dimensions: int = 256) -> list[float]:
    """Offline deterministic lexical proxy, NOT a semantic-embedding accuracy claim."""
    import re

    if dimensions <= 0:
        raise ValueError("Embedding dimension must be positive")
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    vector = [0.0] * dimensions
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0
    return vector


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
    labels: Mapping[str, str],
    warned_ids: Sequence[str],
    *,
    warning_records: Sequence[Mapping[str, Any]] | None = None,
    expected_alternatives: Mapping[str, str] | None = None,
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
    supplied_records: dict[str, Mapping[str, Any]] = {}
    if warning_records is not None:
        for record in warning_records:
            if not isinstance(record, Mapping):
                raise ValueError("Warning audit record must be an object")
            case_id = str(record.get("case_id", ""))
            if case_id not in labels or case_id in supplied_records:
                raise ValueError("Unknown or duplicate warning audit case ID")
            supplied_records[case_id] = record
        if set(supplied_records) != warned:
            raise ValueError("Warning records must correspond exactly to warned PR IDs")

    correct_warnings: set[str] = set()
    for case_id in warned & violations:
        record = supplied_records.get(case_id)
        if warning_records is None:
            # Legacy ID-list scoring is retained solely for standalone equation tests.
            # Benchmark runs must provide warning_records for authorization credit.
            correct_warnings.add(case_id)
        elif (
            expected_alternatives is not None
            and record is not None
            and record.get("alternative_id") == expected_alternatives.get(case_id)
            and record.get("authorization") == "SIMULATED_VERIFIED_RULING"
        ):
            # This records a synthetic replay contract, not production ruling authority.
            correct_warnings.add(case_id)

    tp = len(correct_warnings)
    fp = len(warned - correct_warnings)
    fn = len(violations - correct_warnings)
    audit_rows = [
        {
            "case_id": case_id,
            "ground_truth_class": label,
            "action": "WARN" if case_id in warned else "ABSTAIN",
            "alternative_id": supplied_records.get(case_id, {}).get("alternative_id"),
            "authorization": supplied_records.get(case_id, {}).get("authorization"),
            "correct": (
                case_id in correct_warnings if label == "violation"
                else case_id not in warned
            ),
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
