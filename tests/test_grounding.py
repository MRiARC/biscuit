"""Isolated Stream-2 regression tests: stdlib + Git; optional Tree-sitter CST."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from biscuit.grounding import (
    AnchorState,
    ParseError,
    ParserUnavailable,
    extract_symbols,
    fingerprint_python_ast,
    normalized_python_ast,
)
from biscuit.grounding.lineage import GitLineageError, forward_commits, trace_lineage
from biscuit.grounding.merge_resolver import resolve_merge_parents


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
        timeout=30,
    )
    return proc.stdout.strip()


def initialize_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Grounding Test")
    git(root, "config", "user.email", "grounding@example.invalid")
    write(root, "old/parser.py", "def tok_get(stream):\n    return stream + 1\n")
    first = commit(root, "initial code")
    return root, first


def write(root: Path, path: str, data: str) -> None:
    full = root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(data, encoding="utf-8")


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def test_python_qualified_symbol_and_byte_offsets() -> None:
    text = "# café\nclass Parser:\n    def tok_get(self, stream):\n        return stream + 1\n"
    syms = extract_symbols(text, "python", "cpython/parser.py")
    assert [s.qualified_name for s in syms] == [
        "parser.Parser", "parser.Parser.tok_get",
    ]
    method = syms[1]
    assert method.symbol_type == "method"
    assert method.start_line == 3 and method.end_line == 4
    assert text.encode()[method.start_byte:method.end_byte].startswith(b"def tok_get")
    assert method.signature_hash and len(method.signature_hash) == 64
    assert method.structural_fingerprint and len(method.structural_fingerprint) == 64
    assert method.parser_backend in ("tree-sitter", "stdlib-ast")


def test_c_qualified_symbol_and_struct() -> None:
    code = "struct tok_state { int next; };\nint tok_get(struct tok_state *tok) { return tok->next; }\n"
    syms = extract_symbols(code, "c", "Parser/tokenizer.c")
    assert any(x.qualified_name == "tokenizer.c.tok_state"
               and x.symbol_type == "ast_node" for x in syms)
    function = next(s for s in syms if s.symbol_name == "tok_get")
    assert function.qualified_name == "tokenizer.c.tok_state.tok_get"
    assert function.start_line == 2
    assert code.encode()[function.start_byte:function.end_byte].endswith(b"}")
    assert len(function.signature_hash) == 64


def test_python_lowercase_class_and_nested_functions() -> None:
    text = "class handler:\n    def run(self):\n        def helper(x):\n            return x\n        return helper(2)\n"
    symbols = extract_symbols(text, "python", "pkg/handler.py")
    assert {x.qualified_name: x.symbol_type for x in symbols} == {
        "handler.handler": "class",
        "handler.handler.run": "method",
        "handler.handler.run.helper": "function",
    }
    assert all(x.start_byte < x.end_byte for x in symbols)


def test_c_filename_qualification_and_structural_hash() -> None:
    text = "int tok_get(int x) { return x + 1; }\n"
    sym = next(x for x in extract_symbols(text, "c", "Parser/tokenizer.c")
               if x.symbol_type == "function")
    assert sym.qualified_name == "tokenizer.c.tok_get"
    assert sym.structural_fingerprint is not None
    assert len(sym.structural_fingerprint) == 64


def test_python_ast_variable_and_argument_renames() -> None:
    a = "def parse(stream):\n    value = stream + 1\n    return value\n"
    b = "def parse(thing):\n\n    result = thing + 1\n    return result\n"
    c = "def parse(thing):\n    result = thing + 2\n    return result\n"
    assert normalized_python_ast(a) == normalized_python_ast(b)
    assert fingerprint_python_ast(a) == fingerprint_python_ast(b)
    assert fingerprint_python_ast(a) != fingerprint_python_ast(c)
    tree = ast.parse(a)
    before = ast.dump(tree)
    fingerprint_python_ast(tree)
    assert ast.dump(tree) == before  # Caller-owned AST is not mutated.


def test_normalizer_nested_scope_and_closure_renames() -> None:
    original = "def outer(first):\n    local = first + 1\n    def inner(second):\n        return local + second\n    return inner(first)\n"
    renamed = "def outer(start):\n    value = start + 1\n    def inner(term):\n        return value + term\n    return inner(start)\n"
    changed = "def outer(start):\n    value = start + 2\n    def inner(term):\n        return value + term\n    return inner(start)\n"
    assert fingerprint_python_ast(original) == fingerprint_python_ast(renamed)
    assert fingerprint_python_ast(original) != fingerprint_python_ast(changed)


def test_unsupported_language_and_invalid_python() -> None:
    with pytest.raises(ValueError):
        extract_symbols("fn run() {}", "rust", "a.rs")
    with pytest.raises(ParseError):
        extract_symbols("def broken(:\n", "python", "broken.py")


def test_forward_rename_and_immutable_mapping_evidence(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    git(root, "mv", "old/parser.py", "old/lexer.py")
    rename = commit(root, "rename parser file")
    write(root, "old/lexer.py", "def tok_get(x):\n    return x + 1\n")
    head = commit(root, "rename variable")
    assert forward_commits(root, past) == (rename, head)
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.anchor_state == AnchorState.MIGRATED
    assert result.resolved_path == "old/lexer.py"
    assert result.mapping_confidence is None
    assert any(e.transition_type == "RENAME_M" for e in result.events)
    # Without installed Tree-sitter grammars, fallback is never authoritative.
    assert result.safe_for_automatic_migration == result.verified_parser
    assert all(e.confidence_score is None for e in result.events)
    assert all(len(e.mapping_evidence["diff_patch_sha256"]) == 64 for e in result.events)
    assert all(e.compared_parent_sha for e in result.events)


def test_symbol_moves_into_another_module_without_git_file_rename(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    (root / "old/parser.py").unlink()
    write(root, "new/helper.py", "def tok_get(value):\n    return value + 1\n")
    commit(root, "move symbol to helper")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.resolved_path == "new/helper.py"
    assert result.anchor_state == AnchorState.MIGRATED


def test_symbol_split_marks_ambiguous(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    (root / "old/parser.py").unlink()
    write(root, "new/one.py", "def tok_get(x):\n    return x + 1\n")
    write(root, "new/two.py", "def tok_get(y):\n    return y + 1\n")
    commit(root, "copy into two alternatives")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.anchor_state == AnchorState.AMBIGUOUS
    assert result.resolved_path is None
    assert set(result.candidates) == {"new/one.py", "new/two.py"}
    assert not result.safe_for_automatic_migration


def test_exact_git_copy_is_classified_copy_c(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    original = (root / "old/parser.py").read_text(encoding="utf-8")
    write(root, "copied/parser.py", original)
    commit(root, "copy complete original source file")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.anchor_state == AnchorState.AMBIGUOUS
    assert set(result.candidates) == {"old/parser.py", "copied/parser.py"}
    assert any(e.transition_type == "COPY_C" for e in result.events)
    assert result.mapping_confidence is None


def test_file_rewrite_without_symbol_is_classified_and_deleted(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    write(root, "old/parser.py", "def new_feature(x):\n    return x * 50\n")
    commit(root, "rewrite file and remove historical symbol")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.anchor_state == AnchorState.DELETED
    assert result.resolved_path is None
    assert any(e.transition_type == "REWRITE_SPLIT_B" for e in result.events)
    assert not result.safe_for_automatic_migration


def test_copy_with_original_remaining_requires_abstention(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    write(root, "new/copy.py", "def tok_get(other):\n    return other + 1\n")
    commit(root, "copy symbol without deleting original")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.anchor_state == AnchorState.AMBIGUOUS
    assert result.resolved_path is None
    assert set(result.candidates) == {"old/parser.py", "new/copy.py"}
    assert result.mapping_confidence is None


def test_same_name_unrelated_function_does_not_create_false_move(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    (root / "old/parser.py").unlink()
    write(root, "unrelated/other.py", "def tok_get(stream):\n    return stream * 999\n")
    commit(root, "delete old function and introduce unrelated same-name function")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.anchor_state == AnchorState.DELETED
    assert result.resolved_path is None
    assert not result.safe_for_automatic_migration


def test_conflict_mainline_selected_with_combined_diff_evidence(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    git(root, "checkout", "-qb", "feature")
    write(root, "old/parser.py", "def unrelated(x):\n    return x * 3\n")
    commit(root, "replace historical symbol in feature")
    git(root, "checkout", "-q", "main")
    write(root, "old/parser.py", "def tok_get(stream):\n    return stream + 42\n")
    commit(root, "change original symbol on mainline")
    attempted = subprocess.run(["git", "-C", str(root), "merge", "--no-ff", "feature"],
                               capture_output=True, text=True, check=False)
    assert attempted.returncode != 0
    write(root, "old/parser.py", "def tok_get(stream):\n    return stream + 42\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "resolve conflict with first-parent content")
    merge = git(root, "rev-parse", "HEAD")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.resolved_path == "old/parser.py"
    assert result.anchor_state == AnchorState.MIGRATED
    merge_events = [e for e in result.events if e.transition_commit_sha == merge]
    assert len(merge_events) == 2
    assert all(e.mapping_evidence["first_parent_selected"] for e in merge_events)
    assert all(len(e.mapping_evidence["combined_diff_sha256"]) == 64 for e in merge_events)


def test_clean_two_parent_merge(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    git(root, "checkout", "-qb", "feature")
    write(root, "feature.txt", "feature work\n")
    commit(root, "feature unrelated")
    git(root, "checkout", "-q", "main")
    write(root, "main.txt", "main work\n")
    commit(root, "main unrelated")
    git(root, "merge", "--no-ff", "-qm", "combine clean branches", "feature")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.resolved_path == "old/parser.py"
    assert result.anchor_state in (AnchorState.MERGED, AnchorState.CURRENT)
    merge = git(root, "rev-parse", "HEAD")
    assert len([e for e in result.events if e.transition_commit_sha == merge]) == 2
    assert {e.parent_index for e in result.events if e.transition_commit_sha == merge} == {0, 1}
    assert all(e.confidence_score is None for e in result.events)


def test_divergent_merge_paths_are_ambiguous(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    git(root, "checkout", "-qb", "feature")
    git(root, "mv", "old/parser.py", "old/feature_parser.py")
    commit(root, "feature path")
    git(root, "checkout", "-q", "main")
    git(root, "mv", "old/parser.py", "old/main_parser.py")
    commit(root, "main path")
    # A rename/rename merge needs human conflict resolution. Retain both
    # surviving alternatives so the lineage engine must abstain.
    subprocess.run(
        ["git", "-C", str(root), "merge", "--no-ff", "feature"],
        capture_output=True, text=True, check=False,
    )
    for path in ("old/feature_parser.py", "old/main_parser.py"):
        write(root, path, "def tok_get(arg):\n    return arg + 1\n")
    (root / "old/parser.py").unlink(missing_ok=True)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "retain both divergent paths")
    result = trace_lineage(root, past, "old/parser.py", "tok_get")
    assert result.anchor_state == AnchorState.AMBIGUOUS
    assert result.resolved_path is None
    assert not result.safe_for_automatic_migration


def test_abstain_on_non_ancestor_or_unknown_symbol(tmp_path: Path) -> None:
    root, past = initialize_repo(tmp_path)
    with pytest.raises(GitLineageError, match="symbol not found"):
        trace_lineage(root, past, "old/parser.py", "nonexistent")
    git(root, "checkout", "--orphan", "independent")
    git(root, "rm", "-rfq", ".")
    write(root, "independent.py", "print('not descended')\n")
    commit(root, "unrelated root")
    with pytest.raises(GitLineageError, match="not an ancestor"):
        forward_commits(root, past)


def test_merge_resolver_abstains_on_conflict() -> None:
    decision = resolve_merge_parents([{"a.py"}, {"b.py"}], {"a.py", "b.py"})
    assert decision.state == AnchorState.AMBIGUOUS
    assert decision.confidence_score is None
    assert resolve_merge_parents([{"a.py"}, {"a.py"}], {"a.py"}).state == AnchorState.MERGED


def test_strict_mode_either_uses_treesitter_or_abstains() -> None:
    try:
        syms = extract_symbols("def f(x):\n    return x\n", "python", "a.py",
                               require_tree_sitter=True)
    except ParserUnavailable:
        return  # Minimal package CI lacks optional grammar dependencies.
    assert syms and all(s.parser_backend == "tree-sitter" for s in syms)


def test_only_named_tree_sitter_modules_are_optional() -> None:
    try:
        py = extract_symbols("def f(x):\n    return x\n", "python", "a.py",
                             require_tree_sitter=True)
        c = extract_symbols("int f(int x) { return x; }", "c", "a.c",
                            require_tree_sitter=True)
    except ParserUnavailable:
        if os.environ.get("BISCUIT_REQUIRE_TREE_SITTER") == "1":
            pytest.fail("Tree-sitter integration required but grammar packages are missing")
        pytest.skip("Optional Tree-sitter grammars not installed in minimal CI")
    assert py[0].parser_backend == c[0].parser_backend == "tree-sitter"