# Biscuit: Technology Stack Specification

**Project:** Biscuit — Open Source Decision Intelligence and Knowledge Recovery  
**Team:** Chole Bhature (`@mriarc-08`, `@daksheshsharma2409`, `@utksh1`)  
**Track:** Problem 06 — Open Source Decision Intelligence and Knowledge Recovery  
**Document Level:** Authoritative Master Tech Stack Specification  

---

## 1. Executive Summary & Constraint Checklist

Biscuit is built to extract, verify, ground, and govern architectural decisions across 10-year open-source archives and live engineering chat channels.

### Strict Hackathon Operational Constraints:
- **Open-Source Models Only:** Strictly zero proprietary cloud APIs (no OpenAI, no Anthropic, no Google Gemini API in production runtime).
- **Hardware Memory Boundary:** 8 GB system-RAM acceptance target, not yet demonstrated by full-stack profiling; all figures below are estimates.
- **Offline & Local Execution Scope:** Local model inference, vector search, AST parsing, and evaluation benchmarks execute completely offline without internet access. Live chat interception via Switch and GitHub PR webhook sync require network connectivity.

```
┌────────────────────────────────────────────────────────────────────────┐
│                          BISCUIT TECH STACK                            │
├────────────────────────────────────────────────────────────────────────┤
│  Layer               Technology Choice        Rationale                │
├────────────────────────────────────────────────────────────────────────┤
│  Core Language       Python >=3.11           3.12 recommended; native │
│                                               Tree-sitter bindings     │
│  Data Modeling       Pydantic v2 + SQLModel   Rust-backed validation,  │
│                                               strict schema contracts  │
│  Relational + Vector PostgreSQL 16 + pgvector Scalable hybrid search,  │
│                      (Docker container)       HNSW indexing            │
│  Unit Test DB        SQLite (in-memory)       Portable logic tests only│
│  Syntax Parsing      Tree-sitter (CST) +      Multi-language CST shape │
│                      Python ast (AST)         concrete syntax trees    │
│  Embedding Model     nomic-embed-text-v1.5    Matryoshka 768d, ~275MB  │
│  Pipeline Models     Llama 3.2 3B (Pass 1)    Proposal discovery       │
│  (Local Quantized)   Phi-3.5-mini (Pass 3)    Objection extraction     │
│                      Qwen 2.5-Coder 7B        Spans, Anchors, Watchdog │
│  Inference Runtime   Ollama / llama.cpp       Dynamic model swap,      │
│                                               weights estimate only    │
│  CLI & UX            Typer + Rich             Target scorecard outputs │
│  Agent Bridge        MCP Protocol + FastMCP   Seamless Switch hook     │
│  CI / Testing        Pytest + GitHub Actions  Automated matrix checks  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Language Decision: Python 3.11 Minimum (3.12 Recommended)

**Python 3.11 is the supported minimum**, consistent with the committed `pyproject.toml` and CI. Python 3.12 is recommended for closer parity with the inspected Switch runtime. Raising the minimum would require coordinated packaging and CI changes in a separate PR.

1. **Native Parity with Switch:**
   Inspection of upstream [`sandbox-quantum/switch` (v0.31.0 at commit `7e57cd433a922bc91a00150c106e6ef1f15b1057`)](https://github.com/sandbox-quantum/switch) demonstrates that `switch-core` is built natively on **Python 3.12, FastAPI, Pydantic v2, SQLAlchemy Async, and MCP**. Choosing Python ensures zero translation overhead and allows Biscuit to interface directly with Switch's agent session activity endpoints (`/agent-sessions/{session}/approvals`).
2. **The LLM Latency Bottleneck:**
   **Unmeasured design hypothesis:** Model inference may dominate retrieval and Python orchestration time. The previously quoted fixed 1.5–3 s inference, 5–15 ms vector retrieval, <2 ms glue time, and >98% model-time share have no reproducible traces. Benchmark stage-specific p50/p95 latency and tokens/s with a pinned model hash, quantization, context length, host CPU/GPU, corpus/index size, concurrency, warm/cold state and representative messages before making quantitative claims.
3. **AST & CST Ecosystem Separation:**
   Python provides native, uncompiled access to both Concrete Syntax Trees (via `tree-sitter` Python bindings) and Abstract Syntax Trees (via Python standard library `ast` module). In Go, AST analysis of non-Go targets (such as CPython C code or Python grammar) requires fragile subprocess invocations or CGo wrappers.
4. **Pydantic v2 Core is Rust:**
   Data validation, serialization, and JSON decoding in Pydantic v2 are powered by `pydantic-core`, compiled directly in **Rust**. Biscuit gets near-C validation speeds with clean Pythonic ergonomics.
5. **Native Model Context Protocol (MCP) Support:**
   The open MCP specification (Anthropic / Linux Foundation) has official first-party Python SDK support via **FastMCP** (`fastmcp>=3.2.0,<4`), allowing Biscuit to serve as a high-performance MCP server.

---

## 3. Data Modeling & Storage Engine

### 3.1 SQLModel & Pydantic v2
* **Role:** Declarative data models, API schemas, and validation contracts.
* **Why Selected:** Combines SQLAlchemy 2.0 with Pydantic v2. Eliminates boilerplate by declaring both the database schema and the API input/output validation shapes in a single class definition.
* **Integrity Features:** Native composite unique constraints, foreign key declarations, and field validators.

### 3.2 PostgreSQL 16 + pgvector (Production Storage)
* **Role:** Unified relational storage and high-speed semantic vector retrieval.
* **Why Selected:**
  * Avoids running a separate vector database (e.g. Pinecone, Milvus, Qdrant) that wastes hundreds of megabytes of RAM.
  * Guarantees ACID transactional integrity across evidence spans, message versions, and decision alternatives in a single database transaction.
* **Vector Index:** **HNSW (Hierarchical Navigable Small World)** vector index on `decision_alternatives(embedding)`:
  ```sql
  CREATE INDEX idx_alternatives_embedding 
  ON decision_alternatives 
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
  ```
  HNSW is the proposed retrieval strategy, not measured sub-10 ms performance. The implementation benchmark must report recall@k and p50/p95 query latency by corpus size, concurrency, index settings and hardware.

### 3.3 SQLite Unit Tests and PostgreSQL Integration Tests
* **SQLite scope:** Portable application logic and fast deterministic unit tests only; green SQLite runs cannot establish correctness of PostgreSQL-specific data contracts.
* **PostgreSQL acceptance contract (implementation phase):** CI must run PostgreSQL 16 + pgvector, apply the schema and migrations, and test composite FKs, JSONB, HNSW creation/retrieval, hash/offset verifier triggers, append-only source versions, project-scoped rulings and sign-offs (including cross-project, expired and revoked roles), HITL callback authorization and atomic lifecycle event logging. Include success and expected-rejection cases. Existing Python 3.11/3.12 smoke CI does not exercise these database invariants.

---

## 4. Code Syntax Parsing & Lineage Tracking

To ground decisions into code and track them forward across historical refactors:

### 4.1 Tree-sitter (Concrete Syntax Tree — CST)
* **Bindings:** `tree-sitter>=0.22.0`, `tree-sitter-python`, `tree-sitter-c`.
* **Role:** Multi-language concrete syntax tree parsing.
* **Why Selected:** Preserves exact token ranges, comment placement, and whitespace across C, Python, and Go codebases without requiring a compiler toolchain.

### 4.2 Python `ast` (Abstract Syntax Tree — AST)
* **Module:** Python 3.11+ standard library `ast` (3.12 recommended).
* **Role:** Python-specific structural comparison and subtree normalization.
* **Why Selected:** Identifier masking (replacing variable names with generic tokens `$ARG_0`, `$VAR_1`) enables structural hashing that survives variable renames and cosmetic reformatting.

### 4.3 Git Lineage Traversal
* **Tool:** Native Git CLI via Python `subprocess` with zero external shell wrappers.
* **Commands:**
  * Reverse topological enumeration: `git rev-list --topo-order --reverse $C_{past}..HEAD`
  * Forward diff inspection: `git diff-tree -r -M80% -C -B -p <parent> <commit>`
  * Multi-parent merge combined diffs: `git diff-tree -c -p $M`

---

## 5. Local Model Portfolio & Quantization

To target **8 GB total system RAM**, Biscuit proposes quantized GGUF weights with sequential model loading through **Ollama / llama.cpp**. The system limit remains to be verified under a real workload:

| Model Identifier | Parameter Count | Quantization | Resident Size | Pipeline Role | Selection Rationale |
|---|---|---|---|---|---|
| **`nomic-embed-text-v1.5`** | 137M | FP16 / Q4 (artifact to be pinned) | ~275 MB (estimate) | 768-dim embeddings | Candidate retrieval; validate accuracy and memory using the chosen artifact. |
| **`llama-3.2-3b-instruct`** | 3.2B | Q4_K_M | ~2.0 GB (weights estimate) | Pass 1: Proposal Discovery | Benchmark extraction accuracy, throughput and latency on held-out threads. |
| **`phi-3.5-mini-instruct`** | 3.8B | Q4_K_M | ~2.2 GB (weights estimate) | Pass 3: Argument Mining | Evaluate stance and ruling accuracy; advertised maximum context is not a deployable RAM budget. |
| **`qwen2.5-coder-7b-instruct`** | 7.6B | Q4_K_M | ~4.4 GB (weights estimate) | Pass 4: Synthesis & Watchdog | Candidate MADR/code/offset extraction; deterministic exact verification gates citations. |

---

## 6. Full-Stack Memory Budget Profile

**Unmeasured planning profile:** Load one primary model at a time during ingestion; online embedding+Qwen residency is conditional on full-host profiling, not guaranteed.

```
Full-Stack Memory Profile (Peak Execution Budget):
┌─────────────────────────────────────────────────────────────┬───────────┐
│ Component                                                   │ RAM Usage │
├─────────────────────────────────────────────────────────────┼───────────┤
│ OS Overhead, Host Daemons, & System Disk Cache              │ ~1.50 GB  │
│ PostgreSQL 16 + pgvector (Docker container)                 │ ~0.35 GB  │
│ Python 3.11+ runtime, FastAPI, async services (estimate)       │ ~0.25 GB  │
│ Shared Embedding Model: nomic-embed-text-v1.5 (Q4 resident) │ ~0.28 GB  │
│ Peak Quantized Model: Qwen 2.5-Coder-7B-Instruct (Q4_K_M)  │ ~4.40 GB  │
│ KV cache and context/runtime overhead (placeholder only)      │ ~0.30 GB  │
├─────────────────────────────────────────────────────────────┼───────────┤
│ PLANNED SYSTEM-MEMORY TOTAL (unmeasured)                       │ ~7.08 GB  │
│ NOMINAL HEADROOM (not actual free RAM)                          │ ~0.92 GB  │
└─────────────────────────────────────────────────────────────┴───────────┘
```

* **Arithmetic only:** The planned components sum to ~7.08 GB (`1.50 + 0.35 + 0.25 + 0.28 + 4.40 + 0.30`), leaving *nominal* 0.92 GB below 8.00 GB. The unverified 0.30 GB KV/cache allowance may be exceeded; this is not measured headroom.
* **Sequential offloading:** Request unload using Ollama `keep_alive: 0` between passes, then verify weights/runtime buffers are actually released before loading the next model.

### Required resource and latency validation

- **Initial deployment profile (unmeasured):** Single worker, one concurrent inference request, Q4_K_M for the primary models, embedding artifact/precision pinned, 2,048-token Qwen context cap and batch <=128 where supported. Profile on an 8 GB CPU-only host first; GPU offload requires separately measured host-RAM and VRAM budgets. Do not assume advertised maximum context length fits.
- **System memory evidence:** Measure peak complete-host used/available RAM, per-service RSS/PSS, cache/KV allocations, model-load overlap, PostgreSQL/containers, swapping and OOMs. Report concurrency, tokens processed, and end-to-end latency (p50/p95). Do not label an OS-inclusive sum as process RSS.
- **Reproducibility:** Pin model file hashes, inference runtime/version, context length, batch size, offload/keep-alive behavior, worker count, OS/hardware, database settings and input-workload sizes; attach machine-readable logs and a run manifest.
- **Acceptance:** The complete target workload must finish with measured peak host memory <=8 GB and no OOM or swap dependence. If it fails, reduce context/concurrency or change models and re-run; treat the ~7.08 GB estimate as a hypothesis, never proof.

---

## 7. Integration & Agent Protocols

### 7.1 FastMCP Protocol SDK
* **Package:** `fastmcp>=3.2.0,<4`
* **Protocol Version:** Model Context Protocol (MCP) 2024-11-05 standard.
* **Role:** Exposes Biscuit's decision engine as standard MCP tools (`query_decisions`, `check_pr_diff`, `draft_living_decision`).

### 7.2 Switch Collaboration Platform Bridge
* **Upstream Project:** [`sandbox-quantum/switch` (v0.31.0)](https://github.com/sandbox-quantum/switch)
* **Pinned Commit:** `7e57cd433a922bc91a00150c106e6ef1f15b1057`
* **Role:** Omnichannel gateway connecting to CNCF Slack, Discord, and Mattermost without bespoke bot code.

---

## 8. CLI & Developer Experience

### 8.1 Typer & Rich
* **Packages:** `typer>=0.12.0`, `rich>=13.7.0`
* **Role:** Terminal CLI framework and formatting.
* **Features:**
  * Auto-generated shell completion (`biscuit --install-completion`).
  * Rich terminal scorecards, benchmark execution tables, and colored diff outputs.
* **Proposed Command Surface (Stream 5, per issue #21):**
  * `biscuit evaluate --benchmark pep-505 [--verbose] [--strict-offsets] [--output json]` — runs the offline benchmark and renders the Rich scorecard (the module-level runner interface from `EVALUATION_AND_BENCHMARKS.md` §5, `python -m biscuit.evaluate`, is exposed as this subcommand).
  * `biscuit ingest --platform discourse|mbox|github --url <url>` — ingests a thread into the local database.
  * `biscuit decisions list --status REJECTED` — interactive table of recovered decisions.
  * `biscuit export --output docs/decisions/` — exports MADR 3.0 markdown files to disk.

---

## 9. Dependency Specifications (`pyproject.toml` Roadmap)

**Proposed, not installed:** The following production and development dependencies are planned for an implementation PR. The committed `pyproject.toml` currently remains minimal and supports Python >=3.11.

```toml
[project]
name = "biscuit-intel"
version = "0.1.0"
description = "Open Source Decision Intelligence and Knowledge Recovery"
readme = "HACKATHON.md"
requires-python = ">=3.11"

dependencies = [
    # Core Data Modeling & Validation
    "pydantic>=2.7.0,<3.0.0",
    "sqlmodel>=0.0.18,<0.1.0",
    "sqlalchemy[asyncio]>=2.0.30,<3.0.0",
    "alembic>=1.13.0,<2.0.0",
    "psycopg[binary]>=3.1.18,<4.0.0",
    "pgvector>=0.3.0,<0.4.0",

    # Model Context Protocol (MCP) & FastMCP
    "fastmcp>=3.2.0,<4.0.0",

    # Syntax & AST Parsing
    "tree-sitter>=0.22.0,<0.23.0",
    "tree-sitter-python>=0.21.0,<0.22.0",
    "tree-sitter-c>=0.21.0,<0.22.0",

    # CLI & Terminal UI
    "typer>=0.12.0,<0.13.0",
    "rich>=13.7.0,<14.0.0",

    # HTTP & Utilities
    "httpx>=0.27.0,<0.28.0",
    "python-dateutil>=2.9.0",
    "pyyaml>=6.0.1,<7.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0,<9.0.0",
    "pytest-asyncio>=0.23.0,<0.24.0",
    "pytest-cov>=5.0.0,<6.0.0",
    "ruff>=0.4.0,<0.5.0",
]
```

---

## 10. Related Specifications & Architecture Maps
* [`docs/TWO_PILLAR_ARCHITECTURE.md`](./TWO_PILLAR_ARCHITECTURE.md): Master Two-Way Architecture Blueprint (The Past + Day 1+).
* [`docs/LIVING_DECISION_INTELLIGENCE.md`](./LIVING_DECISION_INTELLIGENCE.md): Switch integration, Slack/Discord HITL cards, and live capture.
* [`docs/STORAGE_AND_RETRIEVAL.md`](./STORAGE_AND_RETRIEVAL.md): Canonical PostgreSQL schema, composite FKs, and triggers.
* [`docs/CODE_GROUNDING_AND_AST.md`](./CODE_GROUNDING_AND_AST.md): AST symbol grounding, forward DAG traversal, and warning gates.
* [`docs/EVALUATION_AND_BENCHMARKS.md`](./EVALUATION_AND_BENCHMARKS.md): Metric suites and benchmark execution harness.
