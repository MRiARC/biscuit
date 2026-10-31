# Offline evaluation fixtures

This directory contains **synthetic unit-test data only**, not the historical
234-message PEP 505 archive, withheld official PEP texts, or empirical model runs.

- The raw thread is checksummed using canonical UTF-8 JSON (sorted keys, compact
  separators, messages sorted by created_at and message_id). Message hashes
  cover exact UTF-8 raw_text bytes.
- The scorer alone reads ground_truth.json and pr_labels.json. Prediction
  adapters receive only raw discussion and case diffs, never expected labels.
- Exactly 20 synthetic PR cases: 5 violations, 10 benign, 5 ambiguous.
- Rejected experimental assignment-expression syntaxes are distinguished
  from Python's **accepted** := operator (benign case pr-09).
- No auto-populated predictions exist. Supply model/engine outputs explicitly;
  a scorecard without predictions says NOT_MEASURED.

Real historic fixtures must be collected separately with rights review, complete
post counts, a pinned manifest, and a fresh canonical checksum. Do not treat this
synthetic smoke fixture as the full benchmark.
