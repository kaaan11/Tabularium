"""Acceptance tests for context discovery record count and size limits."""

from __future__ import annotations

import builtins
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tabularium.diagnostics import diagnose, render_human
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.context_loader import confinement, discovery, load_context
from tabularium.engine.context_loader.discovery import (
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    MAX_TOTAL_BYTES,
    discover,
    read_frontmatter,
)
from tabularium.engine.context_loader.model import BLOCKED, GOVERNED, READY


class TestDiscoveryLimits(unittest.TestCase):
    """Acceptance tests for context discovery record count and size limits."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tabu-limit-test-"))
        self.root = self.tmp / "project"
        self.root.mkdir()
        initialize(self.root)
        # Onboard project to make it valid for status/doctor
        declarations = {
            "title": "Discovery Limits Fixture",
            "purpose": "Exercise record limits",
            "scope": "Discovery limits fixture",
        }
        draft = onboard_project(self.root, declarations)
        assert draft.proposal is not None
        onboard_project(
            self.root,
            declarations,
            build_authorizations(draft.proposal, "alex", "2026-08-19T12:00:00+00:00"),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_task(self, task_id: str, body: str = "", extra_yaml: str = "") -> Path:
        tasks_dir = self.root / ".ai" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        file_path = tasks_dir / f"{task_id}.md"
        content = (
            f"---\n"
            f"id: \"{task_id}\"\n"
            f"title: \"Task {task_id}\"\n"
            f"status: \"INBOX\"\n"
            f"created_at: \"2026-10-30T00:00:00Z\"\n"
            f"{extra_yaml}"
            f"---\n"
            f"{body}\n"
        )
        file_path.write_text(content, encoding="utf-8")
        return file_path

    # 1. Per-record limit
    def test_record_too_large_not_opened_and_blocks_context(self) -> None:
        """Per-record limit: a record of limit + 1 bytes is not opened (verified via spy).
        Diagnostic is RECORD_TOO_LARGE, context is not READY, status shows path and reason.
        """
        task_id = "TASK-2026-0001"
        tasks_dir = self.root / ".ai" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / f"{task_id}.md"

        # File size exactly MAX_RECORD_BYTES + 1
        prefix = (
            f"---\nid: \"{task_id}\"\ntitle: \"T\"\nstatus: \"INBOX\"\n"
            f"created_at: \"2026-10-30T00:00:00Z\"\n---\n"
        ).encode("utf-8")
        needed = (MAX_RECORD_BYTES + 1) - len(prefix)
        self.assertGreater(needed, 0)
        task_file.write_bytes(prefix + b"X" * needed)
        self.assertEqual(task_file.stat().st_size, MAX_RECORD_BYTES + 1)

        real_open = builtins.open
        opened_paths: list[str] = []

        def spy_open(file, *args, **kwargs):
            opened_paths.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=spy_open):
            pkg = load_context(self.root)

        # File should never be opened or read
        self.assertNotIn(str(task_file), opened_paths)

        # Diagnostic should be RECORD_TOO_LARGE
        codes = [d.code for d in pkg.diagnostics]
        self.assertIn("RECORD_TOO_LARGE", codes)

        # Context must not be READY (must be BLOCKED)
        self.assertNotEqual(pkg.package_status, READY)
        self.assertEqual(pkg.package_status, BLOCKED)

        # tabu status should show path and reason
        status_result = diagnose(self.root, "status")
        rendered = render_human(status_result)
        self.assertIn(task_file.name, rendered)
        self.assertIn("RECORD_TOO_LARGE", rendered)
        self.assertIn("record exceeds maximum allowed size", rendered)

    # 2. Record count limit
    def test_record_limit_exceeded_excess_not_read(self) -> None:
        """Record count limit: when lowered in tests (e.g. 5), RECORD_LIMIT_EXCEEDED is raised;
        excess files are not read.
        """
        # Create 7 Task records
        task_files: list[Path] = []
        for i in range(1, 8):
            task_files.append(self._write_task(f"TASK-2026-{i:04d}", body=f"Body {i}"))

        limit = 5
        real_open = builtins.open
        opened_files: list[str] = []

        def spy_open(file, *args, **kwargs):
            opened_files.append(str(file))
            return real_open(file, *args, **kwargs)

        with mock.patch.object(discovery, "MAX_RECORDS", limit), \
             mock.patch("builtins.open", side_effect=spy_open):
            pkg = load_context(self.root)

        # Diagnostic should be RECORD_LIMIT_EXCEEDED
        codes = [d.code for d in pkg.diagnostics]
        self.assertIn("RECORD_LIMIT_EXCEEDED", codes)

        # Context must not be READY
        self.assertNotEqual(pkg.package_status, READY)

        # Excess files (6th and 7th records) must not be read
        excess_files = [str(task_files[5]), str(task_files[6])]
        for excess in excess_files:
            self.assertNotIn(excess, opened_files, f"Excess file {excess} should not have been opened!")

    # 3. Total byte limit
    def test_total_bytes_exceeded(self) -> None:
        """Total byte limit: when lowered in tests, TOTAL_BYTES_EXCEEDED is raised."""
        # Create 4 Tasks of ~250 bytes each
        for i in range(1, 5):
            self._write_task(f"TASK-2026-{i:04d}", body="A" * 150)

        # Total size is ~1000 bytes for 4 files; set limit to 500 bytes
        with mock.patch.object(discovery, "MAX_TOTAL_BYTES", 500):
            pkg = load_context(self.root)

        codes = [d.code for d in pkg.diagnostics]
        self.assertIn("TOTAL_BYTES_EXCEEDED", codes)
        self.assertNotEqual(pkg.package_status, READY)

    # 4. Growth during read
    def test_growth_during_read_stops_at_limit_plus_one_and_rejects(self) -> None:
        """Growth during read: if stat returns small but file is large (via stat patch),
        reading stops at limit + 1 and record is rejected.
        """
        task_id = "TASK-2026-0001"
        body_large = "Z" * (MAX_RECORD_BYTES + 2048)
        task_file = self._write_task(task_id, body=body_large)

        real_stat = Path.stat

        class FakeStat:
            def __init__(self, original_stat):
                self._orig = original_stat
                self.st_size = 200  # stat returns small size

            def __getattr__(self, item):
                return getattr(self._orig, item)

        def patched_stat(path_self):
            orig = real_stat(path_self)
            if path_self == task_file:
                return FakeStat(orig)
            return orig

        with mock.patch.object(Path, "stat", patched_stat):
            rec = read_frontmatter(task_file, self.root, "Task")

        # Reading must stop at limit + 1
        self.assertEqual(rec.bytes_read, MAX_RECORD_BYTES + 1)
        # Record must be rejected
        self.assertTrue(rec.malformed)
        self.assertFalse(rec.usable)
        self.assertEqual(rec.error_code, "RECORD_TOO_LARGE")
        self.assertEqual(rec.body, "")

    # 5. Normal project
    def test_normal_project_demo_remains_ready(self) -> None:
        """Normal project: the shipped examples/demo-project remains READY.
        A real onboarded project with records stays clear of the limits.
        """
        demo_root = Path(__file__).resolve().parents[1] / "examples" / "demo-project"
        pkg = load_context(demo_root)
        self.assertEqual(pkg.entry_mode, GOVERNED)
        self.assertEqual(pkg.package_status, READY)
        limit_codes = {"RECORD_TOO_LARGE", "RECORD_LIMIT_EXCEEDED", "TOTAL_BYTES_EXCEEDED"}
        self.assertFalse(any(d.code in limit_codes for d in pkg.diagnostics))
        # The demo is a fixture users copy: it must load without any warning,
        # and it must actually carry records for the limits to be exercised.
        self.assertEqual([d.code for d in pkg.diagnostics], [])
        repo = discover(demo_root, confinement.ProjectScope(demo_root))
        self.assertGreaterEqual(len(repo.all_records), 4)

    # 6. Memory
    def test_memory_two_mib_record_body_empty(self) -> None:
        """Memory: retained body after discover for a 2 MiB record is empty or None."""
        task_id = "TASK-2026-0001"
        body_2mib = "B" * (2 * 1024 * 1024)
        self._write_task(task_id, body=body_2mib)

        scope = confinement.ProjectScope(self.root)
        repo = discover(self.root, scope)

        matching = [r for r in repo.all_records if r.filename_id == task_id]
        self.assertEqual(len(matching), 1)
        rec = matching[0]

        # Retained body is empty or None
        self.assertTrue(rec.body == "" or rec.body is None)
        self.assertTrue(rec.malformed)
        self.assertEqual(rec.error_code, "RECORD_TOO_LARGE")


class TestNonRegularRecord(unittest.TestCase):
    """A FIFO among the records must not block discovery."""

    def test_fifo_record_is_refused_without_opening(self) -> None:
        import os as _os
        import tempfile as _tf
        from pathlib import Path as _P
        from tabularium.init import initialize
        from tabularium.engine.context_loader.confinement import ProjectScope
        from tabularium.engine.context_loader.discovery import discover
        with _tf.TemporaryDirectory() as raw:
            root = _P(raw).resolve()
            initialize(root)
            _os.mkfifo(root / ".ai" / "tasks" / "TASK-2026-0001.md")
            import threading
            fifo = root / ".ai" / "tasks" / "TASK-2026-0001.md"
            result: list = []
            worker = threading.Thread(target=lambda: result.append(discover(root, ProjectScope(root))),
                                      daemon=True)
            worker.start()
            worker.join(10)
            if worker.is_alive():
                # Unblock the reader so the daemon thread can finish, then fail.
                with open(fifo, "wb"):
                    pass
                self.fail("discover() blocked on a FIFO record")
            repo = result[0]
            bad = [r for r in repo.all_records if r.path.name == "TASK-2026-0001.md"]
            self.assertTrue(bad and bad[0].malformed)
            self.assertEqual(bad[0].malformed_reason, "not a regular file")


class TestDiscoveryEdgeCases(unittest.TestCase):
    """Discovery edge cases: partial task listing refusal, diagnostic precedence, and symlink refusal."""

    def setUp(self) -> None:
        import tempfile as _tf
        from tabularium.init import initialize
        self._holder = _tf.TemporaryDirectory()
        self.addCleanup(self._holder.cleanup)
        from pathlib import Path as _P
        self.root = _P(self._holder.name).resolve()
        initialize(self.root)
        self.tasks = self.root / ".ai" / "tasks"

    def _task(self, n: int, extra: bytes = b"") -> None:
        (self.tasks / f"TASK-2026-{n:04d}.md").write_bytes(
            (f"---\nid: \"TASK-2026-{n:04d}\"\ntitle: \"T\"\nstatus: \"INBOX\"\n"
             f"created_at: \"2026-10-30T00:00:00Z\"\n---\n").encode() + extra)

    def test_task_list_refuses_a_partial_listing(self) -> None:
        from tabularium.task import task_list
        for n in range(1, 8):
            self._task(n)
        with mock.patch("tabularium.engine.context_loader.discovery.MAX_RECORDS", 3):
            result = task_list(self.root)
        self.assertEqual(result.code, "RECORD_LIMIT_EXCEEDED")
        self.assertEqual(len(result.tasks), 0)

    def test_single_huge_record_is_record_too_large_not_total(self) -> None:
        from tabularium.engine.context_loader.confinement import ProjectScope
        from tabularium.engine.context_loader.discovery import discover
        self._task(1, b"x" * 300)
        with mock.patch("tabularium.engine.context_loader.discovery.MAX_RECORD_BYTES", 100), \
             mock.patch("tabularium.engine.context_loader.discovery.MAX_TOTAL_BYTES", 200):
            repo = discover(self.root, ProjectScope(self.root))
        codes = [d[0] for d in repo.diagnostics]
        self.assertIn("RECORD_TOO_LARGE", codes)
        self.assertNotIn("TOTAL_BYTES_EXCEEDED", codes)

    def test_symlinked_record_is_not_opened_through_the_link(self) -> None:
        from tabularium.engine.context_loader.confinement import ProjectScope
        from tabularium.engine.context_loader.discovery import discover
        self._task(1)
        real = self.tasks / "TASK-2026-0001.md"
        hidden = self.root / ".ai" / "tasks" / "real-body.txt"
        real.rename(hidden)
        os.symlink("real-body.txt", real)        # stat() follows it and sees a regular file
        repo = discover(self.root, ProjectScope(self.root))
        recs = [r for r in repo.all_records if r.path.name == "TASK-2026-0001.md"]
        self.assertTrue(recs and recs[0].malformed, "a symlinked record is refused")
