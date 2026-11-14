"""Detached worktree creation, inspection, diffing, and cleanup."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


_SAFE_GIT_CONFIG = (
    "-c", "core.quotepath=false",
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
)


class WorktreeError(RuntimeError):
    """A worktree operation was refused or failed; nothing was applied."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Worktree:
    """A detached worktree plus the identity recorded before the worker ran."""

    def __init__(self, parent: Path, path: Path, git_dir: Path, dotgit: bytes):
        self.parent = parent
        self.path = path
        self.git_dir = git_dir
        self.dotgit = dotgit

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)


def _worktree_git(worktree: Worktree, *args: str, attr_source: str = "") -> str:
    # --attr-source makes git read .gitattributes from the base commit, so a
    # worker-written .gitattributes cannot select a filter/diff driver.
    attr = [f"--attr-source={attr_source}"] if attr_source else []
    try:
        proc = subprocess.run(
            ["git", f"--git-dir={worktree.git_dir}", f"--work-tree={worktree.path}",
             *attr, *_SAFE_GIT_CONFIG, *args],
            cwd=str(worktree.path), capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorktreeError("GIT_OPERATION_FAILED",
                            f"git {args[0]} failed in the worktree") from exc
    return proc.stdout


def create_worktree(target: Path | str, execution_id: str) -> Worktree:
    """Create a detached git worktree under a fresh private directory."""
    parent = Path(tempfile.mkdtemp(prefix=f"tabu-wt-{execution_id}-"))
    path = parent / "wt"
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), "HEAD"],
            cwd=str(target), capture_output=True, text=True, check=True,
        )
        dotgit = (path / ".git").read_bytes()
        text = dotgit.decode("utf-8").strip()
        if not text.startswith("gitdir: "):
            raise WorktreeError("WORKTREE_INVALID", "worktree .git is not a gitdir link")
        git_dir = Path(text[len("gitdir: "):])
        if not git_dir.is_absolute():
            git_dir = (path / git_dir)
        git_dir = git_dir.resolve()
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError) as exc:
        shutil.rmtree(parent, ignore_errors=True)
        raise WorktreeError("WORKTREE_CREATE_FAILED",
                            f"could not create worktree: {type(exc).__name__}") from exc
    except WorktreeError:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    return Worktree(parent, path, git_dir, dotgit)


def cleanup_worktree(target: Path | str, worktree: Worktree) -> None:
    """Delete the worktree files, then prune git's record of it.

    `git worktree remove` would read the worktree (status of untracked and
    modified files) after the worker had it; deleting the directory and
    pruning only touches the main repository.
    """
    shutil.rmtree(worktree.parent, ignore_errors=True)
    try:
        subprocess.run(["git", "worktree", "prune"], cwd=str(target),
                       capture_output=True, check=False)
    except OSError:
        pass


def compute_worktree_diff(worktree: Worktree, base_commit: str) -> str:
    """Stage all changes in worktree and compute diff against base commit."""
    # Keep the legacy facade patch target effective after the implementation
    # moved into this module. The local import avoids a module import cycle.
    from tabularium import change_path as public_api

    try:
        current = (worktree.path / ".git").read_bytes()
    except OSError:
        current = None
    if current != worktree.dotgit:
        raise WorktreeError("WORKTREE_TAMPERED",
                            "the worker changed the worktree's .git link; nothing was staged")
    public_api._worktree_git(worktree, "add", "-A", attr_source=base_commit)
    # Explicit prefixes: a user's diff.noprefix / mnemonicPrefix would change
    # how the parser maps paths ("b/x.py" losing its directory).
    return public_api._worktree_git(worktree, "diff", "--cached", "--no-ext-diff",
                                    "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
                                    base_commit, attr_source=base_commit)
