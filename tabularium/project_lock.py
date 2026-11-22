"""Project-wide advisory lock for governed mutations.

One mutation at a time per repository: the lock is taken before the git
preflight and candidate Task allocation and held until the worktree is
cleaned up and the candidate Task is closed, so approval, apply, report and
commit of two runs can never interleave. Per-file compare-and-swap and the
apply journal stay in place as a second line of defense.

Task mutations, ordinary runs, continuation, initialization, onboarding writes,
and the persistence writer use the same scope. Calls nested in the owning
thread reuse its lock; another thread or process must acquire its own lock.

Two files live in the repository's git common directory, next to the apply
journal (so every worktree of one repository shares them):

* ``tabu-project.lock``: the OS lock target (``flock`` on POSIX). It is never
  deleted: deleting a locked file would let a second process lock a new file
  under the same name.
* ``tabu-project.owner``: who holds the lock (pid, host, optional execution id, start
  time). Written after the lock is acquired, removed before it is released.

A busy project is refused at once with ``PROJECT_BUSY``; nothing waits, since
the holder may be waiting on a human approval.

Stale lock: the OS releases the lock when its holder dies, but a holder that
was killed cannot remove its owner record. Finding the lock free and the
record present therefore means an earlier run ended without cleanup. That is
refused with ``PROJECT_LOCK_STALE`` and the record is left as evidence; the
user inspects the project and deletes the record. Nothing breaks, steals or
clears a lock on its own.

Non-Git projects use a canonical-path hash in private per-user temporary
storage; lock files never become project records or untracked project files.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import socket
import subprocess
import stat
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if os.name == "nt":  # pragma: no cover - not exercised on POSIX
    import msvcrt
else:
    import fcntl

PROJECT_BUSY = "PROJECT_BUSY"
PROJECT_LOCK_STALE = "PROJECT_LOCK_STALE"
PROJECT_LOCK_UNAVAILABLE = "PROJECT_LOCK_UNAVAILABLE"

LOCK_NAME = "tabu-project.lock"
OWNER_NAME = "tabu-project.owner"


class ProjectLockRefused(RuntimeError):
    """The project lock was not taken; nothing was started."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":  # pragma: no cover
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the descriptor releases it as well


def _describe(owner: Path) -> str:
    """A one-line description of the recorded holder, or why there is none."""
    try:
        record = json.loads(owner.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError
    except FileNotFoundError:
        return "holder not recorded yet"
    except (OSError, ValueError):
        return f"unreadable owner record {owner}"
    return (f"pid {record.get('pid', '?')} on {record.get('host', '?')}, "
            f"execution {record.get('execution_id', '?')}, "
            f"started {record.get('started_at', '?')}")


class ProjectLock:
    """Exclusive, non-blocking project lock; use as a context manager."""

    def __init__(self, git_dir: Path, execution_id: str = ""):
        self.lock_path = git_dir / LOCK_NAME
        self.owner_path = git_dir / OWNER_NAME
        self.execution_id = execution_id
        self._fd: int | None = None

    def __enter__(self) -> "ProjectLock":
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                     f"The project lock {self.lock_path} cannot be opened "
                                     f"({type(exc).__name__}); nothing was started.") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("lock is not a regular file")
            if not _try_lock(fd):
                raise ProjectLockRefused(PROJECT_BUSY, (
                    f"The project is busy: another project mutation is running "
                    f"({_describe(self.owner_path)}). Nothing was started; "
                    "run again after it finishes."))
            if os.path.lexists(self.owner_path):
                raise ProjectLockRefused(PROJECT_LOCK_STALE, (
                    f"An earlier project mutation ({_describe(self.owner_path)}) ended without "
                    "completing cleanup, most likely because it was killed. "
                    "Nothing was started. Before continuing, check: `git status` and "
                    "`git diff` for files it may have changed; the apply journal "
                    "(tabu-apply.journal in the git common directory), if present "
                    "(inspect every listed file, restore or keep its changes, then "
                    "delete the journal only after resolving the interrupted apply); "
                    "`git worktree list` for a leftover worktree (`git worktree prune` "
                    "after deleting it); `tabu task list` for a candidate Task it left "
                    f"active. Then delete {self.owner_path} (never {self.lock_path})."))
            record = {"pid": os.getpid(), "host": socket.gethostname(),
                      "execution_id": self.execution_id,
                      "started_at": datetime.now(timezone.utc).isoformat()}
            try:
                with open(self.owner_path, "x", encoding="utf-8") as stream:
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                         f"The project lock owner record {self.owner_path} "
                                         f"cannot be written ({type(exc).__name__}); "
                                         "nothing was started.") from exc
        except OSError as exc:
            _unlock(fd)
            os.close(fd)
            raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                     f"The project lock {self.lock_path} is unavailable "
                                     f"({type(exc).__name__}); nothing was started.") from exc
        except BaseException:
            _unlock(fd)
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc: Any) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            # Record first, lock second: a record without a held lock reads as
            # stale, never the reverse.
            try:
                os.unlink(self.owner_path)
            except FileNotFoundError:
                pass
        finally:
            _unlock(fd)
            os.close(fd)


def git_common_dir(target: Path | str) -> Path | None:
    """The repository's git common directory (shared by all its worktrees)."""
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(target),
                             capture_output=True, text=True, check=True,
                             env={**os.environ, "LC_ALL": "C"}).stdout.strip()
    except FileNotFoundError as exc:
        # Init and Task operations have always supported plain directories
        # without Git installed. Only use the path lock when no repository
        # markers or explicit Git environment could hide a shared Git scope.
        path = Path(target)
        git_environment = any(os.environ.get(name) for name in
                              ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"))
        repository_markers = any(
            os.path.lexists(parent / ".git")
            or ((parent / "HEAD").exists()
                and ((parent / "objects").exists() or (parent / "commondir").exists()))
            for parent in (path, *path.parents))
        if not git_environment and not repository_markers:
            return None
        raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                 "Git is unavailable; the repository's shared lock "
                                 "could not be located. Nothing was started.") from exc
    except subprocess.CalledProcessError as exc:
        if "not a git repository" in (exc.stderr or ""):
            return None
        raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                 "Git could not locate the project lock directory; "
                                 "nothing was started.") from exc
    except OSError as exc:
        raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                 "Git is unavailable; the project lock could not "
                                 "be located. Nothing was started.") from exc
    if not out:
        raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                 "Git returned no project lock directory; nothing was started.")
    git_dir = Path(out)
    if not git_dir.is_absolute():
        git_dir = Path(target) / git_dir
    return git_dir.resolve()


def _lock_directory(target: Path) -> Path:
    git_dir = git_common_dir(target)
    if git_dir is not None:
        return git_dir
    # Non-Git projects support Tasks and report persistence too. Keep their
    # permanent lock inode outside the project, in a private per-user directory.
    # POSIX contenders must agree on the lock inode even with different TMPDIRs.
    temp_root = Path("/tmp" if os.name == "posix" else tempfile.gettempdir())
    base = temp_root / f"tabularium-project-locks-{getattr(os, 'getuid', lambda: 0)()}"
    try:
        base.mkdir(mode=0o700, exist_ok=True)
        info = base.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise OSError("unsafe lock storage")
        directory = base / hashlib.sha256(os.fsencode(target)).hexdigest()
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError("unsafe project lock directory")
        return directory
    except OSError as exc:
        raise ProjectLockRefused(PROJECT_LOCK_UNAVAILABLE,
                                 "Private project lock storage is unavailable; "
                                 "nothing was started.") from exc


_held = threading.local()


@contextmanager
def project_mutation(target: Path, execution_id: str = ""):
    """Re-enter only in the owning thread, for nested governed mutations."""
    directory = _lock_directory(target)
    key = (os.getpid(), directory)
    held = getattr(_held, "locks", None)
    if held is None:
        held = _held.locks = set()
    if key in held:
        yield
        return
    with ProjectLock(directory, execution_id):
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


def serialized_project(target_argument: str, refused: Callable[..., Any],
                       read_only: Callable[[dict], bool] | None = None):
    """Lock before preflight; preserve each public surface's result contract."""
    def decorate(run: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(run)

        @functools.wraps(run)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from tabularium.init import InitError, _canonical_target

            arguments = signature.bind(*args, **kwargs).arguments
            if read_only is not None and read_only(arguments):
                return run(*args, **kwargs)
            try:
                target = _canonical_target(arguments.get(target_argument))
            except InitError:
                # Let the original surface report invalid targets as before.
                return run(*args, **kwargs)
            try:
                with project_mutation(target, str(arguments.get("execution_id") or "")):
                    return run(*args, **kwargs)
            except ProjectLockRefused as exc:
                return refused(target, exc, arguments)
        return wrapper
    return decorate


def serialized_execution(target_argument: str):
    def refused(target: Path, exc: ProjectLockRefused, arguments: dict):
        from tabularium.execution import ProductResult
        return ProductResult(status="REFUSED", code=exc.code, message=str(exc),
                             target=str(target))
    return serialized_project(target_argument, refused)
