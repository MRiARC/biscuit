"""Python/C qualified symbol extraction with an optional Tree-sitter backend.

When optional Tree-sitter grammars are unavailable (the current minimal CI),
Python uses stdlib ``ast`` and C uses a conservative restricted lexer. Symbols
carry ``parser_backend`` so the caller can abstain from using a fallback as
verified Tree-sitter evidence. Parser failures never silently become matches.

Install tree-sitter, tree-sitter-python and tree-sitter-c to use actual CSTs.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import re
from pathlib import PurePosixPath

from .normalizer import NORMALIZATION_VERSION, fingerprint_python_ast
from .types import Symbol


class ParserUnavailable(RuntimeError):
    pass


class ParseError(ValueError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _signature(text: str) -> str:
    return _sha(" ".join(text.split()))


def _module(file_path: str) -> str:
    path = PurePosixPath(file_path)
    return path.stem


def _line_at(raw: bytes, offset: int) -> int:
    return raw.count(b"\n", 0, offset) + 1


def _ast_offsets(node: ast.AST, raw: bytes) -> tuple[int, int]:
    lines = raw.splitlines(keepends=True)
    if not hasattr(node, "end_lineno") or node.end_lineno is None:
        raise ParseError("Python AST lacks end position")
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    return start, end


def _python_ast_symbols(source: bytes, file_path: str) -> list[Symbol]:
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text)
    except (SyntaxError, UnicodeError) as exc:
        raise ParseError(str(exc)) from exc
    result: list[Symbol] = []

    def walk(body: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = ("class" if isinstance(node, ast.ClassDef) else
                        "method" if parents and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and parents[-1][:1].isupper() else "function")
                start, end = _ast_offsets(node, source)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    signature = ast.dump(node.args, include_attributes=False)
                else:
                    signature = ",".join(ast.dump(base, include_attributes=False) for base in node.bases)
                result.append(Symbol(
                    language="python", symbol_type=kind,
                    qualified_name=".".join((_module(file_path), *parents, node.name)),
                    file_path=file_path, start_byte=start, end_byte=end,
                    start_line=node.lineno, end_line=node.end_lineno,
                    signature_hash=_signature(signature),
                    structural_fingerprint=fingerprint_python_ast(node),
                    parser_backend="stdlib-ast", parser_version="python-stdlib",
                    grammar_version="stdlib-ast", normalization_version=NORMALIZATION_VERSION,
                ))
                walk(node.body, (*parents, node.name))
    walk(tree.body, ())
    return result


def _ts_parser(language: str):
    try:
        from tree_sitter import Language, Parser
        grammar = __import__("tree_sitter_python" if language == "python" else "tree_sitter_c")
    except ImportError as exc:
        raise ParserUnavailable("Install tree-sitter, tree-sitter-python and tree-sitter-c") from exc
    try:
        lang = Language(grammar.language())
        try:
            parser = Parser(lang)
        except TypeError:
            parser = Parser()
            parser.set_language(lang)
        version = importlib.metadata.version("tree-sitter")
        grammar_version = importlib.metadata.version(
            "tree-sitter-python" if language == "python" else "tree-sitter-c"
        )
        return parser, version, grammar_version
    except (TypeError, AttributeError) as exc:
        raise ParserUnavailable("Incompatible Tree-sitter Python bindings/grammar versions") from exc


def _node_text(node, raw: bytes) -> str:
    return raw[node.start_byte: node.end_byte].decode("utf-8", errors="strict")


def _python_tree_sitter_symbols(source: bytes, file_path: str) -> list[Symbol]:
    parser, version, grammar = _ts_parser("python")
    tree = parser.parse(source)
    if tree.root_node.has_error:
        raise ParseError("Tree-sitter Python parser returned ERROR node")
    ast_by_name = { (s.qualified_name, s.start_line): s for s in _python_ast_symbols(source, file_path)}
    result: list[Symbol] = []

    def walk(node, parents: tuple[str, ...]) -> None:
        if node.type == "decorated_definition":
            for ch in node.named_children:
                walk(ch, parents)
            return
        if node.type in ("class_definition", "function_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                raise ParseError("Definition node has no name")
            name = _node_text(name_node, source)
            name_qual = ".".join((_module(file_path), *parents, name))
            args = node.child_by_field_name("parameters") or node.child_by_field_name("superclasses")
            ast_symbol = ast_by_name.get((name_qual, node.start_point.row + 1))
            result.append(Symbol(
                language="python", symbol_type=("class" if node.type == "class_definition"
                                                else "method" if parents else "function"),
                qualified_name=name_qual, file_path=file_path,
                start_byte=node.start_byte, end_byte=node.end_byte,
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                signature_hash=_signature(_node_text(args, source) if args else ""),
                structural_fingerprint=(ast_symbol.structural_fingerprint if ast_symbol else None),
                parser_backend="tree-sitter", parser_version=version,
                grammar_version=grammar, normalization_version=NORMALIZATION_VERSION,
            ))
            for ch in node.named_children:
                if ch.type == "block":
                    for body_ch in ch.named_children:
                        walk(body_ch, (*parents, name))
            return
        for ch in node.named_children:
            walk(ch, parents)

    walk(tree.root_node, ())
    return result


def _declarator_name(node, raw: bytes) -> str | None:
    if node.type in ("identifier", "field_identifier"):
        return _node_text(node, raw)
    # Descend through function/pointer/parenthesized declarators, not parameter types.
    child = node.child_by_field_name("declarator")
    if child is not None:
        return _declarator_name(child, raw)
    for ch in node.named_children:
        if "declarator" in ch.type or ch.type == "identifier":
            name = _declarator_name(ch, raw)
            if name:
                return name
    return None


def _c_qualified(file_path: str, name: str, signature: str) -> str:
    # A C struct does not own a function. Include the struct when it is part of
    # the function signature; otherwise qualify by file + function only.
    struct_match = re.search(r"\bstruct\s+(\w+)\s*\*?\s*\w+", signature)
    return ".".join((_module(file_path), *((struct_match.group(1),) if struct_match else ()), name))


def _c_tree_sitter_symbols(source: bytes, file_path: str) -> list[Symbol]:
    parser, version, grammar = _ts_parser("c")
    tree = parser.parse(source)
    if tree.root_node.has_error:
        raise ParseError("Tree-sitter C parser returned ERROR node")
    out: list[Symbol] = []

    def walk(node) -> None:
        if node.type == "function_definition":
            decl = node.child_by_field_name("declarator")
            name = _declarator_name(decl, source) if decl else None
            if not name:
                raise ParseError("C function has no declarator name")
            signature = _node_text(decl, source)
            out.append(Symbol(
                "c", "function", _c_qualified(file_path, name, signature), file_path,
                node.start_byte, node.end_byte,
                node.start_point.row + 1, node.end_point.row + 1,
                _signature(signature), parser_backend="tree-sitter",
                parser_version=version, grammar_version=grammar,
                normalization_version="c-cst-v1",
            ))
        elif node.type == "struct_specifier":
            name = node.child_by_field_name("name")
            if name:
                out.append(Symbol(
                    "c", "struct", f"{_module(file_path)}.{_node_text(name, source)}", file_path,
                    node.start_byte, node.end_byte,
                    node.start_point.row + 1, node.end_point.row + 1,
                    _signature(_node_text(name, source)), parser_backend="tree-sitter",
                    parser_version=version, grammar_version=grammar,
                    normalization_version="c-cst-v1",
                ))
        for ch in node.named_children:
            walk(ch)
    walk(tree.root_node)
    return out


def _c_restricted_lexer(source: bytes, file_path: str) -> list[Symbol]:
    """Minimal offline fallback for simple snippets; never label it Tree-sitter.

    Reject malformed/unbalanced braces rather than guessing lineage evidence.
    For production C parsing, require the Tree-sitter backend.
    """
    try:
        text = source.decode("utf-8")
    except UnicodeError as exc:
        raise ParseError(str(exc)) from exc
    result: list[Symbol] = []
    # This lexer intentionally does not support preprocessor-heavy, macro-based,
    # or GNU-extension declarations; callers must inspect parser_backend.
    pattern = re.compile(r"\b(?:[\w*]+\s+)+([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*\{", re.M)
    for match in pattern.finditer(text):
        level = 1
        pos = match.end()
        while pos < len(text) and level:
            if text[pos] == "{":
                level += 1
            elif text[pos] == "}":
                level -= 1
            pos += 1
        if level:
            raise ParseError("Unbalanced C function braces")
        start = len(text[:match.start()].encode("utf-8"))
        end = len(text[:pos].encode("utf-8"))
        signature = text[match.start():match.end()-1]
        name = match.group(1)
        result.append(Symbol(
            "c", "function", _c_qualified(file_path, name, signature), file_path,
            start, end, _line_at(source, start), _line_at(source, end),
            _signature(signature), parser_backend="restricted-c-lexer",
            parser_version="restricted-v1", grammar_version="none",
            normalization_version="c-lexer-v1",
        ))
    for match in re.finditer(r"\bstruct\s+(\w+)\s*\{", text):
        start = len(text[:match.start()].encode("utf-8"))
        end = len(text[:match.end()].encode("utf-8"))
        result.append(Symbol(
            "c", "struct", f"{_module(file_path)}.{match.group(1)}", file_path,
            start, end, _line_at(source, start), _line_at(source, end),
            _signature(match.group(1)), parser_backend="restricted-c-lexer",
            parser_version="restricted-v1", grammar_version="none",
            normalization_version="c-lexer-v1",
        ))
    return result


def extract_symbols(
    source: str | bytes, language: str, file_path: str,
    *, require_tree_sitter: bool = False,
) -> list[Symbol]:
    """Extract qualified definitions and exact source byte/line ranges.

    ``require_tree_sitter=True`` is the production/verified-grounding mode.
    ``False`` enables clearly labeled, conservative offline test fallbacks.
    """
    raw = source.encode("utf-8") if isinstance(source, str) else source
    if language.lower() not in {"python", "c"}:
        raise ValueError(f"Unsupported grounding language: {language}")
    try:
        return (_python_tree_sitter_symbols(raw, file_path) if language.lower() == "python"
                else _c_tree_sitter_symbols(raw, file_path))
    except ParserUnavailable:
        if require_tree_sitter:
            raise
        return (_python_ast_symbols(raw, file_path) if language.lower() == "python"
                else _c_restricted_lexer(raw, file_path))