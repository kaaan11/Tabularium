"""Acceptance tests for checkpoint and trace directory security.

Verifies:
1. tabu explain and tabu run --dry-run create NOTHING under TMPDIR.
2. Real runs create directories mode 0700 and .db / .execution mode 0600 (even under umask 0002).
3. Pre-existing 0755 user directory is narrowed to 0700 with a notice on stderr; run proceeds.
4. Symlink or foreign uid directory fails-closed with CHECKPOINT_DIR_UNSAFE; writes nothing.
5. Counterparts for traces directory and runner.
6. Existing continue_execution and run workflows work cleanly.
"""

from __future__ import annotations

import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium import cli
from tabularium.execution import (
    CHECKPOINT_DIR_UNSAFE,
    _checkpoint_path,
    continue_execution,
    make_resolution,
    run_instruction,
)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.storage import (
    CheckpointDirUnsafe,
    prepare_checkpoint_storage,
    prepare_trace_storage,
    verify_checkpoint_storage_if_exists,
)
from tabularium.engine.tui import runner
from tabularium.engine.workers import MockWorker


def _setup_project(root: Path) -> None:
    initialize(root)
    declarations = {
        "title": "Security test project",
        "purpose": "Verify safe checkpoint permissions",
        "scope": "Bounded test suite",
    }
    draft = onboard_project(root, declarations)
    res = onboard_project(
        root,
        declarations,
        build_authorizations(draft.proposal, "human:user", "2026-10-30T00:00:00+00:00"),
    )
    assert res.status == "ONBOARDED"

    task_dir = root / ".ai" / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_file = task_dir / "TASK-2026-0001.md"
    task_file.write_text(
        "---\n"
        "schema_version: '1.10'\n"
        "id: TASK-2026-0001\n"
        "title: Security Check\n"
        "objective: Safe directories\n"
        "status: IN_PROGRESS\n"
        "created_at: '2026-10-30T00:00:00Z'\n"
        "created_by: USER\n"
        "---\n"
        "# Security Check\n",
        encoding="utf-8",
    )


class ControlledEnvironmentTestCase(unittest.TestCase):
    """Base test case managing an isolated TMPDIR and restored umask."""

    def setUp(self):
        # 1. Create project in its own directory first, before altering TMPDIR
        self.proj_holder = tempfile.TemporaryDirectory(prefix="tabu-sec-proj-")
        self.root = Path(self.proj_holder.name)
        _setup_project(self.root)

        # 2. Create isolated TMPDIR specifically for checkpoints and traces
        self._orig_tmpdir = os.environ.get("TMPDIR")
        self._orig_tempfile_tempdir = tempfile.tempdir
        self.tmp_holder = tempfile.TemporaryDirectory(prefix="tabu-sec-tmp-")
        self.isolated_tmp = Path(self.tmp_holder.name)
        os.environ["TMPDIR"] = str(self.isolated_tmp)
        tempfile.tempdir = str(self.isolated_tmp)

        self._orig_umask = os.umask(0o022)

        # Worker discovery depends on the machine (PATH, ~/.opencode). Pin it:
        # no CLI worker, an API model, so explain reaches would-run everywhere.
        for name in ("find_native_executable", "find_opencode_executable"):
            patcher = patch(f"tabularium.execution.{name}", return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"TABU_MODEL": "test-provider/test-model"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        os.umask(self._orig_umask)
        if self._orig_tmpdir is not None:
            os.environ["TMPDIR"] = self._orig_tmpdir
        else:
            os.environ.pop("TMPDIR", None)
        tempfile.tempdir = self._orig_tempfile_tempdir
        self.proj_holder.cleanup()
        self.tmp_holder.cleanup()


class AcceptanceTest1ExplainDryRunSideEffectFree(ControlledEnvironmentTestCase):
    """Acceptance Test 1: explain and run --dry-run create NOTHING under TMPDIR."""

    def test_explain_and_dry_run_produce_no_filesystem_entries(self):
        for umask_val in (0o022, 0o002):
            with self.subTest(umask=oct(umask_val)):
                os.umask(umask_val)
                # Ensure isolated TMPDIR is completely empty
                for entry in list(self.isolated_tmp.iterdir()):
                    if entry.is_dir():
                        import shutil
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                self.assertEqual(list(self.isolated_tmp.iterdir()), [])

                # 1. CLI main(["explain", ...])
                code = cli.main(["explain", "Verify safety", str(self.root)])
                self.assertEqual(code, 0)
                self.assertEqual(
                    list(self.isolated_tmp.iterdir()), [],
                    f"explain CLI left entries in {self.isolated_tmp}: {list(self.isolated_tmp.iterdir())}"
                )

                # 2. CLI main(["run", ..., "--dry-run"])
                code = cli.main(["run", "Verify safety", str(self.root), "--dry-run"])
                self.assertEqual(code, 0)
                self.assertEqual(
                    list(self.isolated_tmp.iterdir()), [],
                    f"run --dry-run CLI left entries in {self.isolated_tmp}: {list(self.isolated_tmp.iterdir())}"
                )

                # 3. CLI main(["run", ..., "--dry-run", "--json"])
                code = cli.main(["run", "Verify safety", str(self.root), "--dry-run", "--json"])
                self.assertEqual(code, 0)
                self.assertEqual(
                    list(self.isolated_tmp.iterdir()), [],
                    f"run --dry-run --json CLI left entries in {self.isolated_tmp}: {list(self.isolated_tmp.iterdir())}"
                )

                # 4. default_trace_path() calculation produces no entries
                tp = runner.default_trace_path()
                self.assertFalse(tp.exists())
                self.assertEqual(list(self.isolated_tmp.iterdir()), [])


class AcceptanceTest2RealRunPermissions(ControlledEnvironmentTestCase):
    """Acceptance Test 2: Real run creates dirs 0700, db/execution 0600 under any umask."""

    def test_real_run_permissions_under_strict_and_permissive_umask(self):
        for umask_val in (0o022, 0o002):
            with self.subTest(umask=oct(umask_val)):
                os.umask(umask_val)
                res = run_instruction(
                    "Perform real run",
                    self.root,
                    adapter=MockWorker(),
                )
                self.assertEqual(res.status, "COMPLETED")

                root_dir = self.isolated_tmp / "tabularium-os"
                cp_dir = root_dir / "checkpoints"
                self.assertTrue(root_dir.is_dir())
                self.assertTrue(cp_dir.is_dir())

                # Check directory permissions: strictly 0700
                self.assertEqual(
                    stat.S_IMODE(os.lstat(root_dir).st_mode), 0o700,
                    f"tabularium-os mode is {oct(stat.S_IMODE(os.lstat(root_dir).st_mode))}"
                )
                self.assertEqual(
                    stat.S_IMODE(os.lstat(cp_dir).st_mode), 0o700,
                    f"checkpoints mode is {oct(stat.S_IMODE(os.lstat(cp_dir).st_mode))}"
                )

                # Check files: .db and .execution strictly 0600
                db_path = _checkpoint_path(self.root)
                self.assertTrue(db_path.exists())
                self.assertEqual(
                    stat.S_IMODE(os.lstat(db_path).st_mode), 0o600,
                    f"db file mode is {oct(stat.S_IMODE(os.lstat(db_path).st_mode))}"
                )

                marker_path = db_path.with_suffix(".execution")
                self.assertTrue(marker_path.exists())
                self.assertEqual(
                    stat.S_IMODE(os.lstat(marker_path).st_mode), 0o600,
                    f".execution mode is {oct(stat.S_IMODE(os.lstat(marker_path).st_mode))}"
                )


class AcceptanceTest3NarrowingWidePermissions(ControlledEnvironmentTestCase):
    """Acceptance Test 3: Existing 0755 directory is narrowed to 0700 with a stderr notice."""

    def test_existing_0755_dir_and_0644_files_are_narrowed_with_stderr_notice(self):
        root_dir = self.isolated_tmp / "tabularium-os"
        cp_dir = root_dir / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(root_dir, 0o755)
        os.chmod(cp_dir, 0o755)

        db_path = _checkpoint_path(self.root)
        db_path.touch()
        os.chmod(db_path, 0o644)
        marker = db_path.with_suffix(".execution")
        marker.write_text("prior-exec", encoding="utf-8")
        os.chmod(marker, 0o644)

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            res = run_instruction(
                "Run with wide dir",
                self.root,
                adapter=MockWorker(),
            )

        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(stat.S_IMODE(os.lstat(root_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(cp_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(db_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(marker).st_mode), 0o600)

        err_output = stderr_buf.getvalue()
        self.assertIn("Notice: narrowed permissions on", err_output)
        self.assertIn("to 0700", err_output)
        # Verify notice is single-line format
        lines = [line for line in err_output.splitlines() if "narrowed permissions" in line]
        self.assertTrue(len(lines) >= 1)
        for line in lines:
            self.assertTrue(line.startswith("Notice: narrowed permissions on "))


class AcceptanceTest4SymlinkAndForeignUidFailClosed(ControlledEnvironmentTestCase):
    """Acceptance Test 4: Symlink or foreign uid returns CHECKPOINT_DIR_UNSAFE; writes nothing."""

    def test_symlink_directory_refuses_fail_closed(self):
        real_target = self.isolated_tmp / "real_dir"
        real_target.mkdir(mode=0o700)
        link_dir = self.isolated_tmp / "tabularium-os"
        os.symlink(real_target, link_dir)

        res = run_instruction(
            "Attempt with symlink",
            self.root,
            adapter=MockWorker(),
        )

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, CHECKPOINT_DIR_UNSAFE)
        self.assertIn("symlink", res.message.lower())
        # Nothing should be written to real_target or anywhere
        self.assertEqual(list(real_target.iterdir()), [])

    def test_foreign_uid_directory_refuses_fail_closed(self):
        cp_dir = self.isolated_tmp / "tabularium-os" / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)

        orig_stat = os.stat
        foreign_uid = os.getuid() + 999

        def mocked_stat(path, *args, **kwargs):
            st = orig_stat(path, *args, **kwargs)
            if "tabularium-os" in str(path):
                # Return stat_result with foreign st_uid
                return os.stat_result((
                    st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                    foreign_uid, st.st_gid, st.st_size,
                    st.st_atime, st.st_mtime, st.st_ctime
                ))
            return st

        with patch("os.stat", side_effect=mocked_stat):
            res = run_instruction(
                "Attempt with foreign dir",
                self.root,
                adapter=MockWorker(),
            )

        self.assertEqual(res.status, "REFUSED")
        self.assertEqual(res.code, CHECKPOINT_DIR_UNSAFE)
        self.assertIn(str(foreign_uid), res.message)
        # Nothing should be written
        self.assertEqual(list(cp_dir.iterdir()), [])


class AcceptanceTest5TracesCounterparts(ControlledEnvironmentTestCase):
    """Acceptance Test 5: Traces directory follows all 1-4 requirements."""

    def test_trace_default_path_is_side_effect_free(self):
        tp = runner.default_trace_path()
        self.assertFalse(tp.exists())
        self.assertEqual(list(self.isolated_tmp.iterdir()), [])

    def test_trace_permissions_mode_0700_and_file_0600(self):
        for umask_val in (0o022, 0o002):
            with self.subTest(umask=oct(umask_val)):
                os.umask(umask_val)
                trace_path = runner.default_trace_path()
                handle = runner.start_run(
                    self.root,
                    "Run with trace",
                    adapter=MockWorker(),
                    trace_path=trace_path,
                )
                res = runner.wait(handle, timeout=30)
                self.assertEqual(res.status, "COMPLETED")

                traces_dir = trace_path.parent
                root_dir = traces_dir.parent
                self.assertEqual(stat.S_IMODE(os.lstat(root_dir).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.lstat(traces_dir).st_mode), 0o700)
                self.assertTrue(trace_path.is_file())
                self.assertEqual(stat.S_IMODE(os.lstat(trace_path).st_mode), 0o600)

    def test_trace_wide_directory_is_narrowed_with_stderr_notice(self):
        root_dir = self.isolated_tmp / "tabularium-os"
        traces_dir = root_dir / "traces"
        traces_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(root_dir, 0o755)
        os.chmod(traces_dir, 0o755)

        old_trace = traces_dir / "old.jsonl"
        old_trace.write_text('{"event": "init"}\n', encoding="utf-8")
        os.chmod(old_trace, 0o644)

        stderr_buf = io.StringIO()
        trace_path = traces_dir / "new.jsonl"
        with patch("sys.stderr", stderr_buf):
            handle = runner.start_run(
                self.root,
                "Run trace in wide dir",
                adapter=MockWorker(),
                trace_path=trace_path,
            )
            res = runner.wait(handle, timeout=30)

        self.assertEqual(res.status, "COMPLETED")
        self.assertEqual(stat.S_IMODE(os.lstat(root_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(traces_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(old_trace).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(trace_path).st_mode), 0o600)

        err_output = stderr_buf.getvalue()
        self.assertIn("Notice: narrowed permissions on", err_output)
        self.assertIn("to 0700", err_output)

    def test_trace_symlink_or_foreign_uid_fails_closed(self):
        # Symlink traces dir
        real_traces = self.isolated_tmp / "real_traces"
        real_traces.mkdir(mode=0o700)
        root_dir = self.isolated_tmp / "tabularium-os"
        root_dir.mkdir(mode=0o700)
        link_traces = root_dir / "traces"
        os.symlink(real_traces, link_traces)

        with self.assertRaises(CheckpointDirUnsafe) as ctx:
            runner.start_run(
                self.root,
                "Symlink trace run",
                adapter=MockWorker(),
                trace_path=link_traces / "run.jsonl",
            )
        self.assertEqual(ctx.exception.code, CHECKPOINT_DIR_UNSAFE)
        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertEqual(list(real_traces.iterdir()), [])

        # Foreign UID check
        link_traces.unlink()
        foreign_uid = os.getuid() + 888
        orig_stat = os.stat

        def mocked_stat(path, *args, **kwargs):
            st = orig_stat(path, *args, **kwargs)
            if "traces" in str(path) or "tabularium-os" in str(path):
                return os.stat_result((
                    st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                    foreign_uid, st.st_gid, st.st_size,
                    st.st_atime, st.st_mtime, st.st_ctime
                ))
            return st

        with patch("os.stat", side_effect=mocked_stat):
            with self.assertRaises(CheckpointDirUnsafe) as ctx:
                runner.start_run(
                    self.root,
                    "Foreign uid trace run",
                    adapter=MockWorker(),
                )
            self.assertEqual(ctx.exception.code, CHECKPOINT_DIR_UNSAFE)
            self.assertIn(str(foreign_uid), str(ctx.exception))


class AcceptanceTest6ContinueAndRunCompatibility(ControlledEnvironmentTestCase):
    """Acceptance Test 6: Existing continue_execution and run workflows pass unchanged."""

    def test_continue_execution_works_and_respects_security(self):
        # First, run a step that stops at approval (or inspect/continue pattern)
        res = run_instruction("Initial run", self.root, adapter=MockWorker())
        self.assertEqual(res.status, "COMPLETED")

        # Verify continue without continuation returns NO_CONTINUATION
        cont_res = continue_execution(self.root)
        self.assertEqual(cont_res.status, "REFUSED")
        self.assertEqual(cont_res.code, "NO_CONTINUATION")

        # If checkpoint dir is wide, continue narrows it
        cp_dir = self.isolated_tmp / "tabularium-os" / "checkpoints"
        os.chmod(cp_dir, 0o755)
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            cont_res = continue_execution(self.root)
        self.assertEqual(cont_res.status, "REFUSED")
        self.assertEqual(cont_res.code, "NO_CONTINUATION")
        self.assertEqual(stat.S_IMODE(os.lstat(cp_dir).st_mode), 0o700)
        self.assertIn("Notice: narrowed permissions on", stderr_buf.getvalue())


class TestNoFollowOnWrite(unittest.TestCase):
    """A symlink swapped in after the checks is not followed."""

    def test_write_secure_file_does_not_truncate_a_symlink_target(self) -> None:
        # The checks are taken as passed (the swap lands right after them):
        # only O_NOFOLLOW on the final open stands between the write and the
        # symlink's target.
        import tempfile as _tf
        from unittest.mock import patch as _patch
        from tabularium import storage
        with _tf.TemporaryDirectory() as raw:
            base = Path(raw)
            victim = base / "victim.txt"
            victim.write_text("keep\n")
            target_dir = base / "d"
            target_dir.mkdir(mode=0o700)
            marker = target_dir / "x.execution"
            marker.symlink_to(victim)
            with _patch.object(storage, "_ancestors_under_temp", return_value=[target_dir]), \
                 _patch.object(storage, "_check_ownership_and_mode", return_value=(0o600, False)):
                with self.assertRaises(OSError):
                    storage.write_secure_file(marker, "run-1")
            self.assertEqual(victim.read_text(), "keep\n")


class TestSharedDirectoryAndSymlinkChmod(unittest.TestCase):
    """Shared dirs are never narrowed; chmod never follows a symlink."""

    def test_sticky_world_writable_dir_is_never_narrowed(self) -> None:
        import tempfile as _tf
        from tabularium import storage
        with _tf.TemporaryDirectory() as raw:
            shared = Path(raw) / "shared"
            shared.mkdir()
            os.chmod(shared, 0o1777)                     # like /tmp
            storage._narrow_dir(shared)
            self.assertEqual(stat.S_IMODE(os.lstat(shared).st_mode), 0o1777)

    def test_trace_path_outside_tempdir_does_not_narrow_its_parent(self) -> None:
        import tempfile as _tf
        from tabularium import storage
        with _tf.TemporaryDirectory() as raw:
            shared = Path(raw) / "shared"
            shared.mkdir()
            os.chmod(shared, 0o1777)
            storage.prepare_trace_storage(shared / "trace.jsonl")
            self.assertEqual(stat.S_IMODE(os.lstat(shared).st_mode), 0o1777)

    def test_narrowing_does_not_follow_a_symlink_inside(self) -> None:
        import tempfile as _tf
        from tabularium import storage
        with _tf.TemporaryDirectory() as raw:
            base = Path(raw)
            victim = base / "victim.txt"
            victim.write_text("x")
            os.chmod(victim, 0o644)
            d = base / "d"
            d.mkdir(mode=0o755)
            (d / "link.db").symlink_to(victim)
            storage._narrow_dir(d)
            self.assertEqual(stat.S_IMODE(os.lstat(victim).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o700)
