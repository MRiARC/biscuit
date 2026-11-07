"""Unit tests for offline code grounding, polyglot symbol extraction, and lineage mapping."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from biscuit.grounding import (
    AnchorState,
    CodeSymbolAnchor,
    LineageEvent,
    LineageResult,
    ParserUnavailable,
    ast_structural_fingerprint,
    extract_symbols,
    lineage_storage_records,
    resolve_merge_parents,
)


def test_python_symbol_extraction() -> None:
    code = (
        "class Pipeline:\n"
        "    def run(self, data):\n"
        "        return data.process()\n\n"
        "def helper(x):\n"
        "    return x + 1\n"
    )
    symbols = extract_symbols(code, "python", "src/pipeline.py")
    assert len(symbols) == 3
    names = [s.qualified_name for s in symbols]
    assert names == ["pipeline.Pipeline", "pipeline.Pipeline.run", "pipeline.helper"]

    # Verify positions and types
    cls_sym = symbols[0]
    assert cls_sym.symbol_type == "class"
    assert cls_sym.start_line == 1
    assert cls_sym.language == "python"

    method_sym = symbols[1]
    assert method_sym.symbol_type == "method"
    assert method_sym.start_line == 2

    func_sym = symbols[2]
    assert func_sym.symbol_type == "function"
    assert func_sym.start_line == 5


def test_polyglot_typescript_extraction() -> None:
    code = (
        "export class ParserService {\n"
        "  parse(input: string): Node {\n"
        "    return null;\n"
        "  }\n"
        "}\n\n"
        "export function tokenize(source: string) {\n"
        "  return [];\n"
        "}\n"
    )
    symbols = extract_symbols(code, "typescript", "src/lib/parser.ts")
    assert len(symbols) >= 2
    names = [s.qualified_name for s in symbols]
    assert "parser.ParserService" in names
    assert "parser.tokenize" in names


def test_polyglot_go_and_rust_extraction() -> None:
    go_code = (
        "package main\n\n"
        "func ProcessStream(s *Stream) error {\n"
        "    return nil\n"
        "}\n"
    )
    go_symbols = extract_symbols(go_code, "go", "pkg/stream.go")
    assert any("ProcessStream" in s.qualified_name for s in go_symbols)

    rust_code = (
        "pub fn execute_query(query: &str) -> Result<(), Error> {\n"
        "    Ok(())\n"
        "}\n"
    )
    rust_symbols = extract_symbols(rust_code, "rust", "src/db.rs")
    assert any("execute_query" in s.qualified_name for s in rust_symbols)


def test_c_symbol_extraction_and_strict_mode() -> None:
    c_code = "int tok_get(void) { return 0; }\n"
    symbols = extract_symbols(c_code, "c", "Parser/tokenizer.c")
    assert len(symbols) == 1
    assert "tok_get" in symbols[0].qualified_name

    with pytest.raises(ParserUnavailable):
        extract_symbols(c_code, "c", "Parser/tokenizer.c", strict=True)


def test_anchor_contract_serialization_and_authority_scope() -> None:
    symbol = extract_symbols("def f(x):\n    return x\n", "python", "pkg/a.py")[0]
    alternative = uuid.uuid4()
    anchor = CodeSymbolAnchor.from_symbol(
        symbol,
        alternative_id=alternative,
        repository_url="https://github.com/owner/repo",
        historical_commit_sha="a" * 40,
    )
    record = anchor.to_storage_record()
    assert record["alternative_id"] == alternative
    assert record["symbol_type"] == "function"
    assert record["source_locator_type"] in {"AST_NODE_QUERY", "FILE_BYTE_RANGE"}
    assert record["anchor_state"] == "HISTORICAL"
    assert record["mapping_confidence"] is None
    assert record["signature_hash"] == symbol.signature_hash
    assert record["historical_file_path"] == "pkg/a.py"
    assert record["parser_version"] and record["grammar_version"]
    assert record["ast_normalization_version"]
    assert "verified_parser" not in record

    with pytest.raises(ValueError, match="canonical symbol type"):
        CodeSymbolAnchor.from_symbol(
            replace(symbol, symbol_type="INVALID_TYPE"),
            alternative_id=alternative,
            repository_url="https://example.com",
            historical_commit_sha="a" * 40,
        )


def test_lineage_storage_records_mapping() -> None:
    symbol = extract_symbols("def f(x):\n    return x\n", "python", "old/parser.py")[0]
    anchor = CodeSymbolAnchor.from_symbol(
        symbol,
        alternative_id=uuid.uuid4(),
        repository_url="https://github.com/owner/repo",
        historical_commit_sha="a" * 40,
    )
    event = LineageEvent(
        transition_commit_sha="b" * 40,
        compared_parent_sha="a" * 40,
        parent_index=0,
        old_path="old/parser.py",
        new_path="new/parser.py",
        transition_type="MODIFY",
        state=AnchorState.MIGRATED,
    )
    result = LineageResult(
        historical_commit_sha="a" * 40,
        head_commit_sha="b" * 40,
        historical_path="old/parser.py",
        resolved_path="new/parser.py",
        anchor_state=AnchorState.MIGRATED,
        events=(event,),
    )
    now = datetime.now(UTC)
    records = lineage_storage_records(anchor, result, recorded_at=now)
    assert len(records) == 1
    rec = records[0]
    assert rec["code_anchor_id"] == anchor.id
    assert rec["historical_path"] == "old/parser.py"
    assert rec["migrated_path"] == "new/parser.py"
    assert rec["transition_type"] == "EXACT_MATCH"
    assert rec["recorded_at"] == now

    with pytest.raises(ValueError, match="different historical revision"):
        lineage_storage_records(replace(anchor, historical_commit_sha="c" * 40), result)


def test_merge_resolver_decision_matrix() -> None:
    # 1. Single surviving path is merged
    res = resolve_merge_parents([["path/a.py"]], ["path/a.py"])
    assert res.state == AnchorState.MERGED

    # 2. Empty merge candidates is deleted
    res_del = resolve_merge_parents([["path/a.py"]], [])
    assert res_del.state == AnchorState.DELETED

    # 3. Multiple candidates is ambiguous
    res_amb = resolve_merge_parents([["path/a.py"]], ["path/a.py", "path/b.py"])
    assert res_amb.state == AnchorState.AMBIGUOUS

    # 4. Divergent parent candidates is ambiguous
    res_div = resolve_merge_parents([["path/a.py"], ["path/b.py"]], ["path/a.py"])
    assert res_div.state == AnchorState.AMBIGUOUS


def test_structural_fingerprinting_invariance() -> None:
    code1 = "def compute(x, y):\n    return x * y\n"
    code2 = "def compute(a, b):\n    return a * b\n"
    fp1 = ast_structural_fingerprint(code1)
    fp2 = ast_structural_fingerprint(code2)
    assert fp1 == fp2

    # Malformed syntax falls back safely
    fp_invalid = ast_structural_fingerprint("@babel/parser import { parse }")
    assert isinstance(fp_invalid, str) and len(fp_invalid) == 64
