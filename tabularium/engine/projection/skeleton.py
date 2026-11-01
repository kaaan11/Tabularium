"""Deterministic in-memory AST skeleton extractor for projection (Phase 6).

NO IO, NO FILESYSTEM ACCESS, NO FORBIDDEN IMPORTS.
Pure functional AST manipulation using standard library `ast`.
"""

from __future__ import annotations

import ast


class ASTBodyPruner(ast.NodeTransformer):
    """Replaces function bodies with Ellipsis (...) while preserving docstrings."""

    def _prune_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        doc = ast.get_docstring(node)
        new_body: list[ast.stmt] = []
        if doc:
            new_body.append(ast.Expr(value=ast.Constant(value=doc)))
        new_body.append(ast.Expr(value=ast.Constant(value=...)))
        node.body = new_body
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._prune_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self._prune_body(node)


def generate_skeleton(source_code: str) -> str:
    """Generate a valid Python skeleton (stub) with pruned function bodies."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return source_code

    pruner = ASTBodyPruner()
    pruned_tree = pruner.visit(tree)
    ast.fix_missing_locations(pruned_tree)
    try:
        return ast.unparse(pruned_tree)
    except Exception:
        return source_code
