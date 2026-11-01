"""Pure, database-independent result contracts for code grounding.

Offsets are UTF-8 *bytes*; line numbers are 1-based. Neither confidence field
is inferred from file presence, a Git rename score, or a structural hash.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .normalizer import NORMALIZATION_VERSION


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
    normalization_version: str = NORMALIZATION_VERSION

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
    # ``code_anchor_id`` is attached when Stream 1 persists this event.
    code_anchor_id: uuid.UUID | None = None
    target_commit_sha: str | None = None
    traversal_strategy: str = "FORWARD_COMMIT_DAG_DIFF_M"
    recorded_at: datetime.datetime | None = None


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
    verified_parser: bool = False
    candidates: tuple[str, ...] = ()
    events: tuple[LineageEvent, ...] = ()

    @property
    def safe_for_automatic_migration(self) -> bool:
        return (self.verified_parser and self.resolved_path is not None
                and self.anchor_state in (AnchorState.CURRENT, AnchorState.MIGRATED))


@dataclass(frozen=True)
class CodeSymbolAnchor:
    """DB-independent snapshot matching Section 6 ``code_anchors`` columns.

    This is the Stream-2 producer contract, not a duplicate SQLModel model.
    Stream 1 owns persistence; the decision/workdog streams supply ``alternative_id``.
    Neither a parser fallback nor an exact hash authorizes an outcome.
    """

    id: uuid.UUID
    alternative_id: uuid.UUID
    repository_url: str
    language: str
    symbol_type: str
    qualified_symbol_name: str
    source_locator_type: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    parser_name: str
    parser_version: str
    grammar_version: str
    fingerprint_algorithm: str
    ast_normalization_version: str
    ast_structural_fingerprint: str | None
    signature_hash: str | None
    historical_file_path: str
    historical_commit_sha: str
    anchor_state: AnchorState = AnchorState.HISTORICAL
    head_commit_sha: str | None = None
    mapping_confidence: float | None = None
    verified_parser: bool = False

    @classmethod
    def from_symbol(
        cls, symbol: Symbol, *, alternative_id: uuid.UUID | str,
        repository_url: str, historical_commit_sha: str,
        anchor_id: uuid.UUID | str | None = None,
    ) -> CodeSymbolAnchor:
        """Create a typed alternative-specific record; caller supplies authority/link.

        ``symbol_type`` uses the *lowercase values* of the Postgres enum and
        Stream-1 ``CodeSymbolType`` (e.g. ``FUNCTION.value == 'function'``).
        """
        if not repository_url.startswith(("https://", "http://")):
            raise ValueError("repository_url must be a URL")
        if not historical_commit_sha or not symbol.file_path:
            raise ValueError("Historical commit and file path are required")
        allowed = {"function", "method", "class", "grammar_rule", "ast_node", "interface", "module"}
        if symbol.symbol_type not in allowed:
            raise ValueError(f"Unknown canonical symbol type: {symbol.symbol_type}")
        if symbol.start_byte < 0 or symbol.end_byte <= symbol.start_byte:
            raise ValueError("Invalid byte locator")
        if symbol.start_line <= 0 or symbol.end_line < symbol.start_line:
            raise ValueError("Invalid line locator")
        if not all((
            symbol.parser_backend.strip(),
            symbol.parser_version.strip(),
            symbol.grammar_version.strip(),
            symbol.normalization_version.strip(),
        )):
            raise ValueError("Parser, grammar and normalization provenance must be nonempty")
        return cls(
            id=uuid.UUID(str(anchor_id)) if anchor_id is not None else uuid.uuid4(),
            alternative_id=uuid.UUID(str(alternative_id)),
            repository_url=repository_url,
            language=symbol.language,
            symbol_type=symbol.symbol_type,
            qualified_symbol_name=symbol.qualified_name,
            source_locator_type="AST_NODE_QUERY" if symbol.parser_backend == "tree-sitter" else "FILE_BYTE_RANGE",
            start_line=symbol.start_line, end_line=symbol.end_line,
            start_byte=symbol.start_byte, end_byte=symbol.end_byte,
            parser_name=symbol.parser_backend,
            parser_version=symbol.parser_version,
            grammar_version=symbol.grammar_version,
            fingerprint_algorithm="sha256",
            ast_normalization_version=symbol.normalization_version,
            ast_structural_fingerprint=symbol.structural_fingerprint,
            signature_hash=symbol.signature_hash,
            historical_file_path=symbol.file_path,
            historical_commit_sha=historical_commit_sha,
            verified_parser=symbol.parser_backend == "tree-sitter",
        )

    def to_storage_record(self) -> dict[str, Any]:
        """Exact CodeAnchor(SQLModel) field names, excluding internal verification flag."""
        from dataclasses import fields

        return {
            field.name: (getattr(self, field.name).value if field.name == "anchor_state"
                         else getattr(self, field.name))
            for field in fields(self)
            if field.name != "verified_parser"
        }


def lineage_storage_records(
    anchor: CodeSymbolAnchor, result: LineageResult,
    *, recorded_at: datetime.datetime | None = None,
) -> tuple[dict[str, Any], ...]:
    """Bind transient lineage events to the persistence schema and one anchor ID.

    This produces insert-ready rows; Stream 1 owns the actual DB transaction.
    Migration eligibility is explicitly withheld when parser verification fails.
    """
    if result.historical_commit_sha != anchor.historical_commit_sha:
        raise ValueError("Lineage result is for a different historical revision")
    if result.historical_path != anchor.historical_file_path:
        raise ValueError("Lineage result is for a different historical path")
    timestamp = recorded_at or datetime.datetime.now(datetime.UTC)
    if timestamp.tzinfo is None:
        raise ValueError("recorded_at must have a timezone")
    return tuple({
        "code_anchor_id": anchor.id,
        "historical_commit_sha": result.historical_commit_sha,
        "transition_commit_sha": event.transition_commit_sha,
        "compared_parent_sha": event.compared_parent_sha,
        "parent_index": event.parent_index,
        "target_commit_sha": event.target_commit_sha or event.transition_commit_sha,
        "traversal_strategy": event.traversal_strategy,
        "historical_path": event.old_path or result.historical_path,
        "migrated_path": event.new_path,
        "transition_type": {
            "DELETE": "DELETED",
            "MODIFY": "EXACT_MATCH",
            "UNCHANGED": "EXACT_MATCH",
            "UNRELATED_PARENT": "EXACT_MATCH",
        }.get(event.transition_type, event.transition_type),
        "mapping_evidence": {**event.mapping_evidence,
                             "state": event.state.value,
                             "verified_parser": result.verified_parser,
                             "raw_transition_type": event.transition_type},
        "confidence_score": event.confidence_score,
        "recorded_at": event.recorded_at or timestamp,
    } for event in result.events)