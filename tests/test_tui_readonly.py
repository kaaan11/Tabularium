"""Read-only TUI tests (Faz A, DRL-026 narrow trigger).

The dashboard renders existing read-only surfaces and nothing else. These
tests hold three properties: rendering is pure, loading writes nothing,
and no terminal dependency leaks into the importable package.
"""

from __future__ import annotations

import ast
import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tabularium.engine.tui.app import run
from tabularium.engine.tui.model import (
    CapabilityRow,
    DashboardSnapshot,
    GateRow,
    TaskRow,
    build_snapshot,
)
from tabularium.engine.tui.render import render_dashboard


def _snapshot(**overrides) -> DashboardSnapshot:
    base = {
        "tabularium_version": "0.3.0.dev0",
        "overall_state": "READY",
        "capabilities": (CapabilityRow("runtime", True), CapabilityRow("api", False)),
        "task_selection": "TASK-2026-0001",
        "tasks": (TaskRow("TASK-2026-0001", "IN_PROGRESS", "Do work", "ACTIVE"),),
        "gates": (GateRow("project_health", "PASSED"),),
        "instruction": "Do work",
    }
    base.update(overrides)
    return DashboardSnapshot(**base)


class RenderTests(unittest.TestCase):
    def test_dashboard_shows_all_panes(self):
        text = render_dashboard(_snapshot())
        self.assertIn("Tabularium 0.3.0.dev0 — READY (read-only)", text)
        self.assertIn("Task selection: TASK-2026-0001", text)
        self.assertIn("[ok] runtime", text)
        self.assertIn("[--] api", text)
        self.assertIn("TASK-2026-0001 [IN_PROGRESS] Do work", text)
        self.assertIn("project_health: PASSED", text)

    def test_empty_tasks_render_creation_hint(self):
        text = render_dashboard(_snapshot(tasks=(), task_selection=None))
        self.assertIn("Tasks (0):", text)
        self.assertIn("tabu task create", text)
        self.assertIn("Task selection: none", text)

    def test_no_instruction_omits_preflight_pane(self):
        text = render_dashboard(_snapshot(gates=(), instruction=None))
        self.assertNotIn("Preflight:", text)


class ReadOnlyTests(unittest.TestCase):
    def _fingerprint(self, root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
        return out

    def test_snapshot_and_run_write_nothing(self):
        from tabularium.init import initialize

        with tempfile.TemporaryDirectory(prefix="tabu-tui-") as raw:
            root = Path(raw)
            self.assertEqual(initialize(root).status, "INITIALIZED")
            before = self._fingerprint(root)
            snapshot = build_snapshot(root)
            # Initialized but not onboarded: the dashboard shows the
            # diagnostic state without repairing anything.
            self.assertEqual(snapshot.overall_state, "NOT_ONBOARDED")
            self.assertEqual(snapshot.tasks, ())
            buffer = io.StringIO()
            buffer.isatty = lambda: False  # type: ignore[method-assign]
            with redirect_stdout(buffer):
                self.assertEqual(run(root), 0)
            self.assertIn("(read-only)", buffer.getvalue())
            self.assertEqual(self._fingerprint(root), before)


class NoTerminalDependencyTests(unittest.TestCase):
    def test_no_top_level_curses_import(self):
        package = Path(__file__).resolve().parents[1] / "tabularium" / "engine" / "tui"
        for module in ("__init__.py", "model.py", "render.py", "app.py"):
            tree = ast.parse((package / module).read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(a.name == "curses" for a in node.names),
                        f"{module} imports curses at top level")
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "curses",
                                        f"{module} imports curses at top level")


if __name__ == "__main__":
    unittest.main()
