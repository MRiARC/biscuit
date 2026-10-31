"""Regression: exact quote alone never proves a stale source version."""
from __future__ import annotations

import hashlib

from biscuit.evaluate.metrics import exact_evidence_match


def test_stale_and_unverified_span_cannot_pass_exact_evidence_score() -> None:
    quote = "A unicode ☺ ruling"
    evidence = {
        "message_id": "m1",
        "start_char": 0,
        "end_char": len(quote),
        "quote": quote,
        "sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
    }
    for status in ("STALE_EDITED", "UNVERIFIED", "UNAVAILABLE"):
        invalid = {**evidence, "verification_status": status}
        score = exact_evidence_match([invalid], {"m1": quote})
        assert score["emr"] == 0.0
        assert score["invalid_citation_rate"] == 1.0
    verified = {**evidence, "verification_status": "VERIFIED_EXACT"}
    assert exact_evidence_match([verified], {"m1": quote})["emr"] == 1.0
