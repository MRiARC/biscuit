"""Rich scorecard with a readable text fallback when optional Rich is absent."""

from __future__ import annotations

from typing import TextIO

from biscuit.evaluate.types import BenchmarkOutcome


def render_scorecard(outcome: BenchmarkOutcome, *, file: TextIO | None = None) -> str:
    """Render and return an auditable table. No claims of measured RAM without data."""
    heading = (
        f"Biscuit evaluation: {outcome.benchmark_id} | "
        f"{outcome.dataset_kind} | {outcome.prediction_source}"
    )
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        lines = [heading, "Metric | Target | Result | Status"]
        for score in outcome.metrics:
            value = f"{score.value:.3f}" if score.value is not None else "N/A"
            operator = "<=" if score.direction == "max" else ">="
            lines.append(f"{score.name} | {operator}{score.target:.2f} | {value} | {score.status}")
        lines.append("Memory profile: NOT_MEASURED (no host RSS profiling)")
        lines.append(f"Verdict: {outcome.verdict}")
        result = "\n".join(lines)
        if file is not None:
            file.write(result + "\n")
        return result

    console = Console(file=file, force_terminal=False, width=110, color_system=None, record=True)
    table = Table(title=heading)
    for label in ("Metric", "Target", "Result", "Status"):
        table.add_column(label)
    for score in outcome.metrics:
        operator = "<=" if score.direction == "max" else ">="
        table.add_row(
            score.name,
            f"{operator} {score.target:.2%}",
            f"{score.value:.2%}" if score.value is not None else "N/A",
            score.status,
        )
    console.print(table)
    memory = outcome.memory_profile
    console.print(
        "Memory profile: " + str(memory["measurement_status"])
        + " (peak host RAM / database / model / KV cache not measured)"
    )
    console.print("Verdict: " + outcome.verdict)
    return console.export_text()
