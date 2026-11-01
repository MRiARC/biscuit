# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with the pre-1.0 caveat that minor versions may contain breaking changes.

## [Unreleased]

No unreleased changes yet.

## [0.1.0] — 2026-11-01

First complete release. All five implementation streams merged, integration-
verified, all 366 tests passing, Ruff clean, CI green on Python 3.11 and 3.12.

### Added

#### Stream 1 — Core Relational Models, Database Engine & Universal Ingestion (PR #24, closes #17)

- SQLModel relational schemas for source threads, messages, message versions,
  extraction runs, decision alternatives, code anchors, and the append-only
  watchdog audit log.
- PostgreSQL 16 + pgvector engine with HNSW indexing for semantic retrieval,
  plus a SQLite-compatible fallback exercised by the test suite.
- Universal ingestion adapters for Discourse topics, GitHub PR reviews, and
  `mbox` mailing-list archives.
- Wire-byte SHA-256 provenance over the exact decoded UTF-8 content of every
  source message; versions are append-only and never overwritten.
- Database migrations under `src/biscuit/db/migrations/`.

#### Stream 2 — Code Grounding, Tree-sitter CST Parser & Forward Commit-DAG Lineage (PR #22, closes #18; CI companion PR #27)

- Tree-sitter CST parser with optional `[grounding]` extra
  (`tree-sitter`, `tree-sitter-python`, `tree-sitter-c`).
- Python AST normalizer producing masked-AST fingerprints that survive
  variable renames and reformatting (`coalesce(a, b)` matches
  `coalesce(x, y)` but never a ternary expression).
- Tier-1 code-anchor path/symbol lookup and Tier-4 semantic embedding
  retrieval (embedder injected as a callable).
- Forward commit-DAG lineage walker with merge-base resolution; replaces
  `git log --follow` so decisions stay bound to the correct code across
  renames, copies, and rewrites.
- Strict-mode CI gate (`BISCUIT_REQUIRE_TREE_SITTER=1`) that disables the
  optional-grammar test skip.

#### Stream 3 — 4-Pass Local LLM Extraction Pipeline & Exact Evidence Verifier (PR #23, closes #19)

- Sequential 4-pass pipeline: Llama 3.2-3B proposal discovery → slicer →
  Phi-3.5-mini argument mining → Qwen 2.5-Coder-7B ADR/MADR synthesis.
- `extraction_runs` audit table recording every pass output.
- Zero-based substring + SHA-256 exact-evidence verifier; `MODEL_INFERRED`
  authority never authorizes a ruling.
- Prompts and prompt-assembly helpers under `src/biscuit/extract/prompts/`.

#### Stream 4 — PR Diff Watchdog Guardrails & Switch FastMCP Agent Bridge (PR #28, closes #20)

- `PRDiffInspector` with unified git diff parsing (multiple files, renames,
  new/deleted files, `\ No newline` markers) and masked-AST subtree
  fingerprints.
- `WarningAuthorizationGate` evaluating all four invariants for every
  candidate and recording the verdict in the append-only
  `pr_watchdog_audit_log`:
  1. Decision state `lifecycle_status == 'REJECTED'`
  2. Authoritative ruling resolves to a `VERIFIED_EXACT` `RULING` span
     under a verified project-scoped human quorum
  3. Alternative-specific active code anchor with unambiguous parent-aware
     lineage
  4. AST structural equivalence between the rejected construct and the
     added code
- `AUDIT_DRY_RUN` default execution mode; `INTERACTIVE_HITL` routes a
  maintainer approval card; `PRODUCTION_COMMENT` publishes.
- `PRCommentFormatter` producing verifiable in-line GitHub review comments
  with the exact ruling quote, author, permalink, and zero-based
  `[start_char, end_char)` offsets.
- FastMCP server exposing `query_decisions`, `check_pr_diff`, and
  `draft_living_decision` tools; `draft_living_decision` stores exact
  message versions with SHA-256 over UTF-8 bytes and never assigns
  lifecycle states.
- Switch agent-controller 0.31.0 activity client pinned to upstream commit
  `7e57cd433a922bc91a00150c106e6ef1f15b1057`; Biscuit never imports
  Switch's internal Python packages.
- HMAC-SHA256 callback signature verification with constant-time
  comparison, UUIDv4 idempotency keys, replay prevention (409 Conflict
  semantics), 72-hour TTL expiry, and request/action/payload/idempotency
  compatibility binding.
- Slack Block Kit living-decision approval cards and PR warning
  authorization cards that pin the approvable payload SHA-256.

#### Stream 5 — Evaluation Benchmark Suite, 20 Held-Out PR Fixtures & Rich CLI (PR #25, closes #21)

- Six metric contract suites: proposal F1 ≥ 0.85, judged-state accuracy
  ≥ 0.85, false rejection 0, evidence EMR 100%, AST precision ≥ 0.80,
  watchdog precision ≥ 0.95.
- PEP 505 / 557 / 557 withheld-ground-truth fixtures with SHA-256,
  manifest/provenance metadata, and separately recorded mock prediction
  outputs.
- 20 independent synthetic PR watchdog cases (5 violation / 10 benign /
  5 ambiguous) with validated unified diffs.
- Typer 0.12 + Rich CLI: `biscuit ingest`, `biscuit decisions list|import`,
  `biscuit evaluate`, `biscuit export` (MADR-style).
- Recorded-replay scoring by default; `--no-simulate` fails closed;
  `--predictions` for real output artifacts; `--stream3` opt-in integration
  through an injected Stream 3 adapter.
- `--semantic-embeddings` opt-in loopback-only local Ollama
  `nomic-embed-text` scorer for predicted rationale, with
  `NOT_MEASURED` reporting when ground-truth vectors are missing.
- `--profile-memory` opt-in Linux host point sampling, explicitly labeled
  `PARTIAL_HOST_SAMPLES_ONLY`.
- Reason-coded per-case `WARN`/`ABSTAIN` SQLite audit with ruling and
  authorization references; migration and idempotency tests.

#### Foundation & docs (PRs #1, #2, #3, #4, #6, #7, #26, #29)

- Apache-2.0 license, mandatory PR template, GitHub Actions CI matrix
  (ruff + pytest on Python 3.11 and 3.12).
- Nine authoritative design documents under `docs/` covering the
  two-pillar architecture, tech stack, storage and retrieval, ingestion
  and extraction, code grounding and AST, evaluation and benchmarks,
  and living decision intelligence.
- Next.js product-intro site under `intro/` with its own CI workflow
  (`npm run typecheck`, `npm run build` on Node 22).

### Security

- All watchdog authorization abstains into the append-only audit log when
  any of the four invariants fails; verdicts include
  `WARNING_AUTHORIZED`, `ABSTAINED_UNCERTAIN`, `NO_REJECTED_MATCH`, and
  `ALTERNATIVE_MISMATCH` for cross-alternative associations.
- `HITL_CARD_POSTED` and `COMMENT_PUBLISHED` states are only set on
  confirmed side effects; non-hex and all-zero head SHAs are rejected at
  the authorization boundary.
- Living-decision transcript persistence implements append-only
  message-version and evidence storage with exact-version SHA-256
  provenance.

### Known acceptance gates (documented, non-code)

The following items are documented in PR #25's description and are **not**
blockers for the code deliverable, but are required for full production
acceptance:

- Real, legally usable, independently reviewed PEP 505/572/557 source
  archives with verified human rulings and author roles. The shipped
  fixtures are synthetic and clearly labeled
  `SYNTHETIC_REPLAY_NOT_HISTORICAL`.
- Pinned `nomic` semantic reference vectors for ground truth; the
  shipped synthetic ground truth has no pinned semantic vectors.
- A complete representative 8 GB target-host run with model,
  PostgreSQL, KV cache, swapping/OOM checks, and per-service RSS/PSS.
  The `--profile-memory` option ships partial host telemetry only.
- The CLI cache is not the authoritative decision store; verified
  recovered decisions and production MADR export require the
  storage/extraction integration.

[Unreleased]: https://github.com/MRiARC/biscuit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/MRiARC/biscuit/releases/tag/v0.1.0
