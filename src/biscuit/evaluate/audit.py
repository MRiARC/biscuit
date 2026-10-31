"""Local SQLite audit storage for evaluated PR warnings and abstentions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


_AUDIT_COLUMNS = {
    "reason_code": "TEXT NOT NULL DEFAULT 'UNSPECIFIED'",
    "alternative_id": "TEXT",
    "authorization": "TEXT",
    "ruling_source_message_id": "TEXT",
    "ruling_quote_sha256": "TEXT",
}


def write_pr_audit(
    database: Path,
    *,
    benchmark_id: str,
    run_sha256: str,
    rows: list[dict[str, Any]],
) -> int:
    """Store idempotent, evidence-linked audit rows; migrate older local caches."""
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
                reason_code TEXT NOT NULL DEFAULT 'UNSPECIFIED',
                alternative_id TEXT,
                authorization TEXT,
                ruling_source_message_id TEXT,
                ruling_quote_sha256 TEXT,
                PRIMARY KEY (run_sha256, benchmark_id, case_id)
            )"""
        )
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(pr_watchdog_audit_log)")
        }
        for column, declaration in _AUDIT_COLUMNS.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE pr_watchdog_audit_log ADD COLUMN {column} {declaration}"
                )
        conn.executemany(
            """INSERT OR IGNORE INTO pr_watchdog_audit_log
               (run_sha256, benchmark_id, case_id, ground_truth_class, action,
                correct, reason_code, alternative_id, authorization,
                ruling_source_message_id, ruling_quote_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_sha256,
                    benchmark_id,
                    row["case_id"],
                    row["ground_truth_class"],
                    row["action"],
                    int(row["correct"]),
                    row.get("reason_code", "UNSPECIFIED"),
                    row.get("alternative_id"),
                    row.get("authorization"),
                    row.get("ruling_source_message_id"),
                    row.get("ruling_quote_sha256"),
                )
                for row in rows
            ],
        )
    return len(rows)
