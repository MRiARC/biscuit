"""Pure, database-independent result contracts for code grounding.

Offsets are UTF-8 *bytes*; line numbers are 1-based. Neither confidence field
is inferred from file presence, a Git rename score, or a structural hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AnchorState(str, Enum):
    HISTORICAL = "HISTORICAL"
    CURRENT = "CURRENT"
    MIGRATED = "MIGRATED"
    SPLIT = "SPLIT"
    MERGED = "MERGED"
    DELETED = "DELETED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class Symbol:
    language: str
    symbol_type: str
    qualified_name: str
    file_path: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    signature_hash: str
    structural_fingerprint: str | None = None
    parser_backend: str = ""
    parser_version: str = ""
    grammar_version: str = ""
    normalization_version: str = "python-ast-mask-v1"

    @property
    def symbol_name(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class LineageEvent:
    transition_commit_sha: str
    compared_parent_sha: str
    parent_index: int
    old_path: str | None
    new_path: str | None
    transition_type: str
    state: AnchorState
    confidence_score: float | None = None
    mapping_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeResolution:
    state: AnchorState
    paths: tuple[str, ...]
    reason: str
    confidence_score: float | None = None


@dataclass(frozen=True)
class LineageResult:
    historical_commit_sha: str
    head_commit_sha: str
    historical_path: str
    resolved_path: str | None
    anchor_state: AnchorState
    mapping_confidence: float | None = None
    candidates: tuple[str, ...] = ()
    events: tuple[LineageEvent, ...] = ()

    @property
    def safe_for_automatic_migration(self) -> bool:
        return self.anchor_state in (AnchorState.CURRENT, AnchorState.MIGRATED)