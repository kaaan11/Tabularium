"""Tests run on a full project copy under bubblewrap."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.engine.code_gate import (
    APPLIED, DENIED, SANDBOX_TREE_UNSUPPORTED, SANDBOX_UNAVAILABLE, TESTS_FAILED,
    apply_patch_transactional,
)
from tabularium.engine.code_gate import isolated_run
from tabularium.engine.code_gate.isolated_run import (
    SandboxError, build_tree, probe_isolation, run_isolated, run_tests_on_copy,
)
from tests.test_phase5_code_gate import CodeGateFixture

AVAILABLE, WHY = probe_isolation()
# CI must exercise the sandbox; a silent skip there would hide a broken runner.
if os.environ.get("CI") and not AVAILABLE:
    raise RuntimeError(f"bwrap isolation is required in CI: {WHY}")
needs_bwrap = unittest.skipUnless(AVAILABLE, f"bwrap isolation unavailable: {WHY}")

PY = sys.executable


def bound(command):
    """Every host source and sandbox destination _runtime_paths would bind."""
    return {path for pair in isolated_run._runtime_paths(command) for path in pair}


def run_py(project, code, overlay=None, **kwargs):
    return run_tests_on_copy(project, overlay or {}, [PY, "-c", code], **kwargs)


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory()
        self.root = Path(self.holder.name).resolve()
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "__init__.py").write_text("")
        (self.root / "pkg" / "foo.py").write_text("def value():\n    return 1\n")
        (self.root / "pkg" / "bar.py").write_text("from pkg.foo import value\n\ndef doubled():\n    return value() * 2\n")
        (self.root / ".ai").mkdir()
        (self.root / ".ai" / "policy.md").write_text("governed\n")

    def tearDown(self):
        self.holder.cleanup()

    def git_init(self):
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)


@needs_bwrap
class TestIsolationContract(ProjectFixture):
    def test_patched_module_is_tested_with_the_rest_of_the_project(self):
        result = run_py(self.root, "from pkg.bar import doubled; assert doubled() == 4, doubled()",
                        overlay={"pkg/foo.py": "def value():\n    return 2\n"})
        self.assertTrue(result.passed, result.output())

    def test_unpatched_project_is_not_importable(self):
        code = f"import os; assert not os.path.exists({str(self.root / 'pkg' / 'foo.py')!r})"
        self.assertTrue(run_py(self.root, code).passed)

    def test_project_root_write_is_blocked(self):
        code = f"open({str(self.root / 'PWNED')!r}, 'w')"
        result = run_py(self.root, code)
        self.assertFalse(result.passed)
        self.assertFalse((self.root / "PWNED").exists())

    def test_network_is_unreachable(self):
        code = ("import socket\n"
                "try:\n    socket.create_connection(('1.1.1.1', 53), 3)\nexcept OSError as e:\n"
                "    print('blocked', e.errno)\nelse:\n    raise SystemExit('network reachable')\n")
        result = run_py(self.root, code)
        self.assertTrue(result.passed, result.output())
        self.assertIn("blocked", result.stdout)

    def test_host_paths_outside_the_allowlist_are_invisible(self):
        secret_dir = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".tabu-isolation-test-"))
        try:
            (secret_dir / "token").write_text("s3cret")
            code = (f"import os; assert not os.path.exists({str(secret_dir)!r});"
                    f"assert os.path.expanduser('~') == '/tmp/home'")
            result = run_py(self.root, code)
            self.assertTrue(result.passed, result.output())
        finally:
            shutil.rmtree(secret_dir)

    def test_real_project_under_home_is_invisible(self):
        # Fixtures live in /tmp, which the sandbox
        # replaces anyway; check a project where real ones live.
        project = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".tabu-isolation-project-"))
        self.addCleanup(shutil.rmtree, project)
        (project / "mod.py").write_text("x = 1\n")
        code = f"import os; assert not os.path.exists({str(project / 'mod.py')!r}); assert os.path.exists('mod.py')"
        result = run_tests_on_copy(project, {}, [PY, "-c", code])
        self.assertTrue(result.passed, result.output())

    def test_environment_is_not_inherited(self):
        with patch.dict(os.environ, {"TABU_TEST_SECRET": "leak"}):
            result = run_py(self.root, "import os; assert 'TABU_TEST_SECRET' not in os.environ")
        self.assertTrue(result.passed, result.output())

    def test_governance_is_read_only_inside_the_copy(self):
        (self.root / "constitution").mkdir()
        (self.root / "constitution" / "p.md").write_text("x\n")
        for path in (".ai/policy.md", "constitution/p.md"):
            result = run_py(self.root, f"open({path!r}, 'w')")
            self.assertFalse(result.passed, path)
            self.assertIn("Read-only file system", result.stderr, path)

    def test_sandbox_root_is_read_only(self):
        result = run_py(self.root, "open('/planted', 'w')")
        self.assertFalse(result.passed)
        self.assertIn("Read-only file system", result.stderr)

    def test_copy_is_writable_and_discarded(self):
        result = run_py(self.root, "open('pkg/new.txt', 'w').write('x')")
        self.assertTrue(result.passed, result.output())
        self.assertFalse((self.root / "pkg" / "new.txt").exists())

    def test_exit_code_is_reported(self):
        result = run_py(self.root, "raise SystemExit(3)")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.passed)

    def test_nested_user_namespaces_are_disabled(self):
        code = "import ctypes; print(ctypes.CDLL('libc.so.6').unshare(0x10000000))"
        result = run_py(self.root, code)
        self.assertEqual(result.stdout.strip(), "-1", result.output())

    def test_timeout_kills_background_children(self):
        tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tree)
        result = run_isolated(tree, ["/bin/sh", "-c", "(sleep 2; touch late) & sleep 60"], timeout=1)
        self.assertTrue(result.timed_out)
        time.sleep(3)
        self.assertFalse((tree / "late").exists())

    def test_timeout_kills_the_whole_sandbox(self):
        started = time.monotonic()
        result = run_py(self.root, "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)",
                        timeout=1)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertFalse(result.passed)
        self.assertLess(time.monotonic() - started, 20)

    def test_output_is_capped(self):
        result = run_py(self.root, "print('x' * 10000)", output_limit=100)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout), 100)

    def test_output_flood_never_reaches_the_host_disk(self):
        # Output went to host temp files before the cap.
        with patch.object(tempfile, "TemporaryFile", side_effect=AssertionError("host temp file")):
            result = run_isolated(self.root, ["/usr/bin/yes"], timeout=2, output_limit=1000)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout), 1000)

    def test_both_streams_are_returned(self):
        result = run_py(self.root, "import sys; print('out'); print('err', file=sys.stderr)")
        self.assertEqual((result.stdout.strip(), result.stderr.strip()), ("out", "err"))


class TestFailClosed(ProjectFixture):
    def test_missing_bwrap_refuses_without_running(self):
        marker = self.root / "ran-unconfined"
        with patch.object(isolated_run, "find_bwrap", return_value=None):
            with self.assertRaises(SandboxError) as caught:
                run_isolated(self.root, [PY, "-c", f"open({str(marker)!r}, 'w')"])
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)
        self.assertFalse(marker.exists())

    @needs_bwrap
    def test_mount_failure_after_clone_is_not_a_test_failure(self):
        # bwrap reports child-pid before mounting.
        with patch.object(isolated_run, "_runtime_paths", return_value=[("/nonexistent-prefix",) * 2]):
            with self.assertRaises(SandboxError) as caught:
                run_isolated(self.root, ["/usr/bin/true"])
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)

    @needs_bwrap
    def test_missing_executable_is_not_a_test_failure(self):
        with self.assertRaises(SandboxError):
            run_isolated(self.root, ["/nonexistent/command"])

    def test_bwrap_setup_failure_is_not_a_test_failure(self):
        # A bwrap that exits before starting the child reports no child-pid.
        with patch.object(isolated_run, "find_bwrap", return_value=shutil.which("false")):
            with self.assertRaises(SandboxError) as caught:
                run_isolated(self.root, [PY, "-c", "pass"])
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)

    def test_project_under_a_system_directory_is_refused(self):
        with self.assertRaises(SandboxError) as caught:
            isolated_run.bwrap_args("bwrap", self.root, ["x"], hidden=(Path("/opt/project"),), status_fd=3)
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)

    def test_empty_command_is_refused(self):
        with self.assertRaises(SandboxError):
            run_isolated(self.root, [])

    def test_interpreter_prefix_containing_the_project_is_refused(self):
        with patch.object(isolated_run, "_runtime_paths", return_value=[(str(self.root.parent), str(self.root.parent))]):
            with self.assertRaises(SandboxError) as caught:
                isolated_run.bwrap_args("bwrap", self.root, ["x"], hidden=(self.root,), status_fd=3)
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)

    def test_venv_inside_the_project_is_allowed_when_git_ignores_it(self):
        venv = self.root / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (self.root / ".gitignore").write_text(".venv/\n")
        self.git_init()
        copy = Path(tempfile.mkdtemp())  # the copy leaves the ignored venv out
        self.addCleanup(shutil.rmtree, copy)
        with patch.object(isolated_run, "_runtime_paths", return_value=[(str(venv), str(venv))]):
            args = isolated_run.bwrap_args("bwrap", copy, ["x"], hidden=(self.root,), status_fd=3)
        self.assertIn(str(venv), args)

    def test_project_directory_with_planted_pyvenv_cfg_is_refused(self):
        # A pyvenv.cfg inside project content is no venv.
        (self.root / "pkg" / "pyvenv.cfg").write_text("")
        copy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, copy)
        build_tree(self.root, copy, {})
        with patch.object(isolated_run, "_runtime_paths", return_value=[(str(self.root / "pkg"),) * 2]):
            with self.assertRaises(SandboxError):
                isolated_run.bwrap_args("bwrap", copy, ["x"], hidden=(self.root,), status_fd=3)

    def test_venv_without_git_is_refused(self):
        venv = self.root / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        copy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, copy)
        with patch.object(isolated_run, "_runtime_paths", return_value=[(str(venv), str(venv))]):
            with self.assertRaises(SandboxError):
                isolated_run.bwrap_args("bwrap", copy, ["x"], hidden=(self.root,), status_fd=3)

    def test_symlinked_pyvenv_cfg_is_not_a_venv(self):
        (self.root / ".gitignore").write_text(".venv/\n")
        self.git_init()
        venv = self.root / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").symlink_to(self.root / "pkg" / "foo.py")
        copy = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, copy)
        with patch.object(isolated_run, "_runtime_paths", return_value=[(str(venv), str(venv))]):
            with self.assertRaises(SandboxError):
                isolated_run.bwrap_args("bwrap", copy, ["x"], hidden=(self.root,), status_fd=3)

    def test_other_project_directory_is_refused(self):
        with patch.object(isolated_run, "_runtime_paths", return_value=[(str(self.root / "pkg"), str(self.root / "pkg"))]):
            with self.assertRaises(SandboxError) as caught:
                isolated_run.bwrap_args("bwrap", self.root, ["x"], hidden=(self.root,), status_fd=3)
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)


class TestRuntimePaths(ProjectFixture):
    """The real _runtime_paths, not a mock."""

    def test_script_directory_is_never_bound(self):
        script = self.root / "pkg" / "run.py"
        self.assertNotIn(str(self.root / "pkg"), bound([str(script)]))

    def test_home_and_root_are_never_bound(self):
        home = os.path.realpath(Path.home())
        for exe in (f"{home}/bin/pytest", f"{home}/run.sh", "/bin/sh", "/sh"):
            paths = bound([exe])
            self.assertNotIn(home, paths, exe)
            self.assertNotIn("/", paths, exe)

    def fake_prefix(self, *parts):
        prefix = Path(tempfile.mkdtemp()).joinpath(*parts)
        self.addCleanup(shutil.rmtree, prefix.parents[len(parts) - 1])
        (prefix / "lib" / "python3.99").mkdir(parents=True)
        (prefix / "lib" / "python3.99" / "os.py").write_text("")
        return prefix

    def test_non_python_bin_directory_is_not_bound(self):
        tools = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tools)
        (tools / "bin").mkdir()
        self.assertNotIn(str(tools), bound([str(tools / "bin" / "tool")]))

    def test_home_is_not_bound_even_when_it_looks_like_a_prefix(self):
        home = self.fake_prefix("home")
        with patch.object(Path, "home", return_value=home):
            self.assertNotIn(str(home), bound([str(home / "bin" / "pytest")]))

    def test_script_directory_is_not_bound_even_when_it_looks_like_a_prefix(self):
        scripts = self.fake_prefix("scripts")
        self.assertNotIn(str(scripts), bound([str(scripts / "run.py")]))

    def test_user_site_directory_is_not_a_prefix(self):
        # ~/.local has lib/python3.X but no stdlib.
        local = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, local)
        (local / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
        (local / "bin").mkdir()
        self.assertNotIn(str(local), bound([str(local / "bin" / "bandit")]))

    def test_venv_binds_the_installation_it_was_made_from(self):
        base = self.fake_prefix("base")
        venv = base.parent / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\nversion = 3.99\n")
        paths = bound([str(venv / "bin" / "python")])
        self.assertIn(str(venv), paths)
        self.assertIn(str(base), paths)

    def test_venv_home_pointing_at_user_site_binds_nothing_more(self):
        local = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, local)
        (local / "lib" / "python3.14").mkdir(parents=True)
        venv = local / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(f"home = {local / 'bin'}\n")
        self.assertNotIn(str(local), bound([str(venv / "bin" / "python")]))

    def test_symlinked_pyvenv_cfg_does_not_make_a_prefix(self):
        fake = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, fake)
        (fake / "bin").mkdir()
        (fake / "pyvenv.cfg").symlink_to(self.root / "pkg" / "foo.py")
        self.assertNotIn(str(fake), bound([str(fake / "bin" / "python")]))

    def test_venv_bin_binds_its_prefix(self):
        venv = self.root / ".venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        self.assertIn(str(venv), bound([str(venv / "bin" / "python")]))

    @needs_bwrap
    def test_project_script_cannot_run_unpatched_code(self):
        script = self.root / "pkg" / "run.py"
        script.write_text(f"#!{PY}\nimport sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
                          "import foo\nassert foo.value() == 2, 'unpatched'\n")
        script.chmod(0o755)
        # The real project directory is not in the sandbox, so the script
        # cannot even start there.
        with self.assertRaises(SandboxError) as caught:
            run_tests_on_copy(self.root, {"pkg/foo.py": "def value():\n    return 2\n"}, [str(script)])
        self.assertEqual(caught.exception.code, SANDBOX_UNAVAILABLE)

    def test_alias_to_root_is_never_bound(self):
        # A symlink to "/" passed the filters unresolved.
        alias = Path(tempfile.mkdtemp()) / "root_alias"
        self.addCleanup(shutil.rmtree, alias.parent)
        alias.symlink_to("/")
        paths = bound([str(alias / "bin" / "python3")])
        self.assertNotIn(str(alias), paths)
        self.assertNotIn("/", paths)

    def test_symlinked_home_is_never_bound(self):
        real_home = self.fake_prefix("real_home")
        link_home = real_home.parent / "link_home"
        link_home.symlink_to(real_home)
        with patch.object(Path, "home", return_value=link_home):
            paths = bound([str(link_home / "bin" / "pytest")])
        self.assertNotIn(str(link_home), paths)
        self.assertNotIn(str(real_home), paths)

    def test_alias_shows_the_approved_source(self):
        real = self.fake_prefix("real")
        alias = real.parent / "alias"
        alias.symlink_to(real)
        self.assertIn((str(real), str(alias)), isolated_run._runtime_paths([str(alias / "bin" / "python")]))

    @needs_bwrap
    def test_src_layout_imports_the_patched_package(self):
        (self.root / "src" / "mypkg").mkdir(parents=True)
        (self.root / "src" / "mypkg" / "__init__.py").write_text("V = 'unpatched'\n")
        result = run_tests_on_copy(self.root, {"src/mypkg/__init__.py": "V = 'patched'\n"},
                                   [PY, "-c", "import mypkg; print(mypkg.V)"])
        self.assertEqual(result.stdout.strip(), "patched", result.output())

    @needs_bwrap
    def test_bare_command_finds_the_venv_bin(self):
        result = run_tests_on_copy(self.root, {}, ["python", "-c", "import sys; print(sys.prefix)"])
        self.assertTrue(result.passed, result.output())

    @needs_bwrap
    def test_system_shell_runs(self):
        result = run_tests_on_copy(self.root, {}, ["/bin/sh", "-c", "test -f pkg/foo.py && echo ok"])
        self.assertTrue(result.passed, result.output())
        self.assertEqual(result.stdout.strip(), "ok")


class TestBuildTree(ProjectFixture):
    def build(self, overlay=None):
        dest = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dest)
        build_tree(self.root, dest, overlay or {})
        return dest

    def test_git_project_copies_tracked_and_untracked_but_not_ignored(self):
        (self.root / ".gitignore").write_text(".env\n")
        (self.root / ".env").write_text("TOKEN=s3cret\n")
        self.git_init()
        (self.root / "pkg" / "untracked.py").write_text("x = 1\n")
        dest = self.build()
        self.assertTrue((dest / "pkg" / "bar.py").is_file())
        self.assertTrue((dest / "pkg" / "untracked.py").is_file())
        self.assertFalse((dest / ".env").exists())
        self.assertFalse((dest / ".git").exists())

    def test_git_subdirectory_honours_ignore_files(self):
        sub = self.root / "sub"
        sub.mkdir()
        (sub / ".gitignore").write_text(".secret\n")
        (sub / ".secret").write_text("s3cret\n")
        (sub / "m.py").write_text("x = 1\n")
        self.git_init()
        dest = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dest)
        build_tree(sub, dest, {})
        self.assertTrue((dest / "m.py").is_file())
        self.assertFalse((dest / ".secret").exists())
        self.assertFalse((dest / "pkg").exists())

    def test_plain_directory_skips_env_files(self):
        (self.root / ".env").write_text("TOKEN=1\n")
        (self.root / ".env.local").write_text("TOKEN=1\n")
        dest = self.build()
        self.assertFalse((dest / ".env").exists())
        self.assertFalse((dest / ".env.local").exists())

    def test_overlay_on_read_only_file_keeps_its_mode(self):
        ro = self.root / "pkg" / "ro.py"
        ro.write_text("x = 1\n")
        ro.chmod(0o444)
        dest = self.build({"pkg/ro.py": "x = 2\n"})
        self.assertEqual((dest / "pkg" / "ro.py").read_text(), "x = 2\n")
        self.assertEqual((dest / "pkg" / "ro.py").stat().st_mode & 0o777, 0o444)

    def test_overlay_path_traversal_is_refused(self):
        with self.assertRaises(SandboxError):
            self.build({"../escape.py": "x"})

    def test_git_failure_in_a_repository_is_refused(self):
        # Walking would ignore .gitignore.
        self.git_init()
        with patch.object(isolated_run, "_listed_files", return_value=None):
            with self.assertRaises(SandboxError) as caught:
                self.build()
        self.assertEqual(caught.exception.code, SANDBOX_TREE_UNSUPPORTED)

    def test_env_files_are_skipped_even_when_git_does_not_ignore_them(self):
        names = (".env", "prod.ENV", ".envrc", "x.env.local", ".env-prod")
        for name in names:
            (self.root / name).write_text("X=1\n")
        self.git_init()
        dest = self.build()
        for name in names:
            self.assertFalse((dest / name).exists(), name)
        self.assertTrue((dest / "pkg" / "foo.py").exists())

    def test_source_reached_through_a_symlinked_directory_is_refused(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / "secret.txt").write_text("OUTSIDE")
        (self.root / ".gitignore").write_text("sub\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "secret.txt").write_text("x")
        self.git_init()
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "sub/secret.txt"], check=True)
        shutil.rmtree(self.root / "sub")
        (self.root / "sub").symlink_to(outside)
        with self.assertRaises(SandboxError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, SANDBOX_TREE_UNSUPPORTED)

    def test_byte_limit_is_enforced(self):
        with patch.object(isolated_run, "MAX_TREE_BYTES", 10):
            with self.assertRaises(SandboxError) as caught:
                self.build()
        self.assertEqual(caught.exception.code, SANDBOX_TREE_UNSUPPORTED)

    def test_plain_directory_skips_vcs_caches_and_virtualenvs(self):
        (self.root / "vendor" / ".git").mkdir(parents=True)
        (self.root / "vendor" / ".git" / "config").write_text("x")
        (self.root / "venv").mkdir()
        (self.root / "venv" / "pyvenv.cfg").write_text("home = /usr\n")
        (self.root / "pkg" / "__pycache__").mkdir()
        (self.root / "pkg" / "__pycache__" / "foo.pyc").write_bytes(b"x")
        dest = self.build()
        self.assertTrue((dest / "pkg" / "foo.py").is_file())
        for skipped in ("vendor/.git", "venv", "pkg/__pycache__"):
            self.assertFalse((dest / skipped).exists(), skipped)

    def test_overlay_replaces_and_creates_files(self):
        dest = self.build({"pkg/foo.py": "def value():\n    return 2\n", "new/mod.py": "y = 2\n"})
        self.assertIn("return 2", (dest / "pkg" / "foo.py").read_text())
        self.assertEqual((dest / "new" / "mod.py").read_text(), "y = 2\n")
        self.assertIn("return 1", (self.root / "pkg" / "foo.py").read_text())

    def test_mode_is_kept(self):
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        dest = self.build()
        self.assertEqual((dest / "run.sh").stat().st_mode & 0o777, 0o755)

    def test_symlinks_are_copied_as_links_and_never_written_through(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (self.root / "link").symlink_to(outside)
        dest = self.build()
        self.assertTrue((dest / "link").is_symlink())
        for rel in ("link/evil.py", "link/nested/deeper/evil.py"):
            with self.assertRaises(SandboxError) as caught:
                self.build({rel: "x"})
            self.assertEqual(caught.exception.code, SANDBOX_TREE_UNSUPPORTED)
            self.assertEqual(list(outside.iterdir()), [], rel)

    def test_oversized_project_is_refused(self):
        with patch.object(isolated_run, "MAX_TREE_FILES", 2):
            with self.assertRaises(SandboxError) as caught:
                self.build()
        self.assertEqual(caught.exception.code, SANDBOX_TREE_UNSUPPORTED)


class TestCodeGateUsesSandbox(CodeGateFixture):
    DIFF = ('--- a/src/sample.py\n+++ b/src/sample.py\n@@ -1,2 +1,2 @@\n'
            ' def calculate_total(a, b):\n-    return a + b\n+    return a - b\n')

    def apply(self, command):
        proposal = self.make_proposal(self.DIFF)
        return apply_patch_transactional(self.root, proposal, self.make_authorization(proposal),
                                         test_command=command)

    def test_missing_bwrap_denies_and_leaves_target(self):
        original = self.target_file.read_bytes()
        with patch.object(isolated_run, "find_bwrap", return_value=None):
            result = self.apply([PY, "-c", "pass"])
        self.assertEqual(result.status, DENIED)
        self.assertIn(SANDBOX_UNAVAILABLE, result.reason_codes)
        self.assertEqual(self.target_file.read_bytes(), original)

    @needs_bwrap
    def test_command_sees_other_files_and_cannot_touch_the_project(self):
        (self.root / "src" / "helper.py").write_text("HELP = 1\n")
        code = ("import sys; sys.path.insert(0, 'src'); import helper, sample;"
                "assert sample.calculate_total(3, 1) == 2;"
                f"open({str(self.root / 'PWNED')!r}, 'w')")
        result = self.apply([PY, "-c", code])
        self.assertEqual(result.status, TESTS_FAILED)
        # Earlier asserts passed; the write itself failed (hidden or read-only).
        self.assertIn("PWNED", result.test_output)
        self.assertNotIn("AssertionError", result.test_output)
        self.assertFalse((self.root / "PWNED").exists())

    @needs_bwrap
    def test_fifo_or_sparse_target_does_not_hang_the_host(self):
        # The read-back used to open a FIFO and block.
        for trick in ("os.unlink('src/sample.py'); os.mkfifo('src/sample.py')",
                      "os.truncate('src/sample.py', 1 << 33)",
                      "import shutil; shutil.move('src', 'real'); os.symlink('real', 'src')"):
            started = time.monotonic()
            result = self.apply([PY, "-c", f"import os; {trick}"])
            self.assertEqual(result.status, DENIED, trick)
            self.assertIn("TARGET_CHANGED", result.reason_codes, trick)
            self.assertLess(time.monotonic() - started, 30, trick)
            self.assertNotIn("return a - b", self.target_file.read_text())

    def test_runner_error_is_not_reported_as_a_test_failure(self):
        # An infrastructure error is not the patch's fault.
        with patch("tabularium.engine.code_gate.sandbox.run_isolated", side_effect=OSError(28, "No space left")):
            result = self.apply([PY, "-c", "pass"])
        self.assertEqual(result.status, DENIED)
        self.assertIn(SANDBOX_UNAVAILABLE, result.reason_codes)

    @needs_bwrap
    def test_timed_out_command_is_a_failure(self):
        original = self.target_file.read_bytes()
        real = isolated_run.run_isolated
        with patch("tabularium.engine.code_gate.sandbox.run_isolated",
                   lambda *a, **k: real(*a, **{**k, "timeout": 1})):
            result = self.apply([PY, "-c", "import time; time.sleep(30)"])
        self.assertEqual(result.status, TESTS_FAILED)
        self.assertEqual(self.target_file.read_bytes(), original)

    @needs_bwrap
    def test_passing_command_applies(self):
        code = "import sys; sys.path.insert(0, 'src'); import sample; assert sample.calculate_total(3, 1) == 2"
        result = self.apply([PY, "-c", code])
        self.assertEqual(result.status, APPLIED, result.test_output)
        self.assertIn("return a - b", self.target_file.read_text())


if __name__ == "__main__":
    unittest.main()
