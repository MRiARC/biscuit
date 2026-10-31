"""Offline code-grounding APIs. No database, model, or network dependencies."""

from .cst_parser import ParseError, ParserUnavailable, extract_symbols
from .lineage import (
    GitLineageError, enumerate_forward_dag, forward_commits,
    trace_forward_lineage, trace_lineage,
)
from .merge_resolver import resolve_merge_parents
from .normalizer import (
    NORMALIZATION_VERSION,
    ast_structural_fingerprint,
    fingerprint_python_ast,
    normalize_python_ast,
    normalized_python_ast,
)
from .types import AnchorState, LineageEvent, LineageResult, MergeResolution, Symbol

__all__ = [
    "GitLineageError", "forward_commits", "enumerate_forward_dag",
    "trace_lineage", "trace_forward_lineage", "resolve_merge_parents",
    "AnchorState", "LineageEvent", "LineageResult", "MergeResolution", "Symbol",
    "NORMALIZATION_VERSION", "ParseError", "ParserUnavailable", "extract_symbols",
    "ast_structural_fingerprint", "fingerprint_python_ast", "normalize_python_ast",
    "normalized_python_ast",
]