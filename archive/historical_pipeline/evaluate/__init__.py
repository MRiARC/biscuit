"""Offline, auditable benchmark scoring for Biscuit."""

from .embeddings import LocalOllamaEmbedder, OpenAIEmbedder, get_embedder
from .metrics import (
    ast_grounding_precision,
    causal_rationale_alignment,
    exact_evidence_match,
    lifecycle_accuracy,
    pr_warning_metrics,
    proposal_recovery,
)
from .runner import run_benchmark

__all__ = [
    "LocalOllamaEmbedder",
    "OpenAIEmbedder",
    "ast_grounding_precision",
    "causal_rationale_alignment",
    "exact_evidence_match",
    "get_embedder",
    "lifecycle_accuracy",
    "pr_warning_metrics",
    "proposal_recovery",
    "run_benchmark",
]
