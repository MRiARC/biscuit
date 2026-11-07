# Archived Historical & Offline Deliberation Pipeline

> **Status:** Quarantined / Parked for Phase 2  
> **Active Focus:** Live stream monitoring and decision enforcement via Switch Gateway (`biscuit.switch` + `biscuit.watchdog`).

---

## 1. Purpose of This Archive

This directory preserves the batch extraction, thread ingestion, character-offset provenance verification, and offline evaluation benchmark modules. 

In Phase 1, Biscuit is unified with the **Switch Gateway** to act as a **live directional compass**:
- Switch actively listens to live discussions in real time (Slack, Discord, Teams, GitHub comments).
- When maintainers reach consensus, Switch dispatches decisions to Biscuit via FastMCP (`draft_living_decision`).
- When contributors open PRs, Switch queries Biscuit via FastMCP (`check_pr_diff`) to evaluate the 4 Invariants and provide directional cards.

Because neither Switch nor Biscuit originally had live API crawlers to backfill historical discussions from months or years ago, the batch/offline past-conversation pipeline is parked here without data loss.

---

## 2. Archived Modules & File Contents

### `extract/` (The 4-Pass Deliberation Pipeline)
* **`pipeline.py`**: 4-pass extraction orchestrator (`Pass 1 Discovery -> Pass 2 Slicing -> Pass 3 Arguments -> Pass 4 Synthesis`).
* **`pass1_discovery.py`**: LLM proposal discovery over thread messages.
* **`pass2_slicing.py`**: Thread slicing into deliberation sub-dialogues.
* **`pass3_arguments.py`**: Classifying pros, cons, objections, and maintainer consensus.
* **`pass4_synthesis.py`**: Synthesizing MADR 3.0 Architectural Decision Records.
* **`client.py`**: Multi-provider LLM client (`LocalLLMClient`, `LLMClient`) supporting Groq, OpenAI, and Ollama.
* **`prompts.py`**: Prompt templates for historical deliberation mining.
* **`audit.py`**: Audit digest writer for extraction passes.

### `evaluate/` (Offline Prediction Benchmark)
* **`runner.py`**: Benchmark runner evaluating prediction accuracy on offline datasets.
* **`metrics.py`**: Precision, Recall, F1, and MCC score calculations.
* **`fixtures.py`**: Dataset loader for synthetic thread benchmarks.
* **`embeddings.py`**: Semantic embedding helpers for alternative matching.

### `ingest/` (Batch Thread Ingestion)
* **`github.py`**: `GitHubAdapter` parsing static JSON dumps of GitHub review comments.
* **`service.py`**: Relational ingestion service storing threads and messages.
* **`cleaner.py`**: Quoted reply and email header stripper.
* **`hasher.py`**: Canonical message SHA-256 byte hasher.

### `provenance/` (Byte-Exact Evidence Verifier)
* **`matcher.py`**: Character start/end offset locator in message bodies.
* **`verifier.py`**: Exact byte matcher preventing quote hallucination.

### `cli/` (Legacy Commands & Scorecard)
* **`scorecard.py`**: Rich console table formatter for offline benchmark runs.

### `tests/`
Preserves all unit tests associated with the above modules (`test_extract.py`, `test_evaluate.py`, `test_ingest.py`, `test_provenance.py`, `test_review_blockers.py`, etc.).

---

## 3. Platform-by-Platform Revival Roadmap (Phase 2)

When ready to implement historical decision recovery:

1. **GitHub Platform Revival:**
   * Extend `ingest/github.py` with an active REST API fetcher (`gh api` or `httpx` against `https://api.github.com/repos/{owner}/{repo}/issues/{id}`) to download real deliberation threads directly into `RawThreadPayload`.
   * Re-wire `biscuit ingest --platform github` to extract issues automatically.

2. **Slack Platform Revival:**
   * Build a dedicated channel archiver using Slack's `conversations.history` pagination API with user consent and OAuth scopes.

3. **Discourse Platform Revival:**
   * Provide a clean read-only Discourse API crawler for public discussion forums.
