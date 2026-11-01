"""Deterministic AST code indexing, symbol extraction, and selection (Phase 6)."""

from tabularium.engine.projection.skeleton import ASTBodyPruner, generate_skeleton
from .indexer import CodeIndexer, FileIndex, SymbolInfo, parse_file_symbols
from .selector import CodeSelectionResult, CodeSelector, SelectedCodeUnit

__all__ = [
    "CodeIndexer",
    "FileIndex",
    "SymbolInfo",
    "parse_file_symbols",
    "CodeSelector",
    "SelectedCodeUnit",
    "CodeSelectionResult",
    "generate_skeleton",
    "ASTBodyPruner",
]
