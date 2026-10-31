"""Deterministic extraction audit manifests and PostgreSQL persistence adapter.

The extraction workstream remains independently testable: callers inject a live DB
connection only when persistence is required. Without an audit sink the run is
explicitly reported as NOT persisted.
"""

import hashlib
import json
from typing import Any, Protocol
from uuid import UUID


def canonical_json(value: Any) -> str:
    """Stable JSON encoding used for in-process audit fingerprints."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class AuditWriter(Protocol):
    def write(self, record: dict[str, Any]) -> None:
        """Persist a RUNNING/SUCCESS/FAILED extraction_runs transition or raise."""


class PostgresExtractionAuditWriter:
    """DB-API compatible writer for docs/STORAGE_AND_RETRIEVAL.md::extraction_runs.

    The caller owns the PostgreSQL connection and must supply a UUID-backed
    source_thread_id and model/environment revision fingerprints. This adapter
    performs its own commit/rollback; do not share its connection with another
    uncommitted transaction.
    """

    def __init__(self, connection: Any):
        self.connection = connection

    def write(self, record: dict[str, Any]) -> None:
        if not isinstance(record.get("source_thread_id"), UUID):
            raise ValueError("Persisted audit requires an actual source_thread_id UUID")
        try:
            cursor = self.connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO extraction_runs (
                        id, source_thread_id, pipeline_version, schema_version,
                        run_config, run_config_sha256, environment_lock_sha256,
                        model_pass1_name, model_pass1_revision,
                        model_pass3_name, model_pass3_revision,
                        model_pass4_name, model_pass4_revision,
                        embedding_model_name, embedding_model_revision,
                        prompt_templates_hash, source_thread_snapshot_sha256,
                        run_status, records_extracted, completed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s::jsonb, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s,
                        CASE WHEN %s IN ('SUCCESS','FAILED','PARTIAL')
                             THEN NOW() ELSE NULL END
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        run_status = EXCLUDED.run_status,
                        records_extracted = EXCLUDED.records_extracted,
                        completed_at = EXCLUDED.completed_at
                    """,
                    (
                        record["id"], record["source_thread_id"],
                        record["pipeline_version"], record["schema_version"],
                        canonical_json(record["run_config"]),
                        record["run_config_sha256"], record["environment_lock_sha256"],
                        record["model_pass1_name"], record["model_pass1_revision"],
                        record["model_pass3_name"], record["model_pass3_revision"],
                        record["model_pass4_name"], record["model_pass4_revision"],
                        record["embedding_model_name"], record["embedding_model_revision"],
                        record["prompt_templates_hash"],
                        record["source_thread_snapshot_sha256"],
                        record["run_status"], record["records_extracted"],
                        record["run_status"],
                    ),
                )
            finally:
                cursor.close()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
