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
        predictions: Path | None = typer.Option(None, "--predictions"),  # noqa: B008
        output: Path = typer.Option(Path("scorecard.json"), "--output"),  # noqa: B008
    ) -> None:
        """Score predictions against withheld fixture labels (no live models needed)."""
        try:
            outcome = run_benchmark(
                benchmark_id=benchmark, predictions_path=predictions, output_path=output
            )
        except (OSError, ValueError, KeyError) as exc:
            typer.echo(f"Evaluation failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        render_scorecard(outcome)
        typer.echo(f"Saved: {output}")
        if outcome.verdict in {"NOT_MEASURED", "INCOMPLETE"}:
            raise typer.Exit(2)
        if outcome.verdict == "FAILED":
            raise typer.Exit(1)

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
        except (OSError, ValueError, KeyError) as exc:
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
