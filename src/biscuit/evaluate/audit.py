"""Local SQLite audit storage for evaluated PR warnings and abstentions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def write_pr_audit(
    database: Path,
    *,
    benchmark_id: str,
    run_sha256: str,
    rows: list[dict[str, Any]],
) -> int:
    """Persist deterministic per-case results without overwriting prior runs."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pr_watchdog_audit_log (
                run_sha256 TEXT NOT NULL,
                benchmark_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                ground_truth_class TEXT NOT NULL,
                action TEXT NOT NULL,
                correct INTEGER NOT NULL,
                PRIMARY KEY (run_sha256, benchmark_id, case_id)
            )"""
        )
        conn.executemany(
            """INSERT OR IGNORE INTO pr_watchdog_audit_log
               (run_sha256, benchmark_id, case_id, ground_truth_class, action, correct)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_sha256,
                    benchmark_id,
                    row["case_id"],
                    row["ground_truth_class"],
                    row["action"],
                    int(row["correct"]),
                )
                for row in rows
            ],
        )
    return len(rows)
