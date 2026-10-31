"""Regression: exact quote alone never proves a stale source version."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

from biscuit.evaluate.metrics import exact_evidence_match
from biscuit.evaluate.stream3_adapter import stream3_predictor


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


def test_stream3_adapter_cannot_fabricate_missing_hash_or_verified_status() -> None:
    """The extraction result must carry its own exact, verified source evidence."""
    class FakePipeline:
        def run(self, **kwargs):
            return SimpleNamespace(
                run_status="SUCCESS", run_id="run",
                proposals=[], madr=SimpleNamespace(considered_alternatives=[]),
                evidence_spans=[SimpleNamespace(
                    source_message_version_id="persisted-version",
                    start_char=0, end_char=7, exact_quote="ruling!",
                    content_hash="", verification_status="UNVERIFIED",
                )],
            )

    source = {
        "thread_id": "t1",
        "messages": [{
            "message_id": "msg", "author_handle": "a",
            "created_at": "2026-01-01", "raw_text": "ruling!",
            "source_message_version_id": "persisted-version",
        }],
        "pr_cases": [],
    }
    outcome = stream3_predictor(FakePipeline())(source)
    span = outcome["evidence"][0]
    assert span["sha256"] == ""
    assert span["verification_status"] == "UNVERIFIED"
    assert exact_evidence_match([span], {"msg": "ruling!"})["emr"] == 0.0
