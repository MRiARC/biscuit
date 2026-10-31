"""Candidate retrieval for the PR watchdog (signal generation only).

``CandidateRetriever`` implements Step 1 of the guardrail pipeline
(docs/CODE_GROUNDING_AND_AST.md, sections 2 and 6):

* Tier 1 — AST symbol / anchor lookup: an alternative becomes a candidate
  when the PR diff touches one of its anchors (path match, or a top-level
  symbol defined in the added fragment).
* Tier 4 — semantic diff embedding: cosine similarity of the diff text
  embedding against ``decision_alternatives.embedding``.

The provisional cosine threshold ``0.82`` is strictly a *retrieval* filter:
it adds candidates for further analysis and NEVER proves re-introduction or
authorizes a warning (docs/CODE_GROUNDING_AND_AST.md, section 6, Tier 4).
Embeddings are injected as a callable so tests run without live models; when
no embedder is configured the vector tier is skipped.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from biscuit.watchdog.types import (
    CandidateMatch,
    CodeAnchorRecord,
    DecisionStore,
    PRDiff,
)

#: Provisional candidate-retrieval threshold (retrieval signal only).
CANDIDATE_COSINE_THRESHOLD = 0.82

#: Callable that maps text to a dense embedding vector.
Embedder = Callable[[str], "tuple[float, ...] | list[float]"]


def cosine_similarity(
    a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]
) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 when undefined."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class CandidateRetriever:
    """Generates candidate alternatives for an incoming PR diff.

    Candidates are hypotheses only. Authorization is decided later by the
    4-invariant ``WarningAuthorizationGate``; a similarity score can never
    authorize a warning.
    """

    def __init__(
        self,
        store: DecisionStore,
        embedder: Embedder | None = None,
        threshold: float = CANDIDATE_COSINE_THRESHOLD,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self.threshold = threshold

    def retrieve(self, pr_diff: PRDiff) -> list[CandidateMatch]:
        """Return one ``CandidateMatch`` per candidate alternative.

        An alternative is a candidate when the diff touches one of its
        anchors (path or symbol) or when the semantic similarity of the
        added diff text reaches the provisional retrieval threshold.
        """
        diff_embedding = self._embed_diff(pr_diff)
        touched_paths = pr_diff.touched_paths
        diff_symbols = pr_diff.all_symbols

        matches: list[CandidateMatch] = []
        for alternative in self._store.all_alternatives():
            similarity: float | None = None
            if diff_embedding is not None and alternative.embedding is not None:
                similarity = cosine_similarity(diff_embedding, alternative.embedding)

            anchor_ids: list[str] = []
            matched_symbols: list[str] = []
            signals: list[str] = []
            for anchor in alternative.anchors:
                if (
                    anchor.repository_url
                    and pr_diff.repository_url
                    and anchor.repository_url != pr_diff.repository_url
                ):
                    continue
                how = self._anchor_touched(anchor, touched_paths, diff_symbols)
                if how is not None:
                    anchor_ids.append(anchor.id)
                    signals.append(f"anchor:{how}")
                    if how == "symbol":
                        matched_symbols.append(anchor.symbol_name)

            vector_hit = similarity is not None and similarity >= self.threshold
            if vector_hit:
                signals.append(f"vector:cosine>={self.threshold}")

            if anchor_ids or vector_hit:
                matches.append(
                    CandidateMatch(
                        alternative_id=alternative.id,
                        similarity=similarity,
                        matched_anchor_ids=tuple(anchor_ids),
                        matched_symbols=tuple(matched_symbols),
                        signals=tuple(signals),
                    )
                )
        return matches

    def _embed_diff(self, pr_diff: PRDiff) -> tuple[float, ...] | list[float] | None:
        if self._embedder is None:
            return None
        text = pr_diff.added_text
        if not text.strip():
            return None
        return self._embedder(text)

    @staticmethod
    def _anchor_touched(
        anchor: CodeAnchorRecord,
        touched_paths: frozenset[str],
        diff_symbols: frozenset[str],
    ) -> str | None:
        """Return how the diff touches ``anchor``: ``path``, ``symbol`` or None.

        A path match counts even for inactive (SPLIT/AMBIGUOUS/DELETED)
        anchors: retrieval stays generous; the gate enforces activeness.
        """
        if anchor.current_path in touched_paths or anchor.historical_file_path in (touched_paths):
            return "path"
        if anchor.symbol_name in diff_symbols:
            return "symbol"
        return None
