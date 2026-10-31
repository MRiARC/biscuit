"""Versioned Python AST fingerprints which ignore local identifier spelling.

The algorithm never masks imported/global call targets, attribute names, literals,
or operators: treating those as interchangeable can create dangerous false matches.
"""

from __future__ import annotations

import ast
import copy
import hashlib

NORMALIZATION_VERSION = "python-ast-mask-v1"


class IdentifierMasker(ast.NodeTransformer):
    """Use first-binding order for stable argument/local-variable tokens."""

    def __init__(self) -> None:
        self.args: dict[str, str] = {}
        self.locals: dict[str, str] = {}

    def visit_arg(self, node: ast.arg) -> ast.arg:
        if node.arg not in self.args:
            self.args[node.arg] = f"$ARG_{len(self.args)}"
        node.arg = self.args[node.arg]
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if isinstance(node.ctx, (ast.Store, ast.Del)) and node.id not in self.args:
            self.locals.setdefault(node.id, f"$VAR_{len(self.locals)}")
        node.id = self.args.get(node.id, self.locals.get(node.id, node.id))
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.name = "$FUNC"
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        node.name = "$FUNC"
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node.name = "$CLASS"
        return self.generic_visit(node)


def normalized_python_ast(source_or_node: str | ast.AST) -> str:
    """Return a deterministic AST serialization; does not mutate caller's AST."""
    tree = ast.parse(source_or_node) if isinstance(source_or_node, str) else source_or_node
    tree = IdentifierMasker().visit(copy.deepcopy(tree))
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def fingerprint_python_ast(source_or_node: str | ast.AST) -> str:
    return hashlib.sha256(normalized_python_ast(source_or_node).encode("utf-8")).hexdigest()


# Familiar aliases for callers using the architectural specification.
normalize_python_ast = normalized_python_ast
ast_structural_fingerprint = fingerprint_python_ast