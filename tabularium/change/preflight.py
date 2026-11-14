"""Target and patch preflight checks for the change flow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from tabularium.change_config import TESTS_NOT_CONFIGURED, TESTS_NOT_CONFIGURED_MESSAGE
from tabularium.change.worktree import Worktree


_UNSUPPORTED_DIFF_MARKERS = (
    "Binary files ", "GIT binary patch", "old mode ", "new mode ",
    # A new executable would be shown as 100755 but tested and applied as 0644.
    "new file mode 100755",
    "rename from ", "rename to ", "copy from ", "copy to ",
    "new file mode 120000", "deleted file mode 120000",
    "new file mode 160000", "Subproject commit ",
)


def resolve_test_command(target: Path | str, command: list[str]) -> list[str]:
    """Point a relative interpreter path inside a project virtualenv at the real venv.

    The isolated copy leaves git-ignored virtualenvs out, so ``.venv/bin/python``
    would not exist under the sandbox root; the absolute spelling lets the
    sandbox bind that venv after its own checks. Any other relative path (a
    tracked script, say) stays relative and runs from the patched copy.
    """
    first = command[0]
    if os.path.isabs(first) or "/" not in first:
        return list(command)
    rel = os.path.normpath(first)
    if rel.startswith(".."):
        return list(command)
    root = Path(target).resolve()
    parent = (root / rel).parent
    while parent != root:
        if (parent / "pyvenv.cfg").is_file():
            return [str(root / rel), *command[1:]]
        parent = parent.parent
    # Normalized, so "./.venv/../x.sh" does not walk through a directory the
    # copy leaves out; "./" keeps it a path rather than a PATH lookup.
    return [f"./{rel}", *command[1:]]


def get_git_user_name(target: Path | str) -> str:
    """Read git config user.name, falling back to $USER."""
    try:
        proc = subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(target),
            capture_output=True,
            text=True,
            check=False,
        )
        name = proc.stdout.strip()
        if name:
            return name
    except OSError:
        pass
    return os.environ.get("USER", "")


def check_git_clean(target: Path | str) -> tuple[bool, str, str]:
    """Verify that target is a git repo and tracked files have no uncommitted changes.

    Changes under .ai/ are ignored so auto-tasks and execution reports do not
    block subsequent --change runs with GIT_DIRTY.

    Returns:
        (clean: bool, error_code: str, head_commit_or_message: str)
    """
    target_str = str(target)
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_str,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return False, "NOT_GIT_REPOSITORY", "Target project is not a git repository."

        # Diff paths are relative to the repository root; a target inside a
        # subdirectory would apply them to the wrong place.
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=target_str,
                             capture_output=True, text=True, check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(target_str).resolve():
            return False, "NOT_REPOSITORY_ROOT", (
                "Target project must be the root of its git repository for --change.")

        # Check for uncommitted tracked changes (ignoring untracked files)
        status_proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "-uno"],
            cwd=target_str,
            capture_output=True,
            check=False,
        )
        if status_proc.returncode != 0:
            return False, "GIT_STATUS_FAILED", "Failed to inspect git status."

        # NUL-separated entries: no quoting, and a rename or copy carries its
        # source as the next entry. A change counts unless every path it
        # touches is under .ai/ (moving a tracked file into .ai/ is dirty).
        entries = status_proc.stdout.decode("utf-8", "surrogateescape").split("\0")
        dirty_lines = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            paths = [entry[3:]]
            if entry[0] in "RC" and index < len(entries):
                paths.append(entries[index])
                index += 1
            if all(Path(p).parts[:1] == (".ai",) for p in paths):
                continue
            dirty_lines.append(entry)

        if dirty_lines:
            return False, "GIT_DIRTY", (
                "Target project has uncommitted tracked changes; "
                "commit or stash them before running with --change."
            )

        head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target_str,
            capture_output=True,
            text=True,
            check=False,
        )
        if head_proc.returncode != 0:
            return False, "GIT_NO_HEAD", "Target repository has no HEAD commit."

        return True, "", head_proc.stdout.strip()
    except OSError as exc:
        return False, "GIT_UNAVAILABLE", f"Git command failed: {exc}"


def load_change_preflight(
    target: Path,
    allow_untested: bool,
    *,
    public_api: Any,
) -> tuple[tuple[str, Path, Any, str] | None, tuple[str, str] | None]:
    """Validate the target, journal, and test configuration before task/worker setup."""
    clean, err_code, base_commit_or_msg = public_api.check_git_clean(target)
    if not clean:
        return None, (err_code, base_commit_or_msg)
    base_commit = base_commit_or_msg

    journal = public_api.apply_journal_path(target)
    if journal is None:
        return None, (
            "JOURNAL_UNAVAILABLE",
            "The git directory could not be located for the apply journal.",
        )
    if journal.exists():
        return None, (
            "INTERRUPTED_APPLY",
            f"An earlier --change apply was interrupted ({journal} lists its files). "
            "Check those files (git diff / git status), restore or keep them, "
            "then delete the journal before the next --change.",
        )

    try:
        change_config = public_api.load_change_config(target)
        current_config_digest = (
            change_config.digest if change_config is not None
            else public_api.config_digest(target)
        )
    except public_api.ChangeConfigError as exc:
        return None, ("CHANGE_CONFIG_INVALID", f"Invalid change configuration: {exc}")

    if change_config is None and not allow_untested:
        return None, (TESTS_NOT_CONFIGURED, TESTS_NOT_CONFIGURED_MESSAGE)

    return (base_commit, journal, change_config, current_config_digest), None


def apply_preflight_reason(target: Path, worktree: Worktree, fp: Any) -> str:
    """Why this patch would be refused at apply time, or "" if it applies exactly.

    Runs before the approval prompt so the user is never asked to approve a
    change that the transactional apply would then refuse. The staged text
    must reproduce the worker's file byte for byte (final newline, CRLF).
    """
    from tabularium.engine.code_gate.parser import apply_patch_to_text

    rel = fp.normalized_path
    dest = Path(target) / rel
    if fp.is_deletion:
        return f"{rel}: deleting files is not supported"
    if fp.is_creation and dest.exists():
        return f"{rel} already exists in the project (untracked or ignored)"
    if not fp.is_creation and not dest.is_file():
        return f"{rel} is not an existing regular file in the project"
    for parent in dest.parents:
        if parent == Path(target) or not parent.is_relative_to(Path(target)):
            break
        if os.path.lexists(parent) and (parent.is_symlink() or not parent.is_dir()):
            return f"{rel}: {parent.relative_to(Path(target))} is not a directory"
    try:
        original = dest.read_text(encoding="utf-8") if dest.exists() else ""
        staged = apply_patch_to_text(original, fp.hunks)
        produced = (worktree.path / rel).read_bytes()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"{rel}: patch cannot be applied ({type(exc).__name__})"
    if staged.encode("utf-8") != produced:
        return (f"{rel}: the patch would not reproduce the worker's file exactly "
                "(line endings or final newline)")
    return ""


def apply_journal_path(target: Path) -> Path | None:
    """Where an in-progress apply records itself: inside the main git dir."""
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=str(target),
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    git_dir = Path(out)
    if not git_dir.is_absolute():
        git_dir = Path(target) / git_dir
    return git_dir.resolve() / "tabu-apply.journal"


def unsupported_diff_reason(raw_diff: str, parsed_count: int) -> str:
    """Why the parsed patches do not cover the whole diff, or "" if they do."""
    # Read the compatibility constant at call time so facade patches remain
    # effective for callers that used tabularium.change_path before the split.
    from tabularium import change_path as public_api

    headers = sum(1 for line in raw_diff.splitlines() if line.startswith("diff --git "))
    if headers != parsed_count:
        return f"{headers} changed paths but {parsed_count} text patches"
    for line in raw_diff.splitlines():
        if line.startswith(public_api._UNSUPPORTED_DIFF_MARKERS):
            return f"unsupported change: {line.strip()[:80]}"
    return ""

