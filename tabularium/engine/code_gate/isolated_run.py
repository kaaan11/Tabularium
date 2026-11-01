"""Run an untrusted test command against a disposable project copy under bubblewrap.

The command sees an allowlisted read-only system (``/usr``, ``/etc``, ``/opt``
and the interpreter prefixes it needs), a private ``/tmp`` and the copy at
``/work``. It gets no network, no view of the operator's home or of the real
project, and no inherited environment. Without a working ``bwrap`` nothing
runs: callers get ``SANDBOX_UNAVAILABLE`` and must not fall back to an
unconfined run.

Known limits: the copy lives on the host disk and is not size-limited while
the command runs; there is no CPU or memory limit beyond the timeout. The
copy has no ``.git`` and git submodules are not copied. Only Python
interpreter prefixes are bound, so other runtimes installed outside
``/usr`` or ``/opt`` are not available.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .model import SANDBOX_TREE_UNSUPPORTED, SANDBOX_UNAVAILABLE

SANDBOX_ROOT = "/work"
DEFAULT_TIMEOUT = 300.0
DEFAULT_OUTPUT_LIMIT = 64 * 1024
MAX_TREE_FILES = 20_000
MAX_TREE_BYTES = 256 * 1024 * 1024
TMP_SIZE = 512 * 1024 * 1024
# Governance stays read-only inside the copy as well.
READONLY_SUBPATHS = (".ai", "constitution")
_SYSTEM_DIRS = ("/usr", "/etc", "/opt")
_USR_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
_SKIP_DIRS = frozenset({".git", "__pycache__"})
_GIT = ("git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false")


class SandboxError(Exception):
    """The isolated run could not be set up; nothing was executed unconfined."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class IsolatedRun:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def output(self) -> str:
        return f"STDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"


def find_bwrap() -> str | None:
    return shutil.which("bwrap")


# --- project copy -----------------------------------------------------------

def _listed_files(root: Path) -> list[str] | None:
    """Tracked and untracked, non-ignored files when root is inside a git work tree.

    Run from a subdirectory, ``ls-files`` lists that subtree relative to it
    and still honours every applicable ignore file.
    """
    try:
        inside = subprocess.run([*_GIT, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=30)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        listed = subprocess.run(
            [*_GIT, "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    return sorted({p for p in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if p})


def _is_env_file(name: str) -> bool:
    folded = name.casefold()
    return folded.startswith(".env") or folded.endswith(".env") or ".env." in folded


def _in_git_repo(root: Path) -> bool:
    return any((p / ".git").exists() for p in (root, *root.parents))


def _walked_files(root: Path) -> list[str]:
    """Every file below root, skipping VCS metadata, caches and virtualenvs."""
    found: list[str] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        kept = []
        for name in dirs:
            path = here / name
            if path.is_symlink():
                files.append(name)
            elif name not in _SKIP_DIRS and not (path / "pyvenv.cfg").exists():
                kept.append(name)
        dirs[:] = kept
        found.extend((here / name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def _make_parents(base: Path, rel: str) -> Path:
    """Create rel's parent directories under base without following symlinks."""
    current = base
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise SandboxError(SANDBOX_TREE_UNSUPPORTED, f"{rel!r} would be written through a symlink")
    return base / rel


def _real_dirs(root: Path, parts: tuple[str, ...], checked: set[tuple[str, ...]]) -> bool:
    """Whether every directory on the way to a source file is a real directory.

    Missing ones count as real: the file itself is then reported missing.
    """
    for depth in range(1, len(parts) + 1):
        prefix = parts[:depth]
        if prefix in checked:
            continue
        try:
            info = root.joinpath(*prefix).lstat()
        except FileNotFoundError:
            return True
        if not stat.S_ISDIR(info.st_mode):
            return False
        checked.add(prefix)
    return True


def copy_holds(base: str | Path, rel: str, expected: bytes) -> bool:
    """Whether base/rel is still a regular file holding exactly expected.

    The test command may have replaced it with a FIFO, a huge sparse file or
    a symlinked directory; none of those is opened or read in full.
    """
    current = Path(base)
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                return False
        except OSError:
            return False
    target = current / PurePosixPath(rel).name
    try:
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(expected):
            return False
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as stream:
        return stream.read(len(expected) + 1) == expected


def _write_overlay(target: Path, rel: str, content: str) -> None:
    """Replace or create target; an existing file keeps its mode, even read-only."""
    mode = None
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise SandboxError(SANDBOX_TREE_UNSUPPORTED, f"{rel!r} is not a regular file in the copy")
        mode = stat.S_IMODE(info.st_mode)
        target.unlink()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        stream.write(content)
    if mode is not None:
        os.chmod(target, mode)


def build_tree(project_root: str | Path, dest: str | Path, overlay: dict[str, str]) -> None:
    """Copy the project's working files into dest, then write overlay on top.

    Regular files keep their mode, symlinks are copied as symlinks and
    everything else is skipped. ``.env`` files are never copied, even when
    git does not ignore them. Neither the copy nor the read of the source
    goes through a symlinked directory.
    """
    root = Path(project_root).resolve()
    base = Path(dest).resolve()
    names = _listed_files(root)
    if names is None:
        if _in_git_repo(root):
            # Without git's answer the ignore list is unknown; walking would
            # copy ignored secrets.
            raise SandboxError(SANDBOX_TREE_UNSUPPORTED, "git could not list the project files")
        names = _walked_files(root)
    names = [rel for rel in names if not _is_env_file(PurePosixPath(rel).name)]
    if len(names) > MAX_TREE_FILES:
        raise SandboxError(SANDBOX_TREE_UNSUPPORTED, f"{len(names)} files exceed {MAX_TREE_FILES}")
    total = 0
    checked: set[tuple[str, ...]] = set()
    for rel in names:
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise SandboxError(SANDBOX_TREE_UNSUPPORTED, f"unsafe listed path {rel!r}")
        src = root / rel
        if not _real_dirs(root, pure.parts[:-1], checked):
            raise SandboxError(SANDBOX_TREE_UNSUPPORTED, f"{rel!r} is reached through a symlinked directory")
        try:
            info = src.lstat()
        except FileNotFoundError:
            continue  # tracked but deleted in the working tree
        target = _make_parents(base, rel)
        if stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(src), target)
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > MAX_TREE_BYTES:
                raise SandboxError(SANDBOX_TREE_UNSUPPORTED, f"project copy exceeds {MAX_TREE_BYTES} bytes")
            shutil.copyfile(src, target, follow_symlinks=False)
            os.chmod(target, info.st_mode & 0o777)
    for rel, content in overlay.items():
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts:
            raise SandboxError(SANDBOX_TREE_UNSUPPORTED, f"unsafe overlay path {rel!r}")
        _write_overlay(_make_parents(base, rel), rel, content)


# --- bubblewrap -------------------------------------------------------------

def _is_venv(path: str) -> bool:
    try:
        return stat.S_ISREG((Path(path) / "pyvenv.cfg").lstat().st_mode)
    except OSError:
        return False


def _is_python_prefix(path: str) -> bool:
    """A virtualenv, or an installation with its standard library.

    A bare ``lib/python3*`` is not enough: ``~/.local`` has one for user
    site-packages and would expose everything beside it.
    """
    return _is_venv(path) or any(Path(path).glob("lib/python3*/os.py"))


def _venv_base(venv: str) -> str | None:
    """The installation a venv was made from (``home = <prefix>/bin``)."""
    try:
        with open(Path(venv) / "pyvenv.cfg", encoding="utf-8", errors="replace") as stream:
            text = stream.read(4096)
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "home":
            bin_dir = Path(value.strip())
            return str(bin_dir.parent) if bin_dir.name == "bin" and bin_dir.is_absolute() else None
    return None


def _bindable(path: str, home: str) -> bool:
    if any(path == d or path.startswith(d + "/") for d in _SYSTEM_DIRS):
        return False
    # Home or any ancestor of it, which includes "/".
    return os.path.commonpath([path, home]) != path


def _runtime_paths(command: list[str]) -> list[tuple[str, str]]:
    """(source, destination) binds for the Python prefixes the command needs.

    Only directories that look like a Python prefix qualify, never ``/``, the
    operator's home or one of its ancestors: an arbitrary script directory
    would expose the real, unpatched project. Every check runs on the
    resolved source; an alias spelling (a venv's symlinked base interpreter)
    is only ever a destination that shows that same approved source.
    """
    candidates = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    if command and os.path.isabs(command[0]):
        for exe in (Path(command[0]), Path(os.path.realpath(command[0]))):
            if exe.parent.name == "bin":
                candidates.add(str(exe.parent.parent))
    for venv in [c for c in candidates if _is_venv(c)]:
        base = _venv_base(venv)
        if base:
            candidates.add(base)
    home = os.path.realpath(Path.home())
    binds = set()
    for raw in candidates:
        path = os.path.realpath(raw)
        if not (_bindable(path, home) and os.path.isdir(path) and _is_python_prefix(path)):
            continue
        binds.add((path, path))
        alias = os.path.abspath(raw)
        if alias != path and _bindable(alias, home):
            binds.add((path, alias))
    return sorted(binds)


def _is_project_venv(path: str, project: str, tree: Path) -> bool:
    """A virtualenv inside the project: a real pyvenv.cfg, ignored by git and
    therefore absent from the copy. Without git nothing inside qualifies."""
    rel = os.path.relpath(path, project)
    if not _is_venv(path):
        return False
    try:
        ignored = subprocess.run([*_GIT, "-C", project, "check-ignore", "-q", "--", rel],
                                 capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return ignored.returncode == 0 and not os.path.lexists(tree / rel)


def bwrap_args(bwrap: str, tree: Path, command: list[str], *, hidden: tuple[Path, ...],
               status_fd: int) -> list[str]:
    args = [bwrap, "--unshare-all", "--unshare-user", "--disable-userns",
            "--die-with-parent", "--new-session", "--clearenv"]
    for d in _SYSTEM_DIRS:
        if os.path.isdir(d):
            args += ["--ro-bind", d, d]
    for link in _USR_LINKS:
        if os.path.islink(link):
            args += ["--symlink", os.readlink(link), link]
        elif os.path.isdir(link):
            args += ["--ro-bind", link, link]
    args += ["--dev", "/dev", "--proc", "/proc", "--size", str(TMP_SIZE), "--tmpfs", "/tmp",
             "--dir", "/tmp/home"]
    for secret in hidden:
        if not _bindable(str(secret), "/nonexistent-home"):
            raise SandboxError(SANDBOX_UNAVAILABLE,
                               f"project {secret} lies under a system directory the sandbox exposes")
    runtime = _runtime_paths(command)
    for path, dest in runtime:
        # A prefix that contains the project would expose its unpatched
        # files. Inside the project only a real virtualenv the copy left
        # out (ignored by git) may be bound; anything the copy holds is
        # project content.
        for secret in hidden:
            common = os.path.commonpath([path, str(secret)])
            inside = common == str(secret)
            if common == path or (inside and not _is_project_venv(path, str(secret), tree)):
                raise SandboxError(SANDBOX_UNAVAILABLE,
                                   f"interpreter path {path} overlaps the project; refusing to expose it")
        args += ["--ro-bind", path, dest]
    args += ["--bind", str(tree), SANDBOX_ROOT]
    for sub in READONLY_SUBPATHS:
        inner = tree / sub
        if inner.is_dir() and not inner.is_symlink():
            args += ["--ro-bind", str(inner), f"{SANDBOX_ROOT}/{sub}"]
    bins = [f"{dest}/bin" for path, dest in runtime if os.path.isdir(f"{path}/bin")]
    if command and os.path.isabs(command[0]):
        bins.insert(0, os.path.dirname(command[0]))
    path_env = ":".join([*dict.fromkeys(bins), "/usr/local/bin", "/usr/bin", "/bin"])
    # src layout: the patched package must win over an installed copy.
    pythonpath = f"{SANDBOX_ROOT}/src:{SANDBOX_ROOT}" if (tree / "src").is_dir() else SANDBOX_ROOT
    args += ["--remount-ro", "/", "--chdir", SANDBOX_ROOT,
             "--setenv", "PATH", path_env,
             "--setenv", "HOME", "/tmp/home",
             "--setenv", "TMPDIR", "/tmp",
             "--setenv", "LANG", "C.UTF-8",
             "--setenv", "PYTHONPATH", pythonpath,
             "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
             "--setenv", "PYTHONNOUSERSITE", "1",
             "--json-status-fd", str(status_fd),
             "--", *command]
    return args


def _drain(fd: int, limit: int, result: list) -> None:
    """Keep the first limit bytes of a pipe and discard the rest unread to disk."""
    kept = bytearray()
    cut = False
    while chunk := os.read(fd, 65536):
        room = limit - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > room:
            cut = True
    result.extend([kept.decode("utf-8", "replace"), cut])


def run_isolated(tree: str | Path, command: list[str], *, hidden: tuple[str | Path, ...] = (),
                 timeout: float = DEFAULT_TIMEOUT,
                 output_limit: int = DEFAULT_OUTPUT_LIMIT) -> IsolatedRun:
    """Run command with tree mounted read-write at /work; see the module limits.

    ``hidden`` names host paths (normally the real project) that must stay
    invisible; an interpreter prefix overlapping one of them is refused.
    """
    if not command:
        raise SandboxError(SANDBOX_UNAVAILABLE, "empty test command")
    bwrap = find_bwrap()
    if bwrap is None:
        raise SandboxError(SANDBOX_UNAVAILABLE, "bwrap is not installed; refusing to run tests unconfined")
    tree_path = Path(tree).resolve()
    hidden_paths = tuple(Path(h).resolve() for h in hidden)
    status_r, status_w = os.pipe()
    try:
        args = bwrap_args(bwrap, tree_path, list(command), hidden=hidden_paths, status_fd=status_w)
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"},
                                    pass_fds=(status_w,), start_new_session=True)
        except OSError as exc:
            raise SandboxError(SANDBOX_UNAVAILABLE, f"bwrap could not start: {exc}") from exc
        timed_out = False
        out: list = []
        err: list = []
        readers = [threading.Thread(target=_drain, args=(proc.stdout.fileno(), output_limit, out), daemon=True),
                   threading.Thread(target=_drain, args=(proc.stderr.fileno(), output_limit, err), daemon=True)]
        try:
            os.close(status_w)
            status_w = -1
            for reader in readers:
                reader.start()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
        except BaseException:
            _kill(proc)
            raise
        finally:
            for reader in readers:
                reader.join(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        with os.fdopen(status_r, "rb") as status_stream:
            status_r = -1
            statuses = _parse_status(status_stream.read())
        stdout, cut_out = out or ["", False]
        stderr, cut_err = err or ["", False]
    finally:
        for fd in (status_r, status_w):
            if fd >= 0:
                os.close(fd)
    # bwrap reports exit-code only when the command really ran; a failed
    # mount, a missing executable or a non-bwrap binary leaves it out.
    if "exit-code" not in statuses and not timed_out:
        raise SandboxError(SANDBOX_UNAVAILABLE,
                           f"the test command did not start in the sandbox: {stderr.strip()[:500]}")
    returncode = None if timed_out else statuses["exit-code"]
    return IsolatedRun(returncode=returncode, stdout=stdout, stderr=stderr,
                       timed_out=timed_out, truncated=cut_out or cut_err)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _parse_status(raw: bytes) -> dict[str, int]:
    merged: dict[str, int] = {}
    decoder = json.JSONDecoder()
    text = raw.decode("utf-8", "replace")
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        try:
            item, index = decoder.raw_decode(text, index)
        except ValueError:
            break
        if isinstance(item, dict):
            merged.update({k: v for k, v in item.items() if isinstance(v, int)})
    return merged


def run_tests_on_copy(project_root: str | Path, overlay: dict[str, str], command: list[str], *,
                      timeout: float = DEFAULT_TIMEOUT,
                      output_limit: int = DEFAULT_OUTPUT_LIMIT) -> IsolatedRun:
    """Copy the project, apply overlay, run command isolated, discard the copy."""
    root = Path(project_root).resolve()
    with tempfile.TemporaryDirectory(prefix="tabu-verify-") as scratch:
        tree = Path(scratch) / "tree"
        tree.mkdir()
        build_tree(root, tree, overlay)
        return run_isolated(tree, command, hidden=(root,), timeout=timeout, output_limit=output_limit)


def probe_isolation(timeout: float = 30.0) -> tuple[bool, str]:
    """Run ``true`` with the real profile; used by tests and callers, not doctor."""
    try:
        with tempfile.TemporaryDirectory(prefix="tabu-probe-") as scratch:
            result = run_isolated(scratch, ["/usr/bin/true"], timeout=timeout)
    except SandboxError as exc:
        return False, exc.detail
    if not result.passed:
        return False, f"probe exited with {result.returncode}"
    return True, "bwrap isolation works"
