"""Semantic scoring must never send withheld ground-truth text to the model."""
from __future__ import annotations

import json
import shutil

import pytest

from biscuit.evaluate.embeddings import LocalOllamaEmbedder
from biscuit.evaluate.fixtures import DEFAULT_FIXTURE_ROOT
from biscuit.evaluate.runner import run_benchmark


class FakeModel:
    model = "unit-test-embedder-v1"

    def __init__(self) -> None:
        self.called: list[str] = []

    def __call__(self, value: str) -> list[float]:
        self.called.append(value)
        return [1.0, 0.0]


def _cra(outcome):
    return next(x for x in outcome.metrics if x.name == "Causal rationale alignment")


def test_semantic_mode_abstains_without_independently_pinned_gt_vectors() -> None:
    model = FakeModel()
    scored = run_benchmark("pep-505", recorded_replay=True, semantic_embedder=model)
    assert _cra(scored).status == "NOT_MEASURED"
    assert scored.verdict == "INCOMPLETE"
    assert model.called == []
    assert scored.counts["rationale"]["embedding_method"] == (
        "NOT_MEASURED_GT_MODEL_VECTORS_MISSING"
    )


def test_semantic_mode_scores_only_precomputed_matching_model_vectors(tmp_path) -> None:
    root = tmp_path / "fixtures"
    shutil.copytree(DEFAULT_FIXTURE_ROOT, root)
    oracle_path = root / "pep-505" / "ground_truth.json"
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    for proposal in oracle["proposals"]:
        proposal["rationale_embedding_model"] = "unit-test-embedder-v1"
        proposal["rationale_embedding"] = [1.0, 0.0]
    oracle_path.write_text(json.dumps(oracle), encoding="utf-8")

    model = FakeModel()
    result = run_benchmark(
        "pep-505", fixtures_root=root,
        recorded_replay=True, semantic_embedder=model,
    )
    assert result.verdict == "PASSED"
    assert _cra(result).status == "PASSED"
    assert len(model.called) == 2
    assert result.counts["rationale"]["embedding_model"] == "unit-test-embedder-v1"


def test_embedder_rejects_external_hosts_and_bad_model_replies(monkeypatch) -> None:
    from biscuit.evaluate import embeddings

    with pytest.raises(ValueError, match="loopback"):
        LocalOllamaEmbedder(base_url="https://third-party-llm.example.org")
    with pytest.raises(ValueError, match="nonempty"):
        LocalOllamaEmbedder()("")

    class MockResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size: int):
            return b'{"embeddings": [[0.4, 0.6]]}'

    seen = []

    def fake_urlopen(request, *, timeout):
        seen.append((request.full_url, json.loads(request.data)))
        return MockResponse()

    monkeypatch.setattr(embeddings, "urlopen", fake_urlopen)
    model = LocalOllamaEmbedder()
    assert model("Predicted rationale") == [0.4, 0.6]
    assert seen[0][0] == "http://127.0.0.1:11434/api/embed"
    assert seen[0][1]["keep_alive"] == 0
    assert seen[0][1]["input"] == "Predicted rationale"
