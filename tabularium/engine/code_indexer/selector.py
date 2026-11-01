"""Deterministic code selector for Task-aware AST projection (Phase 6)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tabularium.engine.projection.skeleton import generate_skeleton

_PATH_PATTERN = re.compile(r"\b([A-Za-z0-9_\-./]+\.py)\b")


@dataclass(frozen=True)
class SelectedCodeUnit:
    file_path: str
    skeleton: str
    full_body: str
    char_count: int


@dataclass(frozen=True)
class CodeSelectionResult:
    units: tuple[SelectedCodeUnit, ...] = ()

    def to_dict(self, full_body: bool = False) -> dict[str, Any]:
        return {
            "files": [
                {
                    "path": u.file_path,
                    "code": u.full_body if full_body else u.skeleton,
                    "is_skeleton": not full_body,
                }
                for u in self.units
            ]
        }


class CodeSelector:
    """Selects relevant code files and produces skeletons for Task context."""

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).resolve()

    def select_for_task(self, task_metadata: dict[str, Any], task_body: str = "", intent: str = "") -> CodeSelectionResult:
        """Find relevant python files mentioned in task objective, body, or intent."""
        search_text = f"{task_metadata.get('title', '')} {task_metadata.get('objective', '')} {task_body} {intent}"
        candidates = set(_PATH_PATTERN.findall(search_text))

        # Also inspect scope if specified
        scope = task_metadata.get("scope") or {}
        if isinstance(scope, dict):
            for f in scope.get("files", ()):
                if isinstance(f, str) and f.endswith(".py"):
                    candidates.add(f)

        units: list[SelectedCodeUnit] = []
        for cand in sorted(candidates):
            norm = cand.lstrip("./")
            p = (self.root / norm).resolve()
            if p.is_file() and p.is_relative_to(self.root):
                try:
                    text = p.read_text(encoding="utf-8")
                    skel = generate_skeleton(text)
                    units.append(SelectedCodeUnit(
                        file_path=norm,
                        skeleton=skel,
                        full_body=text,
                        char_count=len(skel),
                    ))
                except (OSError, UnicodeDecodeError):
                    continue

        return CodeSelectionResult(units=tuple(units))
