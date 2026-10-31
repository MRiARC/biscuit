"""Local CLI storage and Typer command regression tests."""

from __future__ import annotations

import json

import pytest

from biscuit.cli.scorecard import render_scorecard
from biscuit.cli.storage import (
    export_madr,
    import_decisions,
    list_decisions,
)
from biscuit.evaluate.runner import run_benchmark


def _decision() -> dict:
    return {
        "decision_id": "ADR-001",
        "title": "Prefer conservative proof",
        "status": "REJECTED",
        "context": "Review context",
        "rationale": "Rejected alternative after review",
        "consequences": "No automatic warnings",
    }


def test_local_decision_import_filter_and_markdown_export(tmp_path) -> None:
    db = tmp_path / "workspace.sqlite3"
    source = tmp_path / "decisions.json"
    source.write_text(json.dumps([_decision()]), encoding="utf-8")
    assert import_decisions(source, db) == 1
    assert [x["decision_id"] for x in list_decisions(db, "REJECTED")] == ["ADR-001"]
    assert list_decisions(db, "ACCEPTED") == []
    assert export_madr(db, tmp_path / "decisions") == 1
    contents = (tmp_path / "decisions" / "ADR-001.md").read_text(encoding="utf-8")
    assert "# Prefer conservative proof" in contents
    assert "## Consequences" in contents


def test_invalid_decisions_cannot_inject_path_or_sql(tmp_path) -> None:
    db = tmp_path / "workspace.sqlite3"
    source = tmp_path / "bad.json"
    source.write_text(json.dumps([{**_decision(), "decision_id": "../bad"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid decision ID"):
        import_decisions(source, db)
    with pytest.raises(ValueError, match="Invalid decision state"):
        list_decisions(db, "REJECTED' OR 1=1")


def test_scorecard_marks_unmeasured_truthfully() -> None:
    outcome = run_benchmark("pep-505")
    rendered = render_scorecard(outcome)
    assert "NOT_MEASURED" in rendered
    assert "SYNTHETIC" in rendered


def test_typer_evaluate_and_scorecard(tmp_path) -> None:
    typer = pytest.importorskip("typer")
    from typer.testing import CliRunner
    from biscuit.cli.main import app

    assert typer is not None
    if app is None:
        pytest.skip("Typer is not installed")
    runner = CliRunner()
    output = tmp_path / "scorecard.json"
    result = runner.invoke(app, ["evaluate", "--benchmark", "pep-505", "--output", str(output)])
    assert result.exit_code == 2
    assert output.exists()
    assert "NOT_MEASURED" in result.output


def test_typer_decisions_list_and_export(tmp_path) -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner
    from biscuit.cli.main import app

    if app is None:
        pytest.skip("Typer is not installed")
    db = tmp_path / "db.sqlite3"
    source = tmp_path / "decisions.json"
    source.write_text(json.dumps([_decision()]), encoding="utf-8")
    runner = CliRunner()
    imported = runner.invoke(app, ["decisions", "import", "--file", str(source), "--db", str(db)])
    assert imported.exit_code == 0, imported.output
    listed = runner.invoke(app, ["decisions", "list", "--status", "REJECTED", "--db", str(db)])
    assert listed.exit_code == 0, listed.output
    assert "ADR-001" in listed.output
    exported = runner.invoke(
        app, ["export", "--output", str(tmp_path / "out"), "--db", str(db)]
    )
    assert exported.exit_code == 0, exported.output
    assert (tmp_path / "out" / "ADR-001.md").is_file()
