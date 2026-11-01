"""Tests for Phase 6: Semantic AST projection and code budget."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from tabularium.engine.code_indexer import (CodeIndexer, CodeSelector,
                                  generate_skeleton, parse_file_symbols)
from tabularium.engine.projection import (CODE, DEFAULT_LIMITS, HOT, project,
                                resolve_profile)
from tests.test_context_loader import Fixture


class TestASTSkeleton(unittest.TestCase):
    """Test AST body pruning / stub generator."""

    def test_generate_skeleton_prunes_function_bodies_preserving_signatures_and_docstrings(self):
        source = '''def calculate_metrics(alpha: float, beta: int = 10) -> float:
    """Compute weighted accuracy."""
    x = alpha * 2
    y = x + beta
    return y / 3.0
'''
        skeleton = generate_skeleton(source)
        # Must parse cleanly as valid Python
        tree = ast.parse(skeleton)
        self.assertEqual(len(tree.body), 1)
        func = tree.body[0]
        self.assertIsInstance(func, ast.FunctionDef)
        self.assertEqual(func.name, "calculate_metrics")
        self.assertEqual(ast.get_docstring(func), "Compute weighted accuracy.")
        # Body statements: docstring + Ellipsis (...)
        self.assertIn("...", skeleton)
        self.assertNotIn("alpha * 2", skeleton)

    def test_generate_skeleton_handles_classes_and_async_methods(self):
        source = '''class WorkerEngine:
    """Core runtime worker."""

    async def execute_task(self, task_id: str) -> bool:
        """Run the task asynchronously."""
        await do_work(task_id)
        return True
'''
        skeleton = generate_skeleton(source)
        tree = ast.parse(skeleton)
        cls = tree.body[0]
        self.assertIsInstance(cls, ast.ClassDef)
        self.assertEqual(cls.name, "WorkerEngine")
        self.assertIn("async def execute_task", skeleton)
        self.assertIn("...", skeleton)
        self.assertNotIn("do_work(task_id)", skeleton)


class TestCodeIndexer(unittest.TestCase):
    """Test repository-level AST indexer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.indexer = CodeIndexer(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_index_files_extracts_symbols_and_imports(self):
        file_path = self.root / "sample_module.py"
        file_path.write_text(
            "import os\n"
            "from typing import Any, List\n\n"
            "class SampleClass:\n"
            '    """Docstring for SampleClass."""\n'
            "    def method_one(self):\n"
            "        pass\n\n"
            "def sample_function(x: int) -> int:\n"
            '    """Function doc."""\n'
            "    return x * 2\n",
            encoding="utf-8",
        )

        indexed = self.indexer.index_files(["sample_module.py"])
        self.assertIn("sample_module.py", indexed)
        info = indexed["sample_module.py"]

        self.assertIn("import os", info.imports)
        self.assertIn("from typing import Any, List", info.imports)

        symbol_names = [s.name for s in info.symbols]
        self.assertIn("SampleClass", symbol_names)
        self.assertIn("sample_function", symbol_names)


class TestCodeSelector(unittest.TestCase):
    """Test task-aware code selection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.selector = CodeSelector(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_select_for_task_finds_referenced_files(self):
        file_one = self.root / "aios" / "engine.py"
        file_one.parent.mkdir(parents=True, exist_ok=True)
        file_one.write_text("def run():\n    return 42\n", encoding="utf-8")

        task_meta = {
            "title": "Fix engine bug",
            "objective": "Modify aios/engine.py to handle timeout",
        }
        res = self.selector.select_for_task(task_meta)
        self.assertEqual(len(res.units), 1)
        self.assertEqual(res.units[0].file_path, "aios/engine.py")
        self.assertIn("def run", res.units[0].skeleton)


class TestCodeProjectionBudget(unittest.TestCase):
    """Test CODE budget profile and code_context projection."""

    def setUp(self):
        self.fx = Fixture()
        self.fx.task("TASK-2026-0001", objective="Inspect code context.")
        self.addCleanup(self.fx.cleanup)

    def test_code_profile_resolution(self):
        prof = resolve_profile(CODE)
        self.assertIsNotNone(prof)
        self.assertEqual(prof.limit_tokens, 8000)
        self.assertIn(CODE, DEFAULT_LIMITS)

    def test_project_with_code_context(self):
        pkg = self.fx.load(task="TASK-2026-0001")
        pkg_dict = pkg.to_dict()
        pkg_dict["code_context"] = {
            "files": [
                {
                    "path": "aios/engine.py",
                    "code": "def process():\n    return 'done'\n",
                    "is_skeleton": False,
                }
            ]
        }

        result = project(pkg_dict, intent="continue task", profile=CODE)
        self.assertEqual(result.outcome, "PROJECTED")
        self.assertIsNotNone(result.payload)
        payload_dict = result.payload.to_dict()
        self.assertIn("code_context", payload_dict)
        files = payload_dict["code_context"]["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["path"], "aios/engine.py")
        self.assertIn("def process", files[0]["code"])

    def test_determinism_with_code_context(self):
        pkg = self.fx.load(task="TASK-2026-0001")
        pkg_dict = pkg.to_dict()
        pkg_dict["code_context"] = {
            "files": [
                {
                    "path": "aios/engine.py",
                    "code": "def process():\n    return 'done'\n",
                    "is_skeleton": False,
                }
            ]
        }
        res1 = project(pkg_dict, intent="continue task", profile=CODE)
        res2 = project(pkg_dict, intent="continue task", profile=CODE)
        self.assertEqual(res1.to_json(), res2.to_json())


if __name__ == "__main__":
    unittest.main()
