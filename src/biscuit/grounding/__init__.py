"""Offline code-grounding APIs. No database, model, or network dependencies."""

from .merge_resolver import resolve_merge_parents
from .symbols import (
    ast_structural_fingerprint,
    extract_symbols,
    fingerprint_python_ast,
)
from .types import (
    NORMALIZATION_VERSION,
    AnchorState,
    CodeSymbolAnchor,
    LineageEvent,
    LineageResult,
    MergeResolution,
    ParseError,
    ParserUnavailable,
    Symbol,
    lineage_storage_records,
)

__all__ = [
    "NORMALIZATION_VERSION",
    "AnchorState",
    "CodeSymbolAnchor",
    "LineageEvent",
    "LineageResult",
    "MergeResolution",
    "ParseError",
    "ParserUnavailable",
    "Symbol",
    "ast_structural_fingerprint",
    "extract_symbols",
    "fingerprint_python_ast",
    "lineage_storage_records",
    "resolve_merge_parents",
]
