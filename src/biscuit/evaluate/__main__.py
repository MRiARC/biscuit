"""No-network, standard-library benchmark CLI: python -m biscuit.evaluate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .aggregate import aggregate_outcomes
from .fixtures import DEFAULT_FIXTURE_ROOT
from .runner import run_benchmark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Biscuit benchmark evaluator")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--benchmark", default="pep-505", help="Fixture identifier")
    group.add_argument("--all", action="store_true", help="Evaluate every installed fixture")
    parser.add_argument("--fixtures-root", type=Path, default=DEFAULT_FIXTURE_ROOT)
    parser.add_argument("--predictions", type=Path, help="JSON prediction file (directory with --all)")
    parser.add_argument("--output", default="scorecard.json", help="JSON output path or 'json'")
    parser.add_argument("--strict-offsets", action="store_true")
    parser.add_argument("--audit-db", type=Path, help="SQLite per-PR audit database")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-simulate", action="store_true", help="Return NOT_MEASURED unless predictions supplied")
    parser.add_argument("--stream3", action="store_true", help="Run installed Stream 3 extraction (NOT mock replay)")
    parser.add_argument("--semantic-embeddings", action="store_true", help="Score predicted rationale with loopback Ollama and precomputed GT vectors")
    parser.add_argument("--profile-memory", action="store_true", help="Sample host memory during actual Stream 3 extraction only")
    args = parser.parse_args(argv)

    if args.stream3 and args.predictions is not None:
        parser.error("--stream3 cannot be combined with --predictions")
    if args.all:
        names = sorted(
            p.name
            for p in args.fixtures_root.iterdir()
            if p.is_dir() and (p / "raw_thread.json").is_file()
        )
        if not names:
            parser.error("No benchmark fixture directories found")
        if args.predictions is not None and not args.predictions.is_dir():
            parser.error("--all requires --predictions to be a directory of ID.json files")
    else:
        names = [args.benchmark]
    predictor = None
    if args.stream3:
        try:
            from biscuit.extract.pipeline import ExtractionPipeline

            from .stream3_adapter import stream3_predictor

            predictor = stream3_predictor(ExtractionPipeline())
        except ImportError as exc:
            parser.exit(1, "Stream 3 extraction is not installed; integrate PR #23: "
                        + str(exc) + "\\n")
    embedder = None
    if args.semantic_embeddings:
        from .embeddings import LocalOllamaEmbedder

        embedder = LocalOllamaEmbedder()
    outputs = []
    outcomes = []
    try:
        for name in names:
            prediction_file = (
                args.predictions / (name + ".json")
                if args.all and args.predictions is not None
                else args.predictions
            )
            result = run_benchmark(
                name, fixtures_root=args.fixtures_root, predictions_path=prediction_file,
                predictor=predictor, semantic_embedder=embedder,
                profile_host_memory=args.profile_memory and predictor is not None,
                audit_db=args.audit_db or Path.home() / ".local/share/biscuit/workspace.sqlite3",
                recorded_replay=(args.predictions is None and not args.no_simulate
                                 and not args.stream3),
            )
            outputs.append(result.to_dict())
            outcomes.append(result)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"Benchmark failed: {exc}\n")

    if args.strict_offsets:
        for result in outputs:
            checked = next(
                metric for metric in result["metrics"]
                if metric["name"] == "Evidence exact match rate"
            )
            if checked["status"] != "PASSED":
                parser.exit(1, "Strict offsets require verified evidence spans\n")

    payload = outputs[0] if len(outputs) == 1 else {
        "schema_version": 1, "benchmarks": outputs,
        "aggregate": aggregate_outcomes(outcomes),
        "verdict": (
            "FAILED" if any(x["verdict"] == "FAILED" for x in outputs)
            else "INCOMPLETE" if any(
                x["verdict"] != "PASSED" for x in outputs
            ) else "PASSED"
        ),
    }
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output != "json":
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
    if args.output == "json" or args.verbose:
        print(rendered)
    else:
        print("Benchmark scorecard:", args.output)
        for output in outputs:
            print(output["benchmark_id"], output["dataset_kind"], output["verdict"])
    if payload["verdict"] == "PASSED":
        return 0
    if payload["verdict"] == "FAILED":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
