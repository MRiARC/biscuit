"""Versioned Python AST fingerprints, insensitive to local spelling and format.

Global call targets, attributes, constants and operators remain distinguishable;
local bindings are scoped, so shadowed variables and closure references do not
collapse into the same structural identifier.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass, field

NORMALIZATION_VERSION = "python-ast-mask-v3-comp-scoped"


@dataclass
class _Scope:
    prefix: str
    args: dict[str, str] = field(default_factory=dict)
    locals: dict[str, str] = field(default_factory=dict)


class IdentifierMasker(ast.NodeTransformer):
    """Replace bound identifiers using lexical scopes and first-binding order."""

    def __init__(self) -> None:
        self.scopes = [_Scope("")]
        self.scope_number = 0
        self.comprehension_enclosing: list[_Scope] = []

    def _scope(self) -> _Scope:
        return self.scopes[-1]

    def _push(self) -> None:
        self.scope_number += 1
        prefix = "" if self.scope_number == 1 else f"$S{self.scope_number}_"
        self.scopes.append(_Scope(prefix))

    def _pop(self) -> None:
        self.scopes.pop()

    def visit_arg(self, node: ast.arg) -> ast.arg:
        scope = self._scope()
        if node.arg not in scope.args:
            scope.args[node.arg] = f"{scope.prefix}$ARG_{len(scope.args)}"
        node.arg = scope.args[node.arg]
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.Name:
        scope = self._scope()
        if isinstance(node.ctx, (ast.Store, ast.Del)) and node.id not in scope.args:
            scope.locals.setdefault(node.id, f"{scope.prefix}$VAR_{len(scope.locals)}")
        for active in reversed(self.scopes):
            mapped = active.args.get(node.id, active.locals.get(node.id))
            if mapped is not None:
                node.id = mapped
                break
        return node

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        node.name = "$FUNC"
        self._push()
        try:
            return self.generic_visit(node)
        finally:
            self._pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        return self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        return self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node.name = "$CLASS"
        self._push()
        try:
            return self.generic_visit(node)
        finally:
            self._pop()

    def visit_Lambda(self, node: ast.Lambda) -> ast.Lambda:
        self._push()
        try:
            return self.generic_visit(node)
        finally:
            self._pop()



    def _visit_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> ast.AST:
        """Bind targets before result/filter loads; the first iterable is external.

        Subsequent iterables use preceding comprehension bindings. NamedExpr
        targets bind in the enclosing function scope, not the comprehension.
        """
        if not node.generators:
            return self.generic_visit(node)
        first = node.generators[0]
        first.iter = self.visit(first.iter)
        enclosing = self._scope()
        self._push()
        self.comprehension_enclosing.append(enclosing)
        try:
            for i, generator in enumerate(node.generators):
                if i:
                    generator.iter = self.visit(generator.iter)
                generator.target = self.visit(generator.target)
                generator.ifs = [self.visit(test) for test in generator.ifs]
            if isinstance(node, ast.DictComp):
                node.key = self.visit(node.key)
                node.value = self.visit(node.value)
            else:
                node.elt = self.visit(node.elt)
        finally:
            self.comprehension_enclosing.pop()
            self._pop()
        return node

    def visit_ListComp(self, node: ast.ListComp) -> ast.AST:
        return self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> ast.AST:
        return self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> ast.AST:
        return self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> ast.AST:
        return self._visit_comprehension(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> ast.NamedExpr:
        if self.comprehension_enclosing and isinstance(node.target, ast.Name):
            scope = self.comprehension_enclosing[-1]
            original = node.target.id
            if original not in scope.args:
                scope.locals.setdefault(original, f"{scope.prefix}$VAR_{len(scope.locals)}")
            node.target.id = scope.args.get(original, scope.locals.get(original))
        else:
            node.target = self.visit(node.target)
        node.value = self.visit(node.value)
        return node


def normalized_python_ast(source_or_node: str | ast.AST) -> str:
    """Return deterministic AST serialization without modifying caller's tree."""
    tree = ast.parse(source_or_node) if isinstance(source_or_node, str) else source_or_node
    tree = IdentifierMasker().visit(copy.deepcopy(tree))
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def fingerprint_python_ast(source_or_node: str | ast.AST) -> str:
    return hashlib.sha256(normalized_python_ast(source_or_node).encode("utf-8")).hexdigest()


normalize_python_ast = normalized_python_ast
ast_structural_fingerprint = fingerprint_python_ast
