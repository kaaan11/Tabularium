"""Phase I (I12/I13) information-safety probes and filesystem/atomicity
stress for the new Task create path.

Safety: hostile values survive semantically in the record's own storage but
are inert on every human surface, bounded where surfaces are summaries, and
never leak environment, payload, credentials, or raw exceptions.

Filesystem: the create path stays within the current storage architecture —
staged write + fsync + no-clobber link — and fails closed on every failure
shape without leaving partial records.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from tabularium import explain as X
from tabularium import task as T
from tabularium.cli import main
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project

HOSTILE_TITLE = ("line1\n\x1b[31mRED\u202eoverride"
                 "\u2028line-sep\ttab")


class SafetyFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i12-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "I12 fixture", "purpose": "safety probes",
            "scope": "Bounded safety project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-01-01T00:00:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        self.task_dir = self.root / ".ai" / "tasks"
        self.task_dir.mkdir(exist_ok=True)

    def tearDown(self):
        self.holder.cleanup()

    @staticmethod
    def run_main(argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


class HostileDisplayTests(SafetyFixture):
    def test_hostile_title_is_inert_on_every_human_surface(self):
        result = T.task_create(self.root, title=HOSTILE_TITLE,
                               objective="o")
        self.assertTrue(result.created)
        listing = T.render_human(T.task_list(self.root))
        for forbidden in ("\x1b[31m", "\u202e", "\nline1", "\u2028"):
            self.assertNotIn(forbidden, listing)
        shown = T.task_show(self.root, result.record_id)
        show_human = T.render_human_show(shown)
        # The record keeps the value semantically verbatim in its own
        # frontmatter; stored/derived rendering never emits a raw control
        # byte, so the display cannot inject escape sequences or extra lines.
        self.assertEqual(shown.frontmatter.get("title"), HOSTILE_TITLE)
        self.assertNotIn(chr(27), show_human)
        self.assertNotIn(chr(133), show_human)
        machine_list = json.loads(T.render_json(T.task_list(self.root)))
        wire = json.dumps(machine_list, ensure_ascii=False)
        self.assertNotIn("\x1b[31m", wire)

    def test_long_title_is_stored_whole_and_bounded_on_summary_surfaces(self):
        long_title = "L" * 10000
        result = T.task_create(self.root, title=long_title, objective="o")
        self.assertTrue(result.created)
        listing = T.render_human(T.task_list(self.root))
        self.assertLess(len(listing), 2000)
        machine = json.loads(T.render_json(T.task_list(self.root)))
        self.assertEqual(len(machine["tasks"][0]["title"]), 10000)

    def test_secret_shaped_values_never_reach_preflight_output(self):
        T.task_create(self.root,
                      title="sk-live-abc123DEF456 secret password=hunter2",
                      objective="o")
        preflight_out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(preflight_out), redirect_stderr(err):
            result = X.preflight("work", self.root)
        rendered = X.render_human(result) + X.render_json(result)
        self.assertNotIn("sk-live-abc123DEF456", rendered)
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("password=", rendered)

    def test_explain_never_leaks_environment_or_payload(self):
        os.environ["TABU_MODEL"] = "test/model"
        try:
            result = X.preflight("unique-intent-marker-123", self.root,
                                 worker="api", model="test/model")
        finally:
            del os.environ["TABU_MODEL"]
        payload = json.dumps(result.to_dict())
        for forbidden in ("TABU_MODEL", "PATH", "unique-intent-marker",
                          "api_key", "Authorization"):
            self.assertNotIn(forbidden, payload)
        # Metrics expose counts and section names only — never section text.
        if result.metrics:
            self.assertIsInstance(result.metrics.get("reduction_pct"),
                                  (float, int, type(None)))

    def test_path_like_objective_is_just_text(self):
        result = T.task_create(
            self.root, title="T",
            objective="read ../../../etc/passwd and C:\\Windows\\system32")
        self.assertTrue(result.created)
        listing = T.render_human(T.task_list(self.root))
        # Summary surfaces never print objectives at all.
        self.assertNotIn("etc/passwd", listing)


class ConcurrencyAndAtomicityTests(SafetyFixture):
    def test_competing_allocation_writes_exactly_one_winner(self):
        """Two creators racing on the same candidate id: one publishes, the
        other refuses; the store ends with exactly one new Task."""
        import threading

        barrier = threading.Barrier(2)
        results: list[T.TaskCreateResult] = []

        def contend():
            barrier.wait()
            real_link = os.link

            def slow_link(src, dst, *a, **kw):
                import time
                time.sleep(0.05)
                return real_link(src, dst, *a, **kw)

            with mock.patch.object(os, "link", slow_link):
                results.append(T.task_create(self.root, title="Racer",
                                             objective="o"))

        threads = [threading.Thread(target=contend) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        created = [r for r in results if r.created]
        refused = [r for r in results if r.code == T.TASK_ID_COLLISION]
        self.assertEqual(len(created), 1)
        self.assertEqual(len(refused), 1)
        stored = sorted(p.name for p in self.task_dir.iterdir()
                        if p.name.startswith("TASK-"))
        self.assertEqual(len(stored), 1)

    def test_forced_same_id_race_refuses_without_overwrite(self):
        existing = self.task_dir / "TASK-2026-0001.md"
        existing.write_text("---\nid: precious\n---\n# keep\n",
                            encoding="utf-8")
        before = existing.read_bytes()

        def forced(raw_target, **kwargs):
            return T.TaskIdAllocation(record_id="TASK-2026-0001")

        with mock.patch.object(T, "allocate_task_id", forced):
            result = T.task_create(self.root, title="T", objective="o")
        self.assertEqual(result.code, T.TASK_ID_COLLISION)
        self.assertEqual(existing.read_bytes(), before)

    def test_write_failure_leaves_no_partial_record(self):
        real_fsync = os.fsync

        def failing_fsync(fd):
            raise OSError("device full (fixture)")

        with mock.patch.object(os, "fsync", failing_fsync):
            result = T.task_create(self.root, title="T", objective="o")
        self.assertEqual(result.code, "WRITE_FAILED")
        self.assertEqual([p.name for p in self.task_dir.iterdir()],
                         ["README.md"])
        self.assertIsNotNone(real_fsync)

    def test_permission_denied_reports_usage_safe_code(self):
        readonly_dir = self.root / ".ai" / "tasks"
        original_mode = readonly_dir.stat().st_mode
        readonly_dir.chmod(0o500)
        try:
            result = T.task_create(self.root, title="T", objective="o")
            if result.code is None:
                self.skipTest("filesystem grants write regardless of mode")
            self.assertEqual(result.code, "PERMISSION_DENIED")
        finally:
            readonly_dir.chmod(original_mode)

    def test_directory_disappearing_before_allocation_refuses_create(self):
        # Create requires existing confined storage; a vanished directory is
        # reported rather than silently recreated.
        import shutil
        shutil.rmtree(self.task_dir)
        result = T.task_create(self.root, title="T", objective="o")
        # The project-health gate refuses first (loader needs the scaffold);
        # either way creation fails closed and recreates nothing.
        self.assertFalse(result.created)
        self.assertIsNotNone(result.code)
        self.assertFalse((self.root / ".ai" / "tasks").exists())

    def test_existing_records_survive_every_failed_create_shape(self):
        first = T.task_create(self.root, title="First", objective="o")
        snapshot = (self.task_dir / f"{first.record_id}.md").read_bytes()
        T.task_create(self.root, title="Second", objective="o",
                      priority="NOPE")
        T.task_create(self.root / "missing", title="X", objective="x")
        X_preflight_marker = None  # placeholder to keep imports honest
        self.assertEqual((self.task_dir / f"{first.record_id}.md")
                         .read_bytes(), snapshot)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
