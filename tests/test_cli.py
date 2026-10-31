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
    import typer
    from typer.testing import CliRunner

    from biscuit.cli.main import app

    assert typer is not None
    assert app is not None
    runner = CliRunner()
    output = tmp_path / "scorecard.json"
    result = runner.invoke(app, ["evaluate", "--benchmark", "pep-505", "--output", str(output)])
    assert result.exit_code == 2
    assert output.exists()
    assert "NOT_MEASURED" in result.output


def test_typer_decisions_list_and_export(tmp_path) -> None:
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


def test_discourse_ingest_fetches_missing_post_pages(monkeypatch) -> None:
    from biscuit.cli import storage

    def fake_read_json(url: str, allowed_host: str) -> dict:
        assert allowed_host == "forum.example.org"
        if url.endswith("/t/topic/123.json"):
            return {
                "id": 123, "title": "Synthetic debate",
                "post_stream": {
                    "stream": [11, 12],
                    "posts": [{
                        "id": 11, "username": "alice", "created_at": "2026-01-01",
                        "raw": "First post",
                    }],
                },
            }
        assert "/t/123/posts.json?" in url
        assert "post_ids" in url
        return {
            "post_stream": {
                "posts": [{
                    "id": 12, "username": "bob", "created_at": "2026-01-02",
                    "cooked": "<p>Second post</p>",
                }],
            },
        }

    monkeypatch.setattr(storage, "_read_topic_json", fake_read_json)
    result = storage._fetch_discourse("https://forum.example.org/t/topic/123")
    assert [m["message_id"] for m in result["messages"]] == ["11", "12"]
    assert result["messages"][0]["content_source"] == "RAW"
    assert result["messages"][1]["content_source"] == "RENDERED_HTML_UNVERIFIED"


def test_discourse_ingest_rejects_incomplete_page(monkeypatch) -> None:
    from biscuit.cli import storage

    def fake_read_json(url: str, allowed_host: str) -> dict:
        if url.endswith(".json") and "/posts.json" not in url:
            return {
                "id": 123, "post_stream": {
                    "stream": [11, 12], "posts": [{"id": 11, "raw": "post"}],
                },
            }
        return {"post_stream": {"posts": []}}

    monkeypatch.setattr(storage, "_read_topic_json", fake_read_json)
    with pytest.raises(ValueError, match="Incomplete"):
        storage._fetch_discourse("https://forum.example.org/t/topic/123")


def test_stdlib_benchmark_cli_runs_without_typer(tmp_path) -> None:
    import subprocess
    import sys

    command = [
        sys.executable, "-m", "biscuit.evaluate",
        "--benchmark", "pep-505", "--output", "json",
    ]
    executed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert executed.returncode == 2
    payload = json.loads(executed.stdout)
    assert payload["verdict"] == "NOT_MEASURED"
    assert payload["dataset_kind"] == "SYNTHETIC_SMOKE_NOT_HISTORICAL"


def test_stdlib_cli_strict_offsets_rejects_unmeasured_fixture() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "biscuit.evaluate", "--strict-offsets", "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "Strict offsets" in result.stderr


def test_export_madr_3_with_structured_options(tmp_path) -> None:
    db = tmp_path / "workspace.sqlite"
    source = tmp_path / "source.json"
    decision = {
        **_decision(),
        "deciders": ["core-maintainer"],
        "date": "2026-11-01",
        "technical_story": "Issue #21",
        "decision_drivers": ["Exact evidence", "No warning spam"],
        "considered_options": [{
            "name": "Conservative gating",
            "description": "Allow only verified rejection rulings",
            "pros": ["No unverified warning"],
            "cons": ["May abstain"],
        }],
        "links": ["https://example.org/thread/1"],
    }
    source.write_text(json.dumps([decision]), encoding="utf-8")
    assert import_decisions(source, db) == 1
    assert export_madr(db, tmp_path / "madr") == 1
    text = (tmp_path / "madr" / "ADR-001.md").read_text(encoding="utf-8")
    for required in (
        "## Context and Problem Statement", "## Decision Drivers",
        "## Considered Options", "## Decision Outcome",
        "## Pros and Cons of the Options", "## Links",
        "core-maintainer", "Conservative gating", "No unverified warning",
    ):
        assert required in text
    assert "VERIFIED_EXACT" not in text


def test_decision_schema_migrates_existing_local_cache(tmp_path) -> None:
    import sqlite3

    from biscuit.cli.storage import connect_db

    db = tmp_path / "previous.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE decisions (decision_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "status TEXT NOT NULL, context TEXT NOT NULL, rationale TEXT NOT NULL, "
            "consequences TEXT NOT NULL)"
        )
    with connect_db(db) as conn:
        fields = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
        assert "record_json" in fields
