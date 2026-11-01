# Biscuit

**Open-Source Decision Intelligence and Knowledge Recovery.**

[![CI](https://github.com/MRiARC/biscuit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/MRiARC/biscuit/actions/workflows/ci.yml)
[![Intro App CI](https://github.com/MRiARC/biscuit/actions/workflows/intro.yml/badge.svg?branch=main)](https://github.com/MRiARC/biscuit/actions/workflows/intro.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/MRiARC/biscuit?include_prereleases)](https://github.com/MRiARC/biscuit/releases)

> **Why does open source forget?** Every major project accumulates decades of
> technical debate — across 300-comment Discourse threads, 10-year-old mailing
> lists, and closed GitHub PR reviews. When a maintainer leaves, the *why*
> behind a rejected idea is lost, and the next contributor reopens the same
> debate. Biscuit recovers that lost rationale, grounds every decision in the
> code it governs, and watches new PRs so the same rejected patterns are never
> silently reintroduced.

---

## What Biscuit does

Biscuit is a **two-pillar decision-intelligence platform** for open-source
projects. It runs entirely on open-source models under an 8 GB memory target.

```
┌────────────────────────────────────────────────────────────────────────┐
│                   BISCUIT: THE TWO-PILLAR ARCHITECTURE                 │
├────────────────────────────────────────────────────────────────────────┤
│  PILLAR 1 — THE PAST (Day -N → Day 0)                                  │
│  Historical Knowledge Recovery Engine                                  │
│  • Ingests 5–10 years of archives (Discourse, GitHub PRs, mbox)        │
│  • Runs a 4-pass local-LLM pipeline (Llama → Phi → Qwen)                │
│  • Verifies human rulings with a zero-based SHA-256 evidence verifier  │
│  • Grounds decisions into code via tree-sitter CST + AST anchors        │
│  • Tracks forward commit-DAG lineage across renames and merges         │
│                                                                        │
│  PILLAR 2 — THE PRESENT & FUTURE (Day 1+)                              │
│  Living Decision Intelligence (powered by Switch)                      │
│  • FastMCP agent bridge exposes query / check / draft tools            │
│  • PR Diff Watchdog: 4-invariant authorization gate                   │
│  • AUDIT_DRY_RUN default; INTERACTIVE_HITL maintainer approval cards   │
│  • HMAC-SHA256 signed callbacks + replay prevention                    │
└────────────────────────────────────────────────────────────────────────┘
```

Both pillars share the **same PostgreSQL 16 + pgvector** relational knowledge
base. Append-only source versions, evidence links, and database triggers
provide auditable provenance under the documented role/trigger assumptions.
Warning authorization requires verified, alternative-specific human authority
*and* code-lineage evidence; `MODEL_INFERRED` authority never authorizes a
warning, and avoiding false alerts is an evaluation objective, not an
unconditional guarantee.

See [`docs/TWO_PILLAR_ARCHITECTURE.md`](docs/TWO_PILLAR_ARCHITECTURE.md) for
the authoritative master architecture and [`docs/CORE_IDEA.md`](docs/CORE_IDEA.md)
for the system concept.

---

## Key features

- **Universal ingestion** — Discourse, GitHub PR reviews, and `mbox` archives,
  all written with wire-byte SHA-256 provenance over the exact decoded
  content. Versions are append-only; nothing is ever overwritten.
- **4-pass local-LLM pipeline** — Llama 3.2-3B discovers proposals, a slicer
  chunks long threads, Phi-3.5-mini mines arguments, and Qwen 2.5-Coder-7B
  synthesizes ADR/MADR records. Pass outputs land in an `extraction_runs`
  audit table.
- **Exact evidence verifier** — every quoted span is checked by zero-based
  substring match + SHA-256 over UTF-8 bytes against the stored source. A
  mismatch fails closed; `MODEL_INFERRED` is never authoritative.
- **Code grounding** — tree-sitter CST + Python AST normalization produces
  masked-AST fingerprints that survive variable renames and reformatting
  (`coalesce(a, b)` matches `coalesce(x, y)` but never a ternary). Tier-1
  anchor path/symbol lookup plus Tier-4 semantic retrieval.
- **Forward commit-DAG lineage** — instead of `git log --follow`, Biscuit
  walks the commit DAG forward and resolves merge-base boundaries so
  decisions stay bound to the right code even across renames, copies, and
  rewrites.
- **PR Diff Watchdog** — a 4-invariant authorization gate
  (decision state · verified ruling quorum · alternative code anchor · AST
  structural equivalence) records every candidate's verdict in an append-only
  `pr_watchdog_audit_log`. `AUDIT_DRY_RUN` is the default.
- **Switch FastMCP bridge** — `query_decisions`, `check_pr_diff`, and
  `draft_living_decision` tools, with HMAC-SHA256 signed callbacks, UUIDv4
  idempotency keys, replay prevention (409 semantics), and 72-hour TTL.
- **Offline evaluation suite** — 6 metric contracts (proposal F1 ≥ 0.85,
  judged-state accuracy ≥ 0.85, false rejection 0, evidence EMR 100%, AST
  precision ≥ 0.80, watchdog precision ≥ 0.95), PEP 505/557/572 withheld
  ground truth, and 20 held-out PR watchdog cases.
- **Rich CLI** — `biscuit ingest`, `biscuit decisions`, `biscuit evaluate`,
  `biscuit export`. Recorded-replay scoring by default; never exposes
  withheld oracle text to the runtime predictor.

---

## Installation

Biscuit targets **Python ≥ 3.11** (3.12 recommended) and ships on PyPI as
`biscuit-intel`.

```bash
# From source (developer install, includes dev tooling and strict grammars)
git clone https://github.com/MRiARC/biscuit.git
cd biscuit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,grounding]"
```

| Extra | What it pulls in |
|-------|------------------|
| *(default)* | `pydantic`, `sqlmodel`, `sqlalchemy`, `httpx`, `fastmcp`, `pgvector`, `typer`, `rich`, … |
| `[grounding]` | `tree-sitter`, `tree-sitter-python`, `tree-sitter-c` — required for strict CST parsing |
| `[dev]` | `pytest`, `pytest-cov`, `ruff` |

### Optional runtime services

- **PostgreSQL 16 + pgvector** for the relational+vector store. A
  SQLite-compatible fallback is exercised by the test suite so most tests
  run without a live database. To exercise the live-PG integration tests:

  ```bash
  export TEST_POSTGRES_URL="postgresql://biscuit:biscuit@localhost:5432/biscuit"
  pytest tests/test_db.py
  ```

- **Local LLM runtime** (Ollama or equivalent) for live inference. The test
  suite and `biscuit evaluate` default to **recorded-replay mode** so nothing
  requires a running model.

---

## Quickstart

```bash
# 1. Verify the install + lint + tests
ruff check .
pytest -q                                 # 366 passed, 4 skipped (live-PG only)

# 2. Run the evaluation benchmark in recorded-replay mode
biscuit evaluate --all --format json      # 3 PEP benchmarks, 13 metric contracts

# 3. Strict-mode grounding tests (requires [grounding] extra)
BISCUIT_REQUIRE_TREE_SITTER=1 pytest -q tests/test_grounding.py

# 4. List / import locally-reviewed decisions and export as MADR
biscuit decisions list
biscuit decisions import path/to/decision.json
biscuit export --format madr --out ./adr/

# 5. Ingest a Discourse topic snapshot into the local SQLite cache
biscuit ingest --source discourse --topic 12345
```

CLI help is always one flag away:

```bash
biscuit --help
biscuit evaluate --help
biscuit decisions --help
```

---

## Project structure

```
biscuit/
├── src/biscuit/
│   ├── cli/            # Typer/Rich CLI: evaluate, ingest, decisions, export
│   ├── models/         # SQLModel relational schemas
│   ├── db/             # PostgreSQL + pgvector engine, SQLite fallback, migrations
│   ├── ingest/         # Discourse / GitHub / mbox adapters, wire-byte SHA-256 hasher
│   ├── extract/        # 4-pass LLM pipeline, prompts, audit run bookkeeping
│   ├── provenance/     # zero-based substring + SHA-256 evidence verifier & matcher
│   ├── grounding/      # tree-sitter CST parser, AST normalizer, forward DAG lineage
│   ├── watchdog/       # PR diff inspector, candidate retriever, authorization gate, formatter
│   ├── switch/         # FastMCP server, Switch activity client, Block Kit cards, callback security
│   └── evaluate/       # metric contracts, scorer, recorded-replay harness, audit persistence
├── tests/              # 17 test modules, 366 tests, synthetic fixtures (no live data)
├── docs/               # 9 authoritative design specs (see "Documentation" below)
├── intro/               # Next.js product-intro site (separate CI: typecheck + build)
├── .github/workflows/  # ci.yml (Python), intro.yml (Node), release.yml (CD)
└── pyproject.toml
```

---

## Documentation

The `docs/` directory contains the authoritative design specifications. Read
them in roughly this order when getting oriented:

| Document | What it covers |
|----------|----------------|
| [`CORE_IDEA.md`](docs/CORE_IDEA.md) | System concept, the "lost why" problem, two-pillar rationale |
| [`TWO_PILLAR_ARCHITECTURE.md`](docs/TWO_PILLAR_ARCHITECTURE.md) | Master architecture blueprint (Mermaid diagrams) |
| [`TECH_STACK.md`](docs/TECH_STACK.md) | Technology choices and hackathon constraint checklist |
| [`STORAGE_AND_RETRIEVAL.md`](docs/STORAGE_AND_RETRIEVAL.md) | PostgreSQL 16 + pgvector schema, HNSW, immutable message versions |
| [`INGESTION_AND_EXTRACTION.md`](docs/INGESTION_AND_EXTRACTION.md) | Universal adapters, 4-tier noise suppression, 4-pass pipeline |
| [`CODE_GROUNDING_AND_AST.md`](docs/CODE_GROUNDING_AND_AST.md) | 4-tier grounding, AST anchors, forward DAG lineage, authorization invariants |
| [`EVALUATION_AND_BENCHMARKS.md`](docs/EVALUATION_AND_BENCHMARKS.md) | Metric contracts, PEP 505/557/572 fixtures, 20-PR watchdog suite |
| [`LIVING_DECISION_INTELLIGENCE.md`](docs/LIVING_DECISION_INTELLIGENCE.md) | Switch FastMCP bridge, watchdog authorization, callback security |
| [`TECH_STACK_AND_ARCHITECTURE.md`](docs/TECH_STACK_AND_ARCHITECTURE.md) | Combined tech-stack + architecture reference |

---

## Testing

```bash
# Standard CI matrix (what GitHub Actions runs on push)
pytest -q                       # 366 passed, 4 skipped (live-PG integration only)
ruff check .                    # all checks pass

# Strict tree-sitter mode (exercises the optional [grounding] extra)
BISCUIT_REQUIRE_TREE_SITTER=1 pytest -q tests/test_grounding.py

# Live PostgreSQL integration
export TEST_POSTGRES_URL="postgresql://…"
pytest tests/test_db.py
```

The 4 skipped tests require a live PostgreSQL DSN via `TEST_POSTGRES_URL`
(or `BISCUIT_TEST_PG_DSN`). They are skipped by design, not by omission —
the SQLite fallback path is exercised separately.

---

## Models used

Per the hackathon's open-source and lightweight constraints, Biscuit uses
three local models at runtime, each picked for a specific pipeline stage:

| Model | Role | Why this model |
|-------|------|----------------|
| **Qwen 2.5-Coder-7B-Instruct** | Pass 4 — ADR/MADR synthesis, code-to-decision grounding | Top-tier open-weights function calling; understands AST structures, Git diffs, and Python syntax |
| **Llama 3.2-3B-Instruct** | Pass 1 — proposal discovery, thread filtering, real-time chat interception | < 2.5 GB RAM, 80+ tok/s on CPU; handles noisy 100+ comment threads |
| **Phi-3.5-mini-instruct** | Pass 3 — argumentation mining, long-context deliberation | 128k context window; trained on synthetic reasoning data |

Embeddings (`nomic-embed-text`) are an opt-in semantic-retrieval tier
(`--semantic-embeddings`) and never replace exact-match evidence verification.
See [`HACKATHON.md`](HACKATHON.md) for the full model justification.

---

## Contributing

Contributions are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md)
for the workflow (fork → branch → PR → two-reviewer approval → squash-merge),
commit-message conventions, and the CI gate. By participating you agree to
abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Security

Found a security issue? Please **do not** open a public issue. See
[`SECURITY.md`](SECURITY.md) for the private disclosure process and the
project's security scope (signed callbacks, replay prevention, audit-log
immutability, watchdog fail-closed semantics).

---

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

---

## Team

**Chole Bhature**

- Adarsh Chauhan — [@mriarc-08](https://github.com/mriarc-08)
- Dakshesh Sharma — [@daksheshsharma2409](https://github.com/daksheshsharma2409)
- Utkarsh Singh — [@utksh1](https://github.com/utksh1)

Originally submitted as Problem 06 — *Open Source Decision Intelligence and
Knowledge Recovery*. See [`HACKATHON.md`](HACKATHON.md) for the original
hackathon submission.
