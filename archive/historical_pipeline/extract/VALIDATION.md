# Stream 3: production validation and acceptance

Automated CI executes the deterministic mock and a loopback HTTP transport test; it
**does not** run a real Llama/Phi/Qwen model or prove the 8 GB host-RAM requirement.
Do not mark live inference, actual PostgreSQL persistence, or the hardware
acceptance condition complete from mock CI alone.

## Prerequisites

- A Linux machine with Ollama and sufficient capacity to run the three pinned local
  models: `llama3.2:3b`, `phi3.5`, `qwen2.5-coder:7b`.
- A real PostgreSQL instance with `docs/STORAGE_AND_RETRIEVAL.md` DDL applied.
- A persisted `source_thread_id` UUID, source-message-version IDs, project-scoped
  verified role grants, and persisted alternative/argument UUIDs.
- Pinned model artifact revisions (not model display-name strings), a genuine
  SHA-256 hash of the dependency/environment lockfile, and a representative
  chronological thread snapshot.

## Live check

```sh
python -m pip install -e '.[dev]'
python -m pip install 'psycopg[binary]'
ollama pull llama3.2:3b
ollama pull phi3.5
ollama pull qwen2.5-coder:7b
ollama list
curl -fsS http://127.0.0.1:11434/api/ps
pytest -q tests/test_extract.py tests/test_provenance.py tests/test_extract_audit.py
```

Call `ExtractionPipeline(LocalLLMClient(mock_mode=False), audit_writer=
PostgresExtractionAuditWriter(connection))` with **real** values for
`source_thread_id`, `model_revisions` (`pass1`, `pass3`, `pass4`,
`embedding`), `environment_lock_sha256`, and `alternative_ids` /
`argument_ids` (plus `verified_project_roles`). A no-database
`ExtractionPipeline` run is allowed for local exploration, but records
`audit_metadata["audit_persisted"] = False`. Do not report those runs as
database-backed evidence.

Inspect the resulting `extraction_runs` row and verify that status transitions
`RUNNING -> SUCCESS` or `RUNNING -> FAILED`; that the actual PostgreSQL
JSONB-text run-config digest, source snapshot digest, lock hash and model revisions
are populated; and that evidence target UUIDs resolve to persisted rows.

## 8 GB host memory acceptance

The inference modules request `keep_alive=0` and explicitly confirm each model
is absent from Ollama `/api/ps` before loading the next. This ensures unload
failure is treated as fatal, **not** that peak complete-host RAM is already proven.

On a Linux host, independently sample `/proc/meminfo` during the full workload
(including Ollama, database, Python process and OS). For example:

```sh
# Start before inference and stop immediately after it finishes.
while true; do
  awk '/^MemTotal:/ {total=$2} /^MemAvailable:/ {avail=$2}
       /^SwapTotal:/ {swap_total=$2} /^SwapFree:/ {swap_free=$2}
       END {print systime(), (total-avail)*1024, (swap_total-swap_free)*1024}' \
    /proc/meminfo >> /tmp/biscuit-memory-samples.tsv
  sleep 0.2
done
```

Acceptance: **measured complete-host peak RAM <= 8,000,000,000 bytes**, no OOM,
no reliance on swap, and all three passes actually execute. Capture model tags,
file hashes, Ollama version, context window, quantization, hardware and host OS;
include p50/p95 latency and a real message workload in the evidence bundle.
If hardware or PostgreSQL is unavailable, this gate remains **NOT VERIFIED**.
