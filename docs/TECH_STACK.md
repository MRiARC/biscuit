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
- **Hardware Memory Boundary:** $\le \text{8 GB RAM}$ total memory footprint for the entire application stack.
- **Offline & Local Execution Scope:** Local model inference, vector search, AST parsing, and evaluation benchmarks execute completely offline without internet access. Live chat interception via Switch and GitHub PR webhook sync require network connectivity.

```
┌────────────────────────────────────────────────────────────────────────┐
│                          BISCUIT TECH STACK                            │
├────────────────────────────────────────────────────────────────────────┤
│  Layer               Technology Choice        Rationale                │
├────────────────────────────────────────────────────────────────────────┤
│  Core Language       Python 3.12+             Native AST, fast PyPI,   │
│                                               Tree-sitter bindings     │
│  Data Modeling       Pydantic v2 + SQLModel   Rust-backed validation,  │
│                                               strict schema contracts  │
│  Relational + Vector PostgreSQL 16 + pgvector Scalable hybrid search,  │
│                      (Docker container)       HNSW indexing            │
│  Test Fallback DB    SQLite (in-memory)       Zero-config CI matrix    │
│  Syntax Parsing      Tree-sitter (CST) +      Multi-language CST shape │
│                      Python ast (AST)         concrete syntax trees    │
│  Embedding Model     nomic-embed-text-v1.5    Matryoshka 768d, ~275MB  │
│  Pipeline Models     Llama 3.2 3B (Pass 1)    Proposal discovery       │
│  (Local Quantized)   Phi-3.5-mini (Pass 3)    Objection extraction     │
│                      Qwen 2.5-Coder 7B        Spans, Anchors, Watchdog │
│  Inference Runtime   Ollama / llama.cpp       Dynamic model swap,      │
│                                               peak RAM < 4.7 GB (model)│
│  CLI & UX            Typer + Rich             Target scorecard outputs │
│  Agent Bridge        MCP Protocol + FastMCP   Seamless Switch hook     │
│  CI / Testing        Pytest + GitHub Actions  Automated matrix checks  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Language Decision: Why Python 3.12+ (vs. Golang & Rust)

While Golang and Rust offer superior raw CPU execution throughput for compiled microservices, **Python 3.12+ was selected as the optimal engineering language for Biscuit**:

1. **Native Parity with Switch:**
   Inspection of upstream [`sandbox-quantum/switch` (v0.31.0 at commit `7e57cd433a922bc91a00150c106e6ef1f15b1057`)](https://github.com/sandbox-quantum/switch) demonstrates that `switch-core` is built natively on **Python 3.12, FastAPI, Pydantic v2, SQLAlchemy Async, and MCP**. Choosing Python ensures zero translation overhead and allows Biscuit to interface directly with Switch's agent session activity endpoints (`/agent-sessions/{session}/approvals`).
2. **The LLM Latency Bottleneck:**
   A local 7B-parameter model inference call on quantized open-source weights takes **$1,500 \text{ ms} - 3,000 \text{ ms}$**. Vector similarity retrieval in pgvector takes **$5 \text{ ms} - 15 \text{ ms}$**. Python glue logic executes in **$< 2 \text{ ms}$**. Because $>98\%$ of wall-clock time is spent in model tensor matrix operations, Go or Rust's microsecond advantage is completely imperceptible.
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
  Provides sub-10ms similarity queries under cosine distance (`vector_cosine_ops`).

### 3.3 SQLite (In-Memory Fallback for Test Matrix)
* **Role:** Zero-configuration local testing and GitHub Actions CI matrix.
* **Why Selected:** Allows `pytest` test suites and GitHub Actions CI workflows to execute in $< 20\text{ seconds}$ without spinning up heavy container infrastructure.

---

## 4. Code Syntax Parsing & Lineage Tracking

To ground decisions into code and track them forward across historical refactors:

### 4.1 Tree-sitter (Concrete Syntax Tree — CST)
* **Bindings:** `tree-sitter>=0.22.0`, `tree-sitter-python`, `tree-sitter-c`.
* **Role:** Multi-language concrete syntax tree parsing.
* **Why Selected:** Preserves exact token ranges, comment placement, and whitespace across C, Python, and Go codebases without requiring a compiler toolchain.

### 4.2 Python `ast` (Abstract Syntax Tree — AST)
* **Module:** Python 3.12 standard library `ast`.
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

To strictly respect the **$\le 8$ GB RAM budget**, Biscuit uses quantized GGUF weights via **Ollama / llama.cpp**:

| Model Identifier | Parameter Count | Quantization | Resident Size | Pipeline Role | Selection Rationale |
|---|---|---|---|---|---|
| **`nomic-embed-text-v1.5`** | 137M | FP16 / Q4 | ~275 MB | 768-dim code hunk & proposal embeddings | Supports Matryoshka dimensionality; SOTA code and prose retrieval. Remains resident. |
| **`llama-3.2-3b-instruct`** | 3.2B | Q4_K_M | ~2.0 GB | Pass 1: Proposal Discovery | Ultra-high throughput (>80 tok/s), low latency (<1s per chunk), scans 200+ post threads rapidly. |
| **`phi-3.5-mini-instruct`** | 3.8B | Q4_K_M | ~2.2 GB | Pass 3: Deliberation Argument Mining | 128k context window, trained specifically on dense multi-turn reasoning; separates objections from consensus. |
| **`qwen2.5-coder-7b-instruct`** | 7.6B | Q4_K_M | ~4.4 GB | Pass 4: MADR Synthesis & PR Watchdog | SOTA open-weights code intelligence, strict JSON/MADR schema compliance, and flawless offset localization. |

---

## 6. Full-Stack Memory Budget Profile

Biscuit employs sequential time-sliced model execution during historical batch ingestion, loading models one at a time. In online watchdog mode, only `nomic-embed` and `qwen2.5-coder` are resident:

```
Full-Stack Memory Profile (Peak Execution Budget):
┌─────────────────────────────────────────────────────────────┬───────────┐
│ Component                                                   │ RAM Usage │
├─────────────────────────────────────────────────────────────┼───────────┤
│ OS Overhead, Host Daemons, & System Disk Cache              │ ~1.50 GB  │
│ PostgreSQL 16 + pgvector (Docker container)                 │ ~0.35 GB  │
│ Python 3.12 Runtime, FastAPI, & Async Event Services        │ ~0.25 GB  │
│ Shared Embedding Model: nomic-embed-text-v1.5 (Q4 resident) │ ~0.28 GB  │
│ Peak Quantized Model: Qwen 2.5-Coder-7B-Instruct (Q4_K_M)  │ ~4.40 GB  │
│ KV Cache Budget & Context Overhead                          │ ~0.30 GB  │
├─────────────────────────────────────────────────────────────┼───────────┤
│ ESTIMATED SYSTEM-MEMORY TOTAL                               │ ~7.08 GB  │
│ ARITHMETIC HEADROOM TO 8.00 GB BOUNDARY                     │ ~0.92 GB  │
└─────────────────────────────────────────────────────────────┴───────────┘
```

* **Calculation:** $1.50 + 0.35 + 0.25 + 0.28 + 4.40 + 0.30 = 7.08\text{ GB}$, leaving $\approx 0.92\text{ GB}$ headroom under the 8.0 GB hard limit.
* **Sequential Offloading:** During batch extraction, `llama-3.2-3b` (~2.0 GB) and `phi-3.5-mini` (~2.2 GB) run in earlier passes and are explicitly offloaded via Ollama API (`keep_alive: 0`) before `qwen2.5-coder-7b` is loaded.

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

---

## 9. Dependency Specifications (`pyproject.toml` Roadmap)

The complete production and development dependencies slated for implementation starting in PR #8:

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
