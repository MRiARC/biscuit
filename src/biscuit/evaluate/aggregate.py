"""Cross-benchmark score aggregation without duplicating shared PR case counts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .types import BenchmarkOutcome, MetricScore


def aggregate_outcomes(outcomes: Sequence[BenchmarkOutcome]) -> dict[str, Any]:
    if not outcomes:
        raise ValueError("At least one benchmark outcome is required")
    grouped: dict[str, list[MetricScore]] = {}
    for outcome in outcomes:
        for metric in outcome.metrics:
            grouped.setdefault(metric.name, []).append(metric)
    rows: list[dict[str, Any]] = []
    for name, scores in grouped.items():
        measured = [m for m in scores if m.value is not None and m.sample_size > 0]
        shared_pr_suite = name.startswith(("PR warning", "Benign false", "Ambiguous abstention"))
        if measured:
            # Each benchmark in this harness tests the same 20 PR cases.
            # Report a macro across runs, but never sum a 20-case suite N times.
            if shared_pr_suite:
                value = sum(float(m.value) for m in measured) / len(measured)
                count = max(m.sample_size for m in measured)
                method = "SHARED_20_PR_SUITE_MACRO"
            else:
                count = sum(m.sample_size for m in measured)
                value = sum(float(m.value) * m.sample_size for m in measured) / count
                method = "WEIGHTED_BY_SAMPLE_SIZE"
        else:
            value = None
            count = 0
            method = "NOT_MEASURED"
        metric = MetricScore(
            name=name,
            value=value,
            target=scores[0].target,
            direction=scores[0].direction,
            sample_size=count,
        )
        rows.append({**metric.to_dict(), "aggregation_method": method})
    verdict = (
        "FAILED" if any(x.verdict == "FAILED" for x in outcomes)
        else "INCOMPLETE" if any(x.verdict != "PASSED" for x in outcomes)
        else "PASSED"
    )
    return {
        "schema_version": 1,
        "benchmark_count": len(outcomes),
        "benchmarks": [x.benchmark_id for x in outcomes],
        "datasets": [x.dataset_kind for x in outcomes],
        "historical_accuracy_measured": False,
        "shared_held_out_pr_case_count": max(
            (int(x.counts.get("held_out_prs", 0)) for x in outcomes), default=0
        ),
        "metrics": rows,
        "verdict": verdict,
    }
