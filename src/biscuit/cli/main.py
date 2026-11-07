"""Typer command tree: evaluate, ingest, decisions list/import, export."""

from __future__ import annotations

import os
from pathlib import Path

from biscuit.cli.storage import export_madr, import_decisions, list_decisions

try:
    import typer
except ImportError:  # Dependency-owning stream must add typer to pyproject.toml.
    typer = None


def _default_db() -> Path:
    return Path(
        os.environ.get("BISCUIT_DB", "~/.local/share/biscuit/workspace.sqlite3")
    ).expanduser()


def _resolve_db_url(db_url: str | None, local_db: Path | None = None) -> str | None:
    """Resolve an authoritative database URL for the CLI commands that support it.

    Priority: explicit ``--db-url`` option, then ``DATABASE_URL``, then
    ``SWITCH_DATABASE_URL``, then ``BISCUIT_TEST_PG_DSN`` (the CI alias).
    When the user explicitly passes ``--db`` (a local SQLite path), we honour
    that and return ``None`` so the local cache is used — explicit user intent
    beats env-var defaults. Returns ``None`` when no authoritative store is
    configured; the local SQLite cache is then the documented fallback for offline use.
    """
    if local_db is not None:
        return None
    return (
        db_url
        or os.getenv("DATABASE_URL")
        or os.getenv("SWITCH_DATABASE_URL")
        or os.getenv("BISCUIT_TEST_PG_DSN")
    )


def _list_decisions_from_store(db_url: str, status: str | None) -> list[dict]:
    """Read the authoritative relational store for the decisions index.

    Returns a list of dicts with ``decision_id`` / ``status`` / ``title`` keys
    matching the local-cache shape so the CLI rendering code is shared.
    """
    from biscuit.db.decision_store import DatabaseDecisionStore
    from biscuit.db.session import get_engine, get_session, init_db

    engine = get_engine(db_url)
    init_db(engine, apply_postgres_triggers=False)
    with get_session(engine) as session:
        store = DatabaseDecisionStore(session)
        records = []
        for alt in store.all_alternatives():
            if status is not None and alt.lifecycle_status.value != status:
                continue
            decision = store.get_decision(alt.decision_id)
            records.append(
                {
                    "decision_id": alt.id,
                    "status": alt.lifecycle_status.value,
                    "title": decision.title if decision else alt.proposal_text,
                    "context": decision.context_summary if decision else alt.proposal_text,
                    "rationale": alt.rejection_summary or "",
                    "consequences": "",
                    "record_json": {
                        "deciders": [],
                        "considered_options": [{"name": alt.proposal_text, "pros": [], "cons": []}],
                        "decision_drivers": [],
                        "links": [],
                    },
                }
            )
        records.sort(key=lambda r: r["decision_id"])
        return records


def _export_madr_from_store(db_url: str, output: Path) -> int:
    """Render MADR-style markdown for every decision in the authoritative store.

    Reuses :func:`biscuit.cli.storage.export_madr`'s rendering by feeding it
    records shaped like the local-cache rows, so the exported markdown is
    byte-identical between the two sources.
    """
    rows = _list_decisions_from_store(db_url, status=None)
    output.mkdir(parents=True, exist_ok=True)
    from biscuit.cli.storage import _madr_list, _option_text

    for item in rows:
        record = item.get("record_json", {})
        title = item["title"].replace("\n", " ").replace("\r", " ")
        header = [f"# {title}", "", f"* Status: {item['status']}", f"* ID: {item['decision_id']}"]
        if record.get("deciders"):
            header.append("* Deciders: " + ", ".join(record["deciders"]))
        options = record.get("considered_options", [])
        detail = []
        for option in options:
            name = option.get("name", option.get("title", "Unnamed option"))
            pro = _madr_list(option.get("pros"))
            con = _madr_list(option.get("cons"))
            detail.append(f"### {name}\n\n**Pros:**\n\n{pro}\n\n**Cons:**\n\n{con}")
        if not detail:
            detail.append("*No option-specific trade-offs supplied in the source record.*")
        document = (
            "\n".join(header)
            + "\n\n## Status\n\n"
            + item["status"]
            + "\n\n## Context and Problem Statement\n\n"
            + item["context"]
            + "\n\n## Decision Drivers\n\n"
            + _madr_list(record.get("decision_drivers"))
            + "\n\n## Considered Options\n\n"
            + _option_text(options)
            + "\n\n## Decision Outcome\n\n"
            + item["rationale"]
            + "\n\n### Consequences\n\n"
            + item.get("consequences", "")
            + "\n\n## Pros and Cons of the Options\n\n"
            + "\n\n".join(detail)
            + "\n\n## Links\n\n"
            + _madr_list(record.get("links"))
            + "\n"
        )
        (output / f"{item['decision_id']}.md").write_text(document, encoding="utf-8")
    return len(rows)


if typer is not None:
    app = typer.Typer(help="Live decision intelligence compass and PR guardrail for Switch.")
    decisions_app = typer.Typer(help="List and import human-reviewed local decisions.")
    app.add_typer(decisions_app, name="decisions")

    @app.command("serve")
    def serve(
        transport: str = typer.Option("stdio", "--transport", help="Transport mode: stdio or sse"),
        port: int = typer.Option(8001, "--port", help="Port for SSE transport"),
    ) -> None:
        """Run Biscuit's FastMCP server providing live directional intelligence to Switch."""
        from biscuit.switch.server import FastMCPServer

        server = FastMCPServer()
        if transport == "sse":
            server.run(transport="sse", port=port)
        else:
            server.run(transport="stdio")

    @decisions_app.command("list")
    def decisions_list(
        status: str | None = typer.Option(None, "--status"),
        db: Path | None = typer.Option(None, "--db"),  # noqa: B008
        db_url: str | None = typer.Option(None, "--db-url"),
        prefer_store: bool = typer.Option(
            True,
            "--store/--local-cache",
            help="Read from the authoritative PostgreSQL store when one is configured (default), or fall back to the local SQLite cache.",
        ),
    ) -> None:
        """Display the decision index, optionally filtered by lifecycle state.

        When an authoritative store is available (``--db-url`` or the
        ``DATABASE_URL`` / ``BISCUIT_TEST_PG_DSN`` env vars), decisions are
        read directly from the production PostgreSQL relational store — the
        local SQLite cache is only a fallback for offline / development use.
        """
        resolved_url = prefer_store and _resolve_db_url(db_url, local_db=db)
        try:
            if resolved_url:
                rows = _list_decisions_from_store(resolved_url, status)
                source_label = "authoritative store"
            else:
                rows = list_decisions(db or _default_db(), status)
                source_label = "local cache"
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
        table = Table(title=f"Recovered decisions ({source_label})")
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
        db_url: str | None = typer.Option(None, "--db-url"),
        prefer_store: bool = typer.Option(
            True,
            "--store/--local-cache",
            help="Read from the authoritative PostgreSQL store when one is configured (default), or fall back to the local SQLite cache.",
        ),
    ) -> None:
        """Export verified decision records as MADR-style markdown files.

        When an authoritative store is available (``--db-url`` or the
        ``DATABASE_URL`` / ``BISCUIT_TEST_PG_DSN`` env vars), the export reads
        directly from the production PostgreSQL relational store. The local
        SQLite cache remains a fallback for offline / development use.
        """
        resolved_url = prefer_store and _resolve_db_url(db_url, local_db=db)
        try:
            if resolved_url:
                count = _export_madr_from_store(resolved_url, output)
                source_label = "authoritative store"
            else:
                count = export_madr(db or _default_db(), output)
                source_label = "local cache"
        except (OSError, ValueError) as exc:
            typer.echo(f"Export failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Exported {count} decision documents from {source_label} to {output}")

    db_app = typer.Typer(help="Authoritative store bootstrap and management.")
    app.add_typer(db_app, name="db")

    @db_app.command("init")
    def db_init(
        db_url: str | None = typer.Option(None, "--db-url"),
    ) -> None:
        """Create all SQLModel tables and (on PostgreSQL) apply the trigger migration."""
        from biscuit.db.session import get_engine, init_db

        url = _resolve_db_url(db_url)
        if url is None:
            typer.echo("No --db-url / DATABASE_URL / BISCUIT_TEST_PG_DSN configured", err=True)
            raise typer.Exit(2)
        try:
            engine = get_engine(url)
            init_db(engine, apply_postgres_triggers=True)
        except (OSError, ValueError) as exc:
            typer.echo(f"DB init failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Schema initialised on {engine.dialect.name}")

    @db_app.command("bootstrap-roles")
    def db_bootstrap_roles(
        fixture: Path = typer.Option(..., "--file"),  # noqa: B008
        db_url: str | None = typer.Option(None, "--db-url"),
    ) -> None:
        """Seed ContributorProjectRoleGrant rows from a JSON fixture.

        Required for the watchdog's invariant 2 (authoritative ruling): the
        authorization gate fails closed unless a verified project-scoped role
        grant exists for the ruling actor. See ``tests/fixtures/bootstrap/`` for
        the fixture format and ``SECURITY.md`` for the role taxonomy.
        """
        from biscuit.db.bootstrap import load_bootstrap_fixture
        from biscuit.db.session import get_engine, get_session, init_db

        url = _resolve_db_url(db_url)
        if url is None:
            typer.echo("No --db-url / DATABASE_URL / BISCUIT_TEST_PG_DSN configured", err=True)
            raise typer.Exit(2)
        try:
            engine = get_engine(url)
            init_db(engine, apply_postgres_triggers=False)
            with get_session(engine) as session:
                summary = load_bootstrap_fixture(session, fixture)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            typer.echo(f"Bootstrap failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(
            f"Bootstrapped {summary['contributors']} contributor(s), "
            f"{summary['grants']} role grant(s), "
            f"{summary['decisions']} provenanced decision(s)."
        )

    @db_app.command("import-decision")
    def db_import_decision(
        fixture: Path = typer.Option(..., "--file"),  # noqa: B008
        db_url: str | None = typer.Option(None, "--db-url"),
    ) -> None:
        """Import a provenanced decision record into the authoritative store.

        Alias for ``biscuit db bootstrap-roles`` when the fixture includes
        both the role grants and the decision records.
        """
        db_bootstrap_roles(fixture=fixture, db_url=db_url)

    @db_app.command("watchdog-check")
    def db_watchdog_check(
        diff_file: Path = typer.Option(..., "--diff"),  # noqa: B008
        repository_url: str = typer.Option(..., "--repository-url"),
        pr_number: int = typer.Option(..., "--pr-number"),
        commit_sha: str = typer.Option(..., "--commit-sha"),
        db_url: str | None = typer.Option(None, "--db-url"),
        execution_mode: str = typer.Option("AUDIT_DRY_RUN", "--mode"),
    ) -> None:
        """Run the PR Diff Watchdog against the authoritative store.

        Reads ``decision_alternatives``, ``evidence_spans``, ``code_anchors``
        and the ruling-quorum sign-offs from the production store, then runs
        the 4-invariant Warning Authorization Gate. The audit row is persisted
        to the append-only ``pr_watchdog_audit_log`` table.
        """
        from biscuit.db.decision_store import DatabaseDecisionStore, persist_watchdog_audit_log
        from biscuit.db.session import get_engine, get_session, init_db
        from biscuit.watchdog import (
            AuditLog,
            CandidateRetriever,
            PRDiffInspector,
            WarningAuthorizationGate,
        )
        from biscuit.watchdog import (
            ExecutionMode as WDExecutionMode,
        )

        url = _resolve_db_url(db_url)
        if url is None:
            typer.echo("No --db-url / DATABASE_URL / BISCUIT_TEST_PG_DSN configured", err=True)
            raise typer.Exit(2)
        try:
            engine = get_engine(url)
            init_db(engine, apply_postgres_triggers=False)
            diff_text = diff_file.read_text(encoding="utf-8")
            inspector = PRDiffInspector()
            pr_diff = inspector.parse_and_analyze(diff_text, repository_url=repository_url)
            mode = WDExecutionMode(execution_mode)
            with get_session(engine) as session:
                store = DatabaseDecisionStore(session)
                retriever = CandidateRetriever(store)
                candidates = retriever.retrieve(pr_diff)
                gate = WarningAuthorizationGate(store, audit_log=AuditLog())
                result = gate.evaluate(
                    pr_diff=pr_diff,
                    candidates=candidates,
                    execution_mode=mode,
                    pr_number=pr_number,
                    commit_sha=commit_sha,
                )
                for entry in gate.audit_log.entries:
                    persist_watchdog_audit_log(session, entry)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            typer.echo(f"Watchdog check failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(
            f"verdict={result.verdict.value} action={result.action_taken.value} "
            f"candidates={len(result.candidate_evaluations)} "
            f"authorized={len(result.authorized_alternative_ids)}"
        )

else:
    app = None


def main() -> None:
    if app is None:
        raise SystemExit(
            "The Biscuit CLI requires typer>=0.12 and rich>=13.7. "
            "Install these optional CLI dependencies before running it."
        )
    app()
