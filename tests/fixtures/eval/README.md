# Offline evaluation fixtures and reproducibility

**These three PEP-themed datasets are original synthetic tests, not historical archives.**
PEP 505 has 2 fabricated posts, PEP 572 has 4, and PEP 557 has 3. No empirical
model performance, 8 GB runtime compliance, or licensed historical source coverage
is implied by a passing replay.

## Included inputs and provenance

- Each benchmark includes `raw_thread.json`, scorer-only `ground_truth.json`,
  recorded `simulated_predictions.json`, and `manifest.json` with canonicalization
  version, expected/actual message count, thread SHA-256, license/rights status,
  retrieval-date field, and independent-review status.
- The manifest identifies origin `ORIGINAL_SYNTHETIC_TEST_DATA`, license metadata
  as not yet applicable to any historical source, and reviewer
  `PENDING_INDEPENDENT_REVIEW`.
- SHA-256 covers each original UTF-8 `raw_text` and the sorted-key compact
  canonical JSON thread (messages sorted by timestamp and ID).
- There are 20 separately held-out **synthetic PRs**: 5 known violations,
  10 benign, 5 ambiguous. Scorer-only files `pr_labels.json`,
  `pr_expected_alternatives.json` and `pr_authorization_oracle.json`
  stay outside the predictor's input.
- PEP 572's Python walrus operator `:=` was accepted, so the relevant PR
  fixture is **benign**. The rejected syntactic *alternatives* are distinct.

## Run locally, without a network/model

```shell
pip install -e .
biscuit evaluate --all --format json
python -m biscuit.evaluate --all --output json
python -m biscuit.evaluate --benchmark pep-505 --strict-offsets --output json
biscuit evaluate --no-simulate --benchmark pep-505
```

The default CLI **recorded synthetic replay** scores a checked-in,
hand-authored mock response, not an actual model run. It is a test of fixture
loading, numerical metrics, PR authorization matching and scorecard generation.
Use `--no-simulate` to get `NOT_MEASURED` when no real predictions exist.
The Python API `run_benchmark()` defaults to `NOT_MEASURED`; request
`recorded_replay=True` explicitly for a mock. Supplying real results through
`--predictions` overrides the default CLI replay.

Causal rationale values use a **deterministic hashed-token lexical proxy** in
synthetic fixtures; this is NOT semantic similarity from the specified nomic
embedding model, and therefore cannot demonstrate real-world CRA performance.

To graduate the historical benchmarks, capture complete legally usable
source archives, preserve author roles and edits, independently review GT
rulings, assign real historical commits, pin model/context versions, and
measure host RAM with Ollama/PostgreSQL running. Never put `ground_truth.json`
or official PEP final texts into extraction/model context.
