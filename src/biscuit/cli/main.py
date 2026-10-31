"""Typer command tree: evaluate, ingest, decisions list/import, export."""

from __future__ import annotations

import os
from pathlib import Path

from biscuit.cli.scorecard import render_scorecard
from biscuit.cli.storage import export_madr, import_decisions, ingest_discourse, list_decisions
from biscuit.evaluate.runner import run_benchmark

try:
    import typer
except ImportError:  # Dependency-owning stream must add typer to pyproject.toml.
    typer = None


def _default_db() -> Path:
    return Path(os.environ.get("BISCUIT_DB", "~/.local/share/biscuit/workspace.sqlite3")).expanduser()


if typer is not None:
    app = typer.Typer(help="Evidence-grounded decision recovery and offline evaluation.")
    decisions_app = typer.Typer(help="List and import human-reviewed local decisions.")
    app.add_typer(decisions_app, name="decisions")

    @app.command()
    def evaluate(
        benchmark: str = typer.Option("pep-505", "--benchmark"),
        all_benchmarks: bool = typer.Option(False, "--all"),
        predictions: Path | None = typer.Option(None, "--predictions"),  # noqa: B008
        fixtures_root: Path | None = typer.Option(None, "--fixtures-root"),  # noqa: B008
        output: Path = typer.Option(Path("scorecard.json"), "--output"),  # noqa: B008
        audit_db: Path | None = typer.Option(None, "--audit-db"),  # noqa: B008
        output_format: str = typer.Option("rich", "--format"),
        strict_offsets: bool = typer.Option(False, "--strict-offsets"),
        verbose: bool = typer.Option(False, "--verbose"),
    ) -> None:
        """Evaluate offline predictions; do not expose hidden ground truth to predictors."""
        import json

        from biscuit.evaluate.fixtures import DEFAULT_FIXTURE_ROOT

        root = fixtures_root or DEFAULT_FIXTURE_ROOT
        if output_format not in {"rich", "json"}:
            typer.echo("--format must be rich or json", err=True)
            raise typer.Exit(2)
        if all_benchmarks:
            if predictions is not None and not predictions.is_dir():
                typer.echo("--all requires a prediction directory", err=True)
                raise typer.Exit(2)
            names = sorted(
                folder.name for folder in root.iterdir()
                if folder.is_dir() and (folder / "raw_thread.json").exists()
            )
        else:
            names = [benchmark]
        if not names:
            typer.echo("No benchmark fixtures found", err=True)
            raise typer.Exit(2)
        try:
            outcomes = [
                run_benchmark(
                    benchmark_id=name,
                    predictions_path=(
                        predictions / f"{name}.json"
                        if all_benchmarks and predictions is not None else predictions
                    ),
                    fixtures_root=root,
                    audit_db=audit_db,
                )
                for name in names
            ]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            typer.echo(f"Evaluation failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        payload = (
            outcomes[0].to_dict() if len(outcomes) == 1
            else {
                "schema_version": 1,
                "benchmarks": [outcome.to_dict() for outcome in outcomes],
                "verdict": (
                    "FAILED" if any(x.verdict == "FAILED" for x in outcomes)
                    else "INCOMPLETE" if any(x.verdict != "PASSED" for x in outcomes)
                    else "PASSED"
                ),
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if output_format == "json":
            typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            for outcome in outcomes:
                render_scorecard(outcome)
                if verbose and "watchdog" in outcome.counts:
                    for row in outcome.counts["watchdog"]["audit_rows"]:
                        typer.echo(
                            f"{row['case_id']}: {row['action']} "
                            f"(truth={row['ground_truth_class']}, correct={row['correct']})"
                        )
            typer.echo(f"Saved: {output}")
        if strict_offsets and any(
            metric.name == "Evidence exact match rate" and metric.status != "PASSED"
            for outcome in outcomes for metric in outcome.metrics
        ):
            typer.echo("Strict offsets: unverified or absent evidence", err=True)
            raise typer.Exit(1)
        if any(outcome.verdict == "FAILED" for outcome in outcomes):
            raise typer.Exit(1)
        if any(outcome.verdict != "PASSED" for outcome in outcomes):
            raise typer.Exit(2)

    @app.command()
    def ingest(
        platform: str = typer.Option(..., "--platform"),
        url: str = typer.Option(..., "--url"),
        db: Path | None = typer.Option(None, "--db"),  # noqa: B008
    ) -> None:
        """Ingest a complete Discourse topic snapshot into the local SQLite cache."""
        if platform.lower() != "discourse":
            typer.echo("Only Discourse ingestion is supported in this workstream", err=True)
            raise typer.Exit(2)
        try:
            count = ingest_discourse(url, db or _default_db())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            typer.echo(f"Ingestion failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Saved {count} messages. Decisions require separate verified extraction.")

    @decisions_app.command("list")
    def decisions_list(
        status: str | None = typer.Option(None, "--status"),
        db: Path | None = typer.Option(None, "--db"),  # noqa: B008
    ) -> None:
        """Display the local decision index, optionally filtered by lifecycle state."""
        try:
            rows = list_decisions(db or _default_db(), status)
        except (OSError, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        if not rows:
            typer.echo("No decisions found. Import verified records with decisions import.")
            return
        try:
            from rich.console import Console
            from rich.table import Table
        except ImportError:
            for row in rows:
                typer.echo(f"{row['decision_id']} | {row['status']} | {row['title']}")
            return
        table = Table(title="Recovered decisions (local cache)")
        for column in ("ID", "Status", "Title"):
            table.add_column(column)
        for row in rows:
            table.add_row(row["decision_id"], row["status"], row["title"])
        Console().print(table)

    @decisions_app.command("import")
    def decisions_import(
        source: Path = typer.Option(..., "--file"),  # noqa: B008
        db: Path | None = typer.Option(None, "--db"),  # noqa: B008
    ) -> None:
        """Import already verified decision records; no automatic LLM extraction."""
        try:
            count = import_decisions(source, db or _default_db())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            typer.echo(f"Import failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Imported {count} decisions")

    @app.command("export")
    def export(
        output: Path = typer.Option(Path("docs/decisions"), "--output"),  # noqa: B008
        db: Path | None = typer.Option(None, "--db"),  # noqa: B008
    ) -> None:
        """Export locally verified decision records as MADR-style markdown files."""
        try:
            count = export_madr(db or _default_db(), output)
        except (OSError, ValueError) as exc:
            typer.echo(f"Export failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Exported {count} decision documents to {output}")

else:
    app = None


def main() -> None:
    if app is None:
        raise SystemExit(
            "The Biscuit CLI requires typer>=0.12 and rich>=13.7. "
            "Install these optional CLI dependencies before running it."
        )
    app()
