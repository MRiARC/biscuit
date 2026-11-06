"""Polyglot symbol extraction and structural fingerprinting for code anchors.

Provides zero-dependency, pure Python symbol extraction for Python and polyglot
languages (TypeScript, JavaScript, Go, Rust, C, Java, etc.) without requiring
fragile native Tree-sitter compiler bindings.
"""

from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import PurePosixPath

from biscuit.grounding.types import (
    NORMALIZATION_VERSION,
    ParseError,
    ParserUnavailable,
    Symbol,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _module(file_path: str) -> str:
    return PurePosixPath(file_path).stem


def _ast_offsets(node: ast.AST, raw: bytes) -> tuple[int, int]:
    lines = raw.splitlines(keepends=True)
    if not hasattr(node, "end_lineno") or node.end_lineno is None:
        raise ParseError("Python AST lacks end position")
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    return start, end


def fingerprint_python_ast(node: ast.AST) -> str:
    """Compute structural fingerprint of an AST node."""
    dumped = ast.dump(node, annotate_fields=True, include_attributes=False)
    # Strip identifiers for structural invariance
    normalized = re.sub(r"'(?:[^'\\]|\\.)*'", "'$ID'", dumped)
    return _sha(normalized)


def ast_structural_fingerprint(code: str) -> str:
    """Compute structural fingerprint for code snippet."""
    try:
        parsed = ast.parse(code)
        if parsed.body:
            return fingerprint_python_ast(parsed.body[0])
    except (SyntaxError, ValueError):
        pass
    # Universal fallback for polyglot snippets
    compact = " ".join(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[^\s\w]", code))
    return _sha(compact)


def _python_ast_symbols(source: bytes, file_path: str) -> list[Symbol]:
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text)
    except (SyntaxError, UnicodeError) as exc:
        raise ParseError(str(exc)) from exc
    result: list[Symbol] = []

    def walk(body: list[ast.stmt], parents: tuple[str, ...], parent_is_class: bool) -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = (
                    "class"
                    if isinstance(node, ast.ClassDef)
                    else "method"
                    if parent_is_class
                    else "function"
                )
                start, end = _ast_offsets(node, source)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signature = ast.dump(node.args, include_attributes=False)
                else:
                    signature = ",".join(
                        ast.dump(base, include_attributes=False) for base in node.bases
                    )
                result.append(
                    Symbol(
                        language="python",
                        symbol_type=kind,
                        qualified_name=".".join((_module(file_path), *parents, node.name)),
                        file_path=file_path,
                        start_byte=start,
                        end_byte=end,
                        start_line=node.lineno,
                        end_line=node.end_lineno,
                        signature_hash=_sha(" ".join(signature.split())),
                        structural_fingerprint=fingerprint_python_ast(node),
                        parser_backend="stdlib-ast",
                        parser_version=f"python-ast/{_python_version()}",
                        grammar_version="stdlib-ast",
                        normalization_version=NORMALIZATION_VERSION,
                    )
                )
                if isinstance(node, ast.ClassDef):
                    walk(node.body, (*parents, node.name), True)

    walk(tree.body, (), False)
    return result


_POLYGLOT_DECL_PATTERNS = {
    "typescript": re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        re.MULTILINE,
    ),
    "javascript": re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        re.MULTILINE,
    ),
    "go": re.compile(
        r"^\s*(?:func(?:\s*\([^)]+\))?\s+|type\s+)([A-Za-z_][A-Za-z0-9_]*)",
        re.MULTILINE,
    ),
    "rust": re.compile(
        r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|type|impl)\s+([A-Za-z_][A-Za-z0-9_]*)",
        re.MULTILINE,
    ),
    "c": re.compile(
        r"^\s*(?:static\s+|inline\s+|extern\s+)?(?:struct\s+|enum\s+|union\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\*?\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        re.MULTILINE,
    ),
}


def _polyglot_symbols(source: bytes, language: str, file_path: str) -> list[Symbol]:
    try:
        text = source.decode("utf-8")
    except UnicodeError as exc:
        raise ParseError(str(exc)) from exc

    pattern = _POLYGLOT_DECL_PATTERNS.get(language.lower())
    if not pattern:
        # Generic identifier scanner
        pattern = re.compile(
            r"^\s*(?:export\s+|pub\s+)?(?:function|class|def|fn|type)\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.MULTILINE,
        )

    symbols: list[Symbol] = []
    mod = _module(file_path)

    for match in pattern.finditer(text):
        name = match.group(1)
        start_byte = match.start()
        end_byte = match.end()
        # Find line number (1-based)
        start_line = text[:start_byte].count("\n") + 1
        end_line = text[:end_byte].count("\n") + 1
        sig = text[start_byte : min(end_byte + 60, len(text))].strip()

        symbols.append(
            Symbol(
                language=language.lower(),
                symbol_type="function",
                qualified_name=f"{mod}.{name}",
                file_path=file_path,
                start_byte=start_byte,
                end_byte=end_byte,
                start_line=start_line,
                end_line=end_line,
                signature_hash=_sha(" ".join(sig.split())),
                structural_fingerprint=_sha(name),
                parser_backend="polyglot-regex",
                parser_version="1.0.0",
                grammar_version="polyglot-v1",
                normalization_version=NORMALIZATION_VERSION,
            )
        )

    return symbols


def extract_symbols(
    source: str | bytes,
    language: str,
    file_path: str,
    *,
    strict: bool = False,
) -> list[Symbol]:
    """Extract code symbols for Python or polyglot source code.

    No external C extensions or compiler toolchains required.
    """
    raw = source.encode("utf-8") if isinstance(source, str) else source
    lang = language.lower()

    if lang == "python":
        return _python_ast_symbols(raw, file_path)

    if lang in ("c", "h") and strict:
        raise ParserUnavailable("Strict Tree-sitter C parser disabled in polyglot mode")

    return _polyglot_symbols(raw, lang, file_path)
