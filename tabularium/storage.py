"""Secure storage and directory validation for Tabularium checkpoints and traces.

Fail-closed validation and permission narrowing:
- Checkpoint and trace directories must be owned by the current user.
- Symlinks are strictly refused (CHECKPOINT_DIR_UNSAFE).
- Existing wide permissions (group/other bits) are narrowed to 0700 for directories
  and 0600 for data files (.db, .execution, .jsonl), with a one-line stderr notice.
- New directories are created mode 0700 regardless of umask.
- SQLite databases and sidecars are initialized mode 0600 regardless of umask.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path


CHECKPOINT_DIR_UNSAFE = "CHECKPOINT_DIR_UNSAFE"


class CheckpointDirUnsafe(RuntimeError):
    """Raised when a checkpoint or trace directory fails safety checks."""

    def __init__(self, path: Path | str, reason: str,
                 code: str = CHECKPOINT_DIR_UNSAFE) -> None:
        super().__init__(f"Checkpoint directory '{path}' is unsafe: {reason}")
        self.path = Path(path)
        self.reason = reason
        self.code = code


def _ancestors_under_temp(target_dir: Path) -> list[Path]:
    """Return all directories under tempfile.gettempdir() leading to target_dir.

    Never includes the system tempdir itself (e.g. /tmp), only descendants
    such as ``tabularium-os`` and ``checkpoints``.
    """
    temp_dir = Path(tempfile.gettempdir()).resolve()
    resolved_target = Path(target_dir)

    # First try without resolve to preserve symlink components
    try:
        rel = resolved_target.relative_to(Path(tempfile.gettempdir()))
        parts = rel.parts
        base = Path(tempfile.gettempdir())
    except ValueError:
        try:
            rel = resolved_target.resolve().relative_to(temp_dir)
            parts = rel.parts
            base = temp_dir
        except ValueError:
            return [target_dir]

    ancestors: list[Path] = []
    cur = base
    for part in parts:
        cur = cur / part
        ancestors.append(cur)
    return ancestors


def _check_ownership_and_mode(p: Path, is_dir: bool = True) -> tuple[int, bool]:
    """Check ownership, symlinks, and mode for an existing path.

    Returns (current_mode, needs_narrowing).
    Raises CheckpointDirUnsafe if symlink or foreign owner detected.
    """
    # 1. Symlink check (lstat without following links)
    if os.path.islink(str(p)):
        raise CheckpointDirUnsafe(p, "path is a symlink")

    try:
        st_l = os.lstat(p)
    except FileNotFoundError:
        return 0, False

    if stat.S_ISLNK(st_l.st_mode):
        raise CheckpointDirUnsafe(p, "path is a symlink")

    current_uid = os.getuid()

    # Query os.stat as well to honor any test patches on os.stat
    try:
        st_s = os.stat(p)
    except FileNotFoundError:
        st_s = st_l

    for st in (st_l, st_s):
        uid = getattr(st, "st_uid", None)
        if uid is not None and uid != current_uid:
            raise CheckpointDirUnsafe(
                p, f"owned by another user (uid {uid} != {current_uid})")

    mode = stat.S_IMODE(st_l.st_mode)
    needs_narrow = (mode & 0o077) != 0
    return mode, needs_narrow


def _is_shared_system_dir(dir_path: Path) -> bool:
    """/, the temp root itself, or any sticky world-writable directory (e.g. /tmp).

    Such a directory is never narrowed: as root that would lock every other
    user and service out of it.
    """
    try:
        resolved = dir_path.resolve()
        if resolved == Path("/") or resolved == Path(tempfile.gettempdir()).resolve():
            return True
        mode = os.lstat(dir_path).st_mode
    except OSError:
        return True
    return bool(mode & stat.S_ISVTX) and bool(mode & stat.S_IWOTH)


def _chmod_nofollow(path: Path, mode: int, *, directory: bool) -> None:
    """chmod through a descriptor opened with O_NOFOLLOW, never through a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _narrow_dir(dir_path: Path) -> None:
    """Narrow directory to 0700, files inside to 0600, and emit notice to stderr."""
    if _is_shared_system_dir(dir_path):
        return
    _chmod_nofollow(dir_path, 0o700, directory=True)
    sys.stderr.write(f"Notice: narrowed permissions on {dir_path} to 0700\n")
    sys.stderr.flush()

    try:
        for item in dir_path.iterdir():
            try:
                if os.path.islink(str(item)):
                    continue
                ist = os.lstat(item)
                if stat.S_ISREG(ist.st_mode) and ist.st_uid == os.getuid():
                    if (stat.S_IMODE(ist.st_mode) & 0o077) != 0:
                        _chmod_nofollow(item, 0o600, directory=False)
            except OSError:
                pass
    except OSError:
        pass


def _verify_and_prepare_dir(dir_path: Path) -> None:
    """Validate or create a single directory with mode 0700."""
    if dir_path.exists() or os.path.islink(str(dir_path)):
        _mode, needs_narrow = _check_ownership_and_mode(dir_path, is_dir=True)
        if not dir_path.is_dir() and not os.path.islink(str(dir_path)):
            raise CheckpointDirUnsafe(dir_path, "path is not a directory")
        if needs_narrow:
            _narrow_dir(dir_path)
    else:
        try:
            os.mkdir(dir_path, 0o700)
            os.chmod(dir_path, 0o700)
        except FileExistsError:
            _verify_and_prepare_dir(dir_path)


def prepare_dir_hierarchy(target_dir: Path) -> None:
    """Ensure all ancestors under tempdir and target_dir itself are safe and 0700."""
    for d in _ancestors_under_temp(target_dir):
        _verify_and_prepare_dir(d)


def verify_dir_hierarchy_if_exists(target_dir: Path) -> None:
    """Validate existing directories in hierarchy; never creates missing ones."""
    for d in _ancestors_under_temp(target_dir):
        if d.exists() or os.path.islink(str(d)):
            _mode, needs_narrow = _check_ownership_and_mode(d, is_dir=True)
            if not d.is_dir() and not os.path.islink(str(d)):
                raise CheckpointDirUnsafe(d, "path is not a directory")
            if needs_narrow:
                _narrow_dir(d)


def prepare_secure_file(file_path: Path) -> None:
    """Ensure directory is ready and create file with 0600 mode if missing."""
    prepare_dir_hierarchy(file_path.parent)

    if file_path.exists() or os.path.islink(str(file_path)):
        _mode, needs_narrow = _check_ownership_and_mode(file_path, is_dir=False)
        if needs_narrow:
            os.chmod(file_path, 0o600)
    else:
        # Pre-create mode 0600 before SQLite or other tools open it
        try:
            fd = os.open(file_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        except FileExistsError:
            _mode, needs_narrow = _check_ownership_and_mode(file_path, is_dir=False)
            if needs_narrow:
                os.chmod(file_path, 0o600)


def write_secure_file(file_path: Path, content: str) -> None:
    """Write text atomically with mode 0600, validating directory first."""
    prepare_dir_hierarchy(file_path.parent)

    if file_path.exists() or os.path.islink(str(file_path)):
        _check_ownership_and_mode(file_path, is_dir=False)

    # O_NOFOLLOW: a symlink placed after the checks must not get its target truncated.
    fd = os.open(file_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def prepare_checkpoint_storage(checkpoint_path: Path | str) -> Path:
    """Validate and prepare directory and secure database file for checkpoint."""
    p = Path(checkpoint_path)
    prepare_secure_file(p)
    return p


def verify_checkpoint_storage_if_exists(checkpoint_path: Path | str) -> None:
    """Validate checkpoint directory and files if they exist; creates nothing."""
    p = Path(checkpoint_path)
    verify_dir_hierarchy_if_exists(p.parent)
    if p.exists() or os.path.islink(str(p)):
        _mode, needs_narrow = _check_ownership_and_mode(p, is_dir=False)
        if needs_narrow:
            os.chmod(p, 0o600)
    marker = p.with_suffix(".execution")
    if marker.exists() or os.path.islink(str(marker)):
        _mode, needs_narrow = _check_ownership_and_mode(marker, is_dir=False)
        if needs_narrow:
            os.chmod(marker, 0o600)


def prepare_trace_storage(trace_path: Path | str) -> Path:
    """Validate and prepare directory and secure file for trace logs."""
    p = Path(trace_path)
    prepare_secure_file(p)
    return p
