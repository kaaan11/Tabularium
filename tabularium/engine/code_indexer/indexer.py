"""Deterministic AST code indexer and structural symbol extractor (Phase 6).

Extracts symbols (classes, functions, imports) from Python source files without LLMs.
Supports semantic body pruning (stub projection) to fit large codebases into token budgets.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tabularium.engine.projection.skeleton import generate_skeleton


@dataclass(frozen=True)
class SymbolInfo:
    """One symbol declaration in an AST."""

    name: str
    kind: str  # "class", "function", "async_function"
    line_start: int
    line_end: int
    signature: str = ""
    docstring: str = ""


@dataclass(frozen=True)
class FileIndex:
    """Indexed symbols and imports for a single source file."""

    file_path: str
    imports: tuple[str, ...] = ()
    symbols: tuple[SymbolInfo, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "imports": list(self.imports),
            "symbols": [asdict(s) for s in self.symbols],
        }


def parse_file_symbols(file_path: str, source_code: str) -> FileIndex:
    """Parse a single Python source string into a structured FileIndex."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return FileIndex(file_path=file_path)

    imports: list[str] = []
    symbols: list[SymbolInfo] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {mod} import {names}")
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            method_names = [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            sig = f"class {node.name}(methods: {', '.join(method_names[:5])})"
            symbols.append(SymbolInfo(
                name=node.name,
                kind="class",
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                signature=sig,
                docstring=doc.split("\n", 1)[0] if doc else "",
            ))
        elif isinstance(node, ast.FunctionDef):
            doc = ast.get_docstring(node) or ""
            args = [a.arg for a in node.args.args]
            sig = f"def {node.name}({', '.join(args)})"
            symbols.append(SymbolInfo(
                name=node.name,
                kind="function",
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                signature=sig,
                docstring=doc.split("\n", 1)[0] if doc else "",
            ))
        elif isinstance(node, ast.AsyncFunctionDef):
            doc = ast.get_docstring(node) or ""
            args = [a.arg for a in node.args.args]
            sig = f"async def {node.name}({', '.join(args)})"
            symbols.append(SymbolInfo(
                name=node.name,
                kind="async_function",
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                signature=sig,
                docstring=doc.split("\n", 1)[0] if doc else "",
            ))

    return FileIndex(
        file_path=file_path,
        imports=tuple(imports),
        symbols=tuple(symbols),
    )


class CodeIndexer:
    """Project-level code indexer extracting AST metadata across repository files."""

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()

    def index_files(self, relative_paths: list[str] | tuple[str, ...]) -> dict[str, FileIndex]:
        results: dict[str, FileIndex] = {}
        for rel in relative_paths:
            path = self.root / rel
            if path.is_file() and path.suffix == ".py":
                try:
                    text = path.read_text(encoding="utf-8")
                    results[rel] = parse_file_symbols(rel, text)
                except (OSError, UnicodeDecodeError):
                    continue
        return results
