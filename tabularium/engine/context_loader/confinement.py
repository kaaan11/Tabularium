"""Central project-context confinement (F01).

Everything read as Tabularium project context must canonically resolve to the
project's own `.ai` root or a true descendant of it. Symlinks, junctions,
reparse points, `..` traversal, and absolute external paths are all defeated by
resolving first and comparing path components afterwards.

Containment is never decided by string prefix: `C:\\project\\.ai-evil` is not
inside `C:\\project\\.ai`.
"""

from __future__ import annotations

from pathlib import Path


class ProjectScope:
    """The single authority on whether a path may be read as project context."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.ai_dir = self.project_root / ".ai"
        try:
            # resolve() follows symlinks, junctions, and reparse points
            resolved_ai = self.ai_dir.resolve(strict=True)
            # The positional anchor itself is not redirectable.  It must be
            # the real direct `.ai` child of the canonical caller project,
            # rather than a junction to another project or to a nested alias.
            self.ai_root: Path | None = resolved_ai \
                if resolved_ai.parent == self.project_root \
                and resolved_ai.name == ".ai" else None
        except (OSError, RuntimeError, ValueError):
            self.ai_root = None

    @property
    def available(self) -> bool:
        return self.ai_root is not None and self.ai_root.is_dir()

    def contains(self, path: Path | str) -> bool:
        """True when `path` canonically resolves inside the project .ai root."""
        if self.ai_root is None:
            return False
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return False
        if resolved == self.ai_root:
            return True
        # component-wise containment, not string prefix
        return resolved.is_relative_to(self.ai_root)

    def resolve_in_scope(self, path: Path | str) -> Path | None:
        """Return the path when it is confined, else None. Never reads."""
        p = Path(path)
        return p if self.contains(p) else None

    def entry(self, relative: str) -> Path | None:
        """A named `.ai` entry (e.g. 'manifest.yaml', 'tasks'), when confined.

        A missing entry returns None too: callers distinguish absence from
        escape by testing existence separately.
        """
        if self.ai_root is None:
            return None
        candidate = self.ai_dir / relative
        return candidate if self.contains(candidate) else None

    def escaped(self, relative: str) -> bool:
        """True when the entry exists on disk but resolves outside the scope."""
        candidate = self.ai_dir / relative
        try:
            exists = candidate.exists()
        except (OSError, ValueError):
            return True
        return bool(exists) and not self.contains(candidate)

    def rel(self, path: Path) -> str:
        """A stable, reportable path label for diagnostics."""
        try:
            return Path(path).relative_to(self.project_root).as_posix()
        except ValueError:
            return Path(path).as_posix()
