"""Stable types shared by the offline evaluator and the terminal renderer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class MetricScore:
    name: str
    value: float | None
    target: float
    direction: str = "min"
    sample_size: int = 0

    @property
    def status(self) -> str:
        if self.value is None or self.sample_size == 0:
            return "NOT_MEASURED"
        if self.direction == "max":
            return "PASSED" if self.value <= self.target else "FAILED"
        return "PASSED" if self.value >= self.target else "FAILED"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "status": self.status}


@dataclass(frozen=True)
class BenchmarkOutcome:
    benchmark_id: str
    dataset_kind: str
    prediction_source: str
    thread_sha256: str
    metrics: tuple[MetricScore, ...]
    counts: dict[str, Any]
    memory_profile: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "benchmark_id": self.benchmark_id,
            "dataset_kind": self.dataset_kind,
            "prediction_source": self.prediction_source,
            "thread_sha256": self.thread_sha256,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "counts": self.counts,
            "memory_profile": self.memory_profile,
            "verdict": self.verdict,
        }

    @property
    def verdict(self) -> str:
        if self.prediction_source == "NOT_SUPPLIED":
            return "NOT_MEASURED"
        if any(metric.status == "FAILED" for metric in self.metrics):
            return "FAILED"
        if any(metric.status == "NOT_MEASURED" for metric in self.metrics):
            return "INCOMPLETE"
        return "PASSED"
