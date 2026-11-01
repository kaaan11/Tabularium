"""Governed worktree change path for Tabularium (V2-09 spike A).

Executes writable worker instructions in an isolated git worktree, evaluates
patches through the security gate, requires explicit single-decision human
confirmation, and commits single-file changes atomically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tabularium.change_config import ChangeConfig, ChangeConfigError, config_digest, load_change_config
from tabularium.terminal import sanitize_terminal
from tabularium.engine.code_gate.changeset import MAX_FILES, apply_changeset_transactional
from tabularium.engine.code_gate import (
    PatchAuthorization,
    PatchProposal,
    evaluate_patch,
    parse_unified_diff,
)
from tabularium.engine.code_gate.isolated_run import SandboxError, run_tests_on_copy
from tabularium.engine.execution.model import ExecutionPlan, PipelineResult
from tabularium.engine.execution.pipeline import start_execution
from tabularium.engine.workers.api import APIWorker, ModelTarget
from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker, find_opencode_executable
from tabularium.engine.workers.cli.worker import NativeCLIWorker, find_native_executable


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


def _finalize_candidate_task(
    target: Path,
    task_id: str,
    *,
    status: str,  # "COMPLETED" or "CANCELLED"
    actor: str = "USER",
    timestamp: str | None = None,
    reason_code: str | None = None,
) -> None:
    """Transition candidate task to its terminal status strictly via SCHEMA_TRANSITIONS."""
    from tabularium.task import read_frontmatter, task_transition

    task_path = Path(target) / ".ai" / "tasks" / f"{task_id}.md"
    current_body = ""
    if task_path.exists():
        try:
            rec = read_frontmatter(task_path, Path(target), "Task")
            current_body = rec.body or ""
        except Exception:
            current_body = ""

    if status == "COMPLETED":
        # Strictly follow SCHEMA_TRANSITIONS without force:
        # INBOX -> ANALYZING -> PLANNED -> IN_PROGRESS -> REVIEW -> COMPLETED
        for intermediate in ("ANALYZING", "PLANNED", "IN_PROGRESS", "REVIEW"):
            task_transition(target, task_id, intermediate)
        task_transition(
            target,
            task_id,
            "COMPLETED",
            approval_status="APPROVED",
            approved_by=actor or "USER",
            approved_at=timestamp or datetime.now(timezone.utc).isoformat(),
        )
    elif status == "CANCELLED":
        # SCHEMA_TRANSITIONS allows INBOX -> CANCELLED directly.
        # If already advanced to intermediate states, all of them also transition to CANCELLED.
        body = current_body.rstrip()
        if reason_code:
            body = f"{body}\n\n## Outcome\n\nCancellation reason: {reason_code}\n"
        task_transition(
            target,
            task_id,
            "CANCELLED",
            result=reason_code,
            body=body,
        )


# Every git command that touches the worktree uses the gitdir recorded at
# creation and these settings, so files the worker wrote (a replaced `.git`,
# repository config, hooks, fsmonitor, diff drivers) cannot run code.
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


def _candidate_task_is_active(target: Path, task_id: str) -> bool:
    from tabularium.task import read_frontmatter
    from tabularium.engine.context_loader.discovery import TERMINAL_TASK_STATUSES

    try:
        rec = read_frontmatter(Path(target) / ".ai" / "tasks" / f"{task_id}.md",
                               Path(target), "Task")
    except Exception:
        return False
    status = str(rec.frontmatter.get("status") or "").strip().upper()
    return status not in TERMINAL_TASK_STATUSES


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


def compute_worktree_diff(worktree: Worktree, base_commit: str) -> str:
    """Stage all changes in worktree and compute diff against base commit."""
    try:
        current = (worktree.path / ".git").read_bytes()
    except OSError:
        current = None
    if current != worktree.dotgit:
        raise WorktreeError("WORKTREE_TAMPERED",
                            "the worker changed the worktree's .git link; nothing was staged")
    _worktree_git(worktree, "add", "-A", attr_source=base_commit)
    # Explicit prefixes: a user's diff.noprefix / mnemonicPrefix would change
    # how the parser maps paths ("b/x.py" losing its directory).
    return _worktree_git(worktree, "diff", "--cached", "--no-ext-diff",
                         "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
                         base_commit, attr_source=base_commit)


# Diff content the text parser does not represent. A diff that carries any of
# these would show the user a change that is never applied (or applied
# differently), so it is refused instead.
_UNSUPPORTED_DIFF_MARKERS = (
    "Binary files ", "GIT binary patch", "old mode ", "new mode ",
    # A new executable would be shown as 100755 but tested and applied as 0644.
    "new file mode 100755",
    "rename from ", "rename to ", "copy from ", "copy to ",
    "new file mode 120000", "deleted file mode 120000",
    "new file mode 160000", "Subproject commit ",
)


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


def commit_applied_change(target: Path, paths: list[str], message: str) -> tuple[str, str]:
    """Commit exactly `paths` in the project; returns (commit sha, "") or ("", reason).

    `--only` leaves anything else the user has staged untouched. The user's own
    hooks and signing settings apply, as for any commit they make.
    """
    existing = [p for p in paths if (Path(target) / p).exists()]
    try:
        # --literal-pathspecs: a worker-chosen file name such as "*" must name
        # that file, not glob in the user's other (even untracked) files.
        subprocess.run(["git", "--literal-pathspecs", "add", "--", *existing], cwd=str(target),
                       capture_output=True, text=True, check=True)
        subprocess.run(["git", "--literal-pathspecs", "commit", "--only", "-m", message,
                        "--", *existing],
                       cwd=str(target), capture_output=True, text=True, check=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(target),
                             capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as exc:
        # Leave the index as it was: the change stays in the working tree only.
        subprocess.run(["git", "--literal-pathspecs", "reset", "-q", "--", *existing],
                       cwd=str(target),
                       capture_output=True, check=False)
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        return "", detail[-1][:200] if detail else f"git {exc.cmd[1]} failed"
    except OSError as exc:
        return "", f"git unavailable ({type(exc).__name__})"
    return sha, ""


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
    headers = sum(1 for line in raw_diff.splitlines() if line.startswith("diff --git "))
    if headers != parsed_count:
        return f"{headers} changed paths but {parsed_count} text patches"
    for line in raw_diff.splitlines():
        if line.startswith(_UNSUPPORTED_DIFF_MARKERS):
            return f"unsupported change: {line.strip()[:80]}"
    return ""


def run_change_path(
    target: Path,
    instruction: str,
    *,
    task_id: str | None = None,
    execution_id: str,
    checkpoint_path: str,
    worker_name: str | None = None,
    model: str | None = None,
    adapter: Any = None,
    collector: Any = None,
    workflow: str = "single",
    confirm: Callable[[str], bool] | None = None,
    custom_verifier: Callable[[dict[str, str]], tuple[bool, str]] | None = None,
    pre_apply_hook: Callable[[], None] | None = None,
    report_persister: Callable[..., Any] | None = None,
) -> Any:
    """Run one instruction in an isolated worktree with security gate and single confirmation."""
    from tabularium.execution import ProductResult, _from_pipeline

    # 1. Preflight check: git repository and clean tracked files
    clean, err_code, base_commit_or_msg = check_git_clean(target)
    if not clean:
        return ProductResult(
            status="REFUSED",
            code=err_code,
            message=base_commit_or_msg,
            target=str(target),
        )
    base_commit = base_commit_or_msg

    journal = apply_journal_path(target)
    if journal is None:
        return ProductResult(
            status="REFUSED",
            code="JOURNAL_UNAVAILABLE",
            message="The git directory could not be located for the apply journal.",
            target=str(target),
        )
    if journal.exists():
        return ProductResult(
            status="REFUSED",
            code="INTERRUPTED_APPLY",
            message=(f"An earlier --change apply was interrupted ({journal} lists its files). "
                     "Check those files (git diff / git status), restore or keep them, "
                     "then delete the journal before the next --change."),
            target=str(target),
        )

    # 1b. Load change configuration before creating task or starting worker
    try:
        change_cfg = load_change_config(target)
        cfg_digest = change_cfg.digest if change_cfg is not None else config_digest(target)
    except ChangeConfigError as exc:
        return ProductResult(
            status="REFUSED",
            code="CHANGE_CONFIG_INVALID",
            message=f"Invalid change configuration: {exc}",
            target=str(target),
        )

    warnings: list[str] = []
    is_auto_task = False

    if task_id:
        from tabularium.task import _is_canonical_task_id
        if not _is_canonical_task_id(task_id):
            return ProductResult(
                status="REFUSED",
                code="TASK_ID_INVALID",
                message="--task must be a canonical id like TASK-2026-0001.",
                target=str(target),
            )
        task_file = Path(target) / ".ai" / "tasks" / f"{task_id}.md"
        if task_file.exists():
            from tabularium.task import read_frontmatter
            try:
                rec = read_frontmatter(task_file, Path(target), "Task")
                obj = (rec.frontmatter.get("objective") or "").strip()
                if instruction.strip() != obj:
                    warn_msg = f"Warning: instruction differs from objective of task {task_id}."
                    print(warn_msg, file=sys.stderr)
                    warnings.append(warn_msg)
            except Exception:
                pass
    else:
        from tabularium.task import task_create

        first_line = instruction.strip().splitlines()[0].strip() if instruction.strip() else ""
        candidate_title = first_line[:80]
        candidate_objective = instruction.strip()
        create_res = task_create(target, title=candidate_title, objective=candidate_objective)
        if not create_res.created:
            return ProductResult(
                status="REFUSED",
                code=create_res.code or "TASK_CREATE_FAILED",
                message=create_res.message or "Failed to create candidate task.",
                target=str(target),
            )
        task_id = create_res.record_id
        is_auto_task = True

    # Create worktree
    try:
        worktree = create_worktree(target, execution_id)
    except WorktreeError as exc:
        if is_auto_task and task_id:
            _finalize_candidate_task(target, task_id, status="CANCELLED", reason_code=exc.code)
        return ProductResult(status="REFUSED", code=exc.code, message=str(exc),
                             target=str(target), warnings=tuple(warnings))
    worktree_dir = worktree.path

    try:
        # 2. Setup worker to operate only in worktree
        if adapter is not None:
            selected_worker = adapter
            if hasattr(selected_worker, "cwd"):
                selected_worker.cwd = str(worktree_dir)
        else:
            choice = (worker_name or "").strip().casefold()
            selected_model = (model or os.environ.get("TABU_MODEL") or "").strip()
            if choice in {"native", "codex", "codex-cli"}:
                exe = find_native_executable()
                if not exe:
                    if is_auto_task and task_id:
                        _finalize_candidate_task(
                            target, task_id, status="CANCELLED", reason_code="WORKER_UNAVAILABLE"
                        )
                    return ProductResult(
                        status="REFUSED",
                        code="WORKER_UNAVAILABLE",
                        message="The selected native worker is unavailable.",
                        target=str(target),
                        warnings=tuple(warnings),
                    )
                selected_worker = NativeCLIWorker(
                    executable=exe, cwd=worktree_dir, sandbox="workspace-write"
                )
            elif choice in {"opencode", "opencode-cli"}:
                exe = find_opencode_executable()
                if not exe:
                    if is_auto_task and task_id:
                        _finalize_candidate_task(
                            target, task_id, status="CANCELLED", reason_code="WORKER_UNAVAILABLE"
                        )
                    return ProductResult(
                        status="REFUSED",
                        code="WORKER_UNAVAILABLE",
                        message="The selected OpenCode worker is unavailable.",
                        target=str(target),
                        warnings=tuple(warnings),
                    )
                selected_worker = OpenCodeCLIWorker(
                    executable=exe,
                    model=selected_model or None,
                    cwd=worktree_dir,
                    change_mode=True,
                )
            elif choice in {"api", "api-model", "litellm"}:
                if not selected_model:
                    if is_auto_task and task_id:
                        _finalize_candidate_task(
                            target, task_id, status="CANCELLED", reason_code="WORKER_CONFIGURATION_MISSING"
                        )
                    return ProductResult(
                        status="REFUSED",
                        code="WORKER_CONFIGURATION_MISSING",
                        message="The API worker requires a resolved model.",
                        target=str(target),
                        warnings=tuple(warnings),
                    )
                selected_worker = APIWorker(model_target=ModelTarget(selected_model))
            elif choice:
                if is_auto_task and task_id:
                    _finalize_candidate_task(
                        target, task_id, status="CANCELLED", reason_code="WORKER_SELECTION_INVALID"
                    )
                return ProductResult(
                    status="REFUSED",
                    code="WORKER_SELECTION_INVALID",
                    message=f"Unknown worker '{worker_name}'; choose native, opencode or api.",
                    target=str(target),
                    warnings=tuple(warnings),
                )
            else:
                exe = find_native_executable()
                if exe:
                    selected_worker = NativeCLIWorker(
                        executable=exe, cwd=worktree_dir, sandbox="workspace-write"
                    )
                else:
                    opencode_bin = find_opencode_executable()
                    if opencode_bin:
                        selected_worker = OpenCodeCLIWorker(
                            executable=opencode_bin,
                            model=selected_model or None,
                            cwd=worktree_dir,
                            change_mode=True,
                        )
                    elif selected_model:
                        selected_worker = APIWorker(model_target=ModelTarget(selected_model))
                    else:
                        if is_auto_task and task_id:
                            _finalize_candidate_task(
                                target, task_id, status="CANCELLED", reason_code="WORKER_SELECTION_REQUIRED"
                            )
                        return ProductResult(
                            status="REFUSED",
                            code="WORKER_SELECTION_REQUIRED",
                            message="No suitable worker available for --change.",
                            target=str(target),
                            warnings=tuple(warnings),
                        )

        resolved_model = getattr(selected_worker, "model_target", None)
        if model and resolved_model is None:
            resolved_model = ModelTarget(model)

        plan = ExecutionPlan(
            project_root=str(target),
            task_id=task_id,
            intent=instruction.strip(),
            execution_id=execution_id,
            checkpoint_path=checkpoint_path,
            workflow=workflow,
            adapter=selected_worker,
            model_target=(
                resolved_model
                if getattr(selected_worker.descriptor, "category", "") == "API_MODEL"
                else None
            ),
        )

        try:
            pipeline_result = start_execution(plan, collector)
        except Exception as exc:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="WORKER_UNAVAILABLE"
                )
            return ProductResult(
                status="REFUSED",
                code="WORKER_UNAVAILABLE",
                message="The selected worker could not be started.",
                target=str(target),
                warnings=tuple(warnings),
            )

        if pipeline_result.status != "COMPLETED":
            code = pipeline_result.reason_codes[0] if pipeline_result.reason_codes else "PIPELINE_FAILED"
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code=code
                )
            res = _from_pipeline(target, pipeline_result)
            if warnings:
                res = ProductResult(
                    status=res.status,
                    code=res.code,
                    message=res.message,
                    target=res.target,
                    pipeline=res.pipeline,
                    approval_request=res.approval_request,
                    warnings=tuple(warnings) + res.warnings,
                    change_result=res.change_result,
                )
            return res

        # 3. Worktree diff
        try:
            raw_diff = compute_worktree_diff(worktree, base_commit)
        except WorktreeError as exc:
            if is_auto_task and task_id:
                _finalize_candidate_task(target, task_id, status="CANCELLED", reason_code=exc.code)
            return ProductResult(status="REFUSED", code=exc.code, message=str(exc),
                                 target=str(target), pipeline=pipeline_result,
                                 warnings=tuple(warnings))
        if not raw_diff.strip():
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="NO_CHANGES_PRODUCED"
                )
            return ProductResult(
                status="COMPLETED",
                code="NO_CHANGES_PRODUCED",
                message="The worker produced no change.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            )

        # 4. Gate evaluation
        try:
            file_patches = parse_unified_diff(raw_diff)
        except Exception as exc:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="DIFF_SYNTAX_INVALID"
                )
            return ProductResult(
                status="REFUSED",
                code="DIFF_SYNTAX_INVALID",
                message=f"Diff syntax parse error: {exc}",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            )

        reason = unsupported_diff_reason(raw_diff, len(file_patches))
        if reason:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="DIFF_UNSUPPORTED"
                )
            return ProductResult(
                status="REFUSED",
                code="DIFF_UNSUPPORTED",
                message=f"The change cannot be applied as shown ({reason}); nothing was applied.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            )

        if not file_patches:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="NO_CHANGES_PRODUCED"
                )
            return ProductResult(
                status="COMPLETED",
                code="NO_CHANGES_PRODUCED",
                message="The worker produced no change.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            )

        target_files = tuple(fp.normalized_path for fp in file_patches)
        proposal = PatchProposal(
            proposal_id=f"prop-{execution_id}",
            execution_id=execution_id,
            raw_diff=raw_diff,
            target_files=target_files,
            task_id=task_id or "",
        )

        evaluation = evaluate_patch(proposal, target)
        if not evaluation.allowed:
            code = evaluation.reason_codes[0] if evaluation.reason_codes else "GATE_FAILED"
            details = "; ".join(f"{c.name}: {c.detail}" for c in evaluation.failed) or code
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code=code
                )
            return ProductResult(
                status="REFUSED",
                code=code,
                message=f"Change proposal failed security gate ({code}): {details}",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            )

        # A change set applies all of its files or none of them; its size is bounded.
        if len(target_files) > MAX_FILES:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="CHANGESET_TOO_LARGE"
                )
            return ProductResult(
                status="REFUSED",
                code="CHANGESET_TOO_LARGE",
                message=(
                    f"The change touches {len(target_files)} files; at most {MAX_FILES} "
                    f"can be applied together."
                ),
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            )

        reason = next((r for fp in file_patches
                       if (r := apply_preflight_reason(target, worktree, fp))), "")
        if reason:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="PATCH_NOT_APPLICABLE"
                )
            return ProductResult(
                status="REFUSED",
                code="PATCH_NOT_APPLICABLE",
                message=f"Not asking for approval: {reason}. Nothing was applied.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            )

        # 5. Single human confirmation
        is_interactive = confirm is not None or sys.stdin.isatty()
        if not is_interactive:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="APPROVAL_REQUIRED"
                )
            change_info = {
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "AWAITING_APPROVAL",
                "apply_result": None,
            }
            return ProductResult(
                status="AWAITING_APPROVAL",
                code="APPROVAL_REQUIRED",
                message=("Applying a change needs an interactive terminal. Nothing was "
                         "applied and the proposal was not kept; re-run the command "
                         "in a terminal to review and approve it."),
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            )

        # 5b. Run isolated tests on project copy if configured
        test_info: dict[str, Any]
        test_duration = 0.0
        if change_cfg is not None:
            # The overlay is the approved diff applied to the project, the same
            # text the transactional apply stages. It is never re-read from the
            # worktree, which a lingering worker process could still change;
            # the apply-time verifier then refuses a worktree that drifted.
            from tabularium.engine.code_gate.parser import apply_patch_to_text
            overlay: dict[str, str] = {}
            for fp in file_patches:
                rel = fp.normalized_path
                dest = Path(target) / rel
                try:
                    original = dest.read_text(encoding="utf-8") if dest.exists() else ""
                    overlay[rel] = apply_patch_to_text(original, fp.hunks)
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    if is_auto_task and task_id:
                        _finalize_candidate_task(
                            target, task_id, status="CANCELLED", reason_code="DIFF_UNSUPPORTED"
                        )
                    return ProductResult(
                        status="REFUSED",
                        code="DIFF_UNSUPPORTED",
                        message=(f"{rel}: the approved diff could not be staged for the isolated "
                                 f"test copy ({type(exc).__name__}); nothing was applied."),
                        target=str(target),
                        pipeline=pipeline_result,
                        warnings=tuple(warnings),
                    )

            cmd_display = " ".join(change_cfg.test_command)
            print(sanitize_terminal(f"Running tests in an isolated copy: {cmd_display}"))
            t0 = time.monotonic()
            try:
                run_res = run_tests_on_copy(
                    target,
                    overlay,
                    resolve_test_command(target, change_cfg.test_command),
                    timeout=float(change_cfg.timeout_seconds),
                )
                test_duration = time.monotonic() - t0
            except (SandboxError, OSError) as exc:
                # A copy that cannot be built (disk full, unreadable file) is a
                # setup failure like any other: refused, never run unconfined.
                test_duration = time.monotonic() - t0
                code = exc.code if isinstance(exc, SandboxError) else "SANDBOX_UNAVAILABLE"
                detail = exc.detail if isinstance(exc, SandboxError) else f"{type(exc).__name__}: {exc}"
                if is_auto_task and task_id:
                    _finalize_candidate_task(
                        target, task_id, status="CANCELLED", reason_code=code
                    )
                change_info = {
                    "proposal": proposal,
                    "binding": proposal.binding(),
                    "diff_digest": proposal.diff_digest,
                    "gate_result": "ALLOWED",
                    "decision": "REFUSED",
                    "apply_result": None,
                    "tests": {
                        "status": "FAILED",
                        "command": list(change_cfg.test_command),
                        "duration_s": test_duration,
                        "timed_out": False,
                        "truncated": False,
                    },
                }
                return ProductResult(
                    status="REFUSED",
                    code=code,
                    message=f"Sandbox error ({code}): {detail}",
                    target=str(target),
                    pipeline=pipeline_result,
                    change_result=change_info,
                    warnings=tuple(warnings),
                )

            if not run_res.passed:
                code = "ISOLATED_TESTS_FAILED"
                if is_auto_task and task_id:
                    _finalize_candidate_task(
                        target, task_id, status="CANCELLED", reason_code=code
                    )
                print("--- Test output ---")
                print(sanitize_terminal(run_res.output()))
                print("-------------------")
                if run_res.truncated:
                    print("Warning: test output was truncated.")

                if run_res.timed_out:
                    msg = f"Isolated tests timed out after {change_cfg.timeout_seconds}s; change was not applied."
                else:
                    msg = f"Isolated tests failed (returncode {run_res.returncode}); change was not applied."

                test_info = {
                    "status": "FAILED",
                    "command": list(change_cfg.test_command),
                    "duration_s": test_duration,
                    "timed_out": run_res.timed_out,
                    "truncated": run_res.truncated,
                }
                change_info = {
                    "proposal": proposal,
                    "binding": proposal.binding(),
                    "diff_digest": proposal.diff_digest,
                    "gate_result": "ALLOWED",
                    "decision": "REFUSED",
                    "apply_result": None,
                    "tests": test_info,
                }
                return ProductResult(
                    status="REFUSED",
                    code=code,
                    message=msg,
                    target=str(target),
                    pipeline=pipeline_result,
                    change_result=change_info,
                    warnings=tuple(warnings),
                )

            test_info = {
                "status": "PASSED",
                "command": list(change_cfg.test_command),
                "duration_s": test_duration,
                "timed_out": False,
                "truncated": run_res.truncated,
            }
        else:
            no_cfg_warn = "Tests: NOT RUN (no .ai/change.yaml)"
            warnings.append(no_cfg_warn)
            test_info = {
                "status": "NOT_CONFIGURED",
                "command": [],
                "duration_s": 0.0,
                "timed_out": False,
                "truncated": False,
            }

        if change_cfg is not None:
            print(f"Tests: PASSED ({test_duration:.2f} s)")
        else:
            print("Tests: NOT RUN (no .ai/change.yaml)")

        if confirm is not None:
            approved = confirm("Uygula? [y/N]")
        else:
            print(sanitize_terminal(f"Target files ({len(target_files)}): {', '.join(target_files)}"))
            print(f"Gate evaluation: PASSED (diff_digest={proposal.diff_digest})")
            print("--- Diff preview ---")
            print(sanitize_terminal(proposal.raw_diff))
            print("--------------------")
            try:
                resp = input("Uygula? [y/N]: ").strip().casefold()
                approved = resp in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                approved = False

        if not approved:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="APPROVAL_REJECTED"
                )
            change_info = {
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "REJECTED",
                "apply_result": None,
                "tests": test_info,
            }
            return ProductResult(
                status="REFUSED",
                code="APPROVAL_REJECTED",
                message="Change application rejected by user.",
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            )

        actor = get_git_user_name(target) or os.environ.get("USER", "unknown")
        now_iso = datetime.now(timezone.utc).isoformat()

        # Check target hasn't changed between tests/approval and apply (CAS check)
        # check_git_clean skips .ai/, so the test configuration is compared on its own.
        clean_now, clean_code, current_head = check_git_clean(target)
        if not clean_now or current_head != base_commit or config_digest(target) != cfg_digest:
            code = "TARGET_CHANGED"
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code=code, actor=actor, timestamp=now_iso
                )
            change_info = {
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "APPROVED",
                "apply_result": None,
                "tests": test_info,
            }
            return ProductResult(
                status="REFUSED",
                code=code,
                message="Target project changed after tests/approval; nothing was applied.",
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            )

        authorization = PatchAuthorization(
            proposal_id=proposal.proposal_id,
            execution_id=proposal.execution_id,
            diff_digest=proposal.diff_digest,
            authorized_by=actor,
            authorized_at=now_iso,
            binding=proposal.binding(),
            note="Single confirmation change authorization",
        )

        # Verifier with hook support for testing CAS / concurrent modification
        def verifier_wrapper(staged: dict[str, str]) -> tuple[bool, str]:
            if pre_apply_hook is not None:
                pre_apply_hook()
            if custom_verifier is not None:
                return custom_verifier(staged)
            # Every staged file must reproduce exactly what the worker wrote in
            # the worktree; anything else means the diff did not round-trip.
            for rel in target_files:
                produced = worktree_dir / rel
                if rel not in staged or not produced.is_file():
                    return False, f"{rel}: staged or worker file missing"
                if staged[rel].encode("utf-8") != produced.read_bytes():
                    return False, f"{rel}: staged content differs from worker output"
            return True, "staged content matches worker output"

        # 6. Apply every file of the change set, or none of them
        apply_res = apply_changeset_transactional(
            target,
            proposal,
            authorization,
            verifier=verifier_wrapper,
            journal_path=journal,
        )

        if not apply_res.applied:
            code = apply_res.reason_codes[0] if apply_res.reason_codes else "PATCH_APPLY_FAILED"
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code=code, actor=actor, timestamp=now_iso
                )
            change_info = {
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "APPROVED",
                "apply_result": apply_res.status,
                "tests": test_info,
            }
            return ProductResult(
                status="REFUSED",
                code=code,
                message=f"Change application failed ({code}): {', '.join(apply_res.reason_codes)}",
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            )

        # 7. Record
        change_info = {
            "proposal": proposal,
            "binding": proposal.binding(),
            "diff_digest": proposal.diff_digest,
            "gate_result": "ALLOWED",
            "decision": "APPROVED",
            "apply_result": apply_res.status,
            "tests": test_info,
        }
        product_res = ProductResult(
            status="COMPLETED",
            code="COMPLETED",
            message="Change applied successfully.",
            target=str(target),
            pipeline=pipeline_result,
            change_result=change_info,
            warnings=tuple(warnings),
        )

        if is_auto_task and task_id:
            _finalize_candidate_task(
                target, task_id, status="COMPLETED", actor=actor, timestamp=now_iso
            )

        # Apply report persistence to .ai/reports
        report_path = ""
        if report_persister is not None:
            report_persister(target, product_res, actor, now_iso)
        else:
            from tabularium.execution import (
                apply_execution_report_proposal,
                build_execution_report_proposal,
            )
            report_prop_item = build_execution_report_proposal(
                target, product_res, author=actor
            )
            if report_prop_item and report_prop_item.ok:
                persisted = apply_execution_report_proposal(
                    target,
                    report_prop_item.proposal,
                    authorized_by=actor,
                    authorized_at=now_iso,
                    note="Change execution report persistence",
                )
                if getattr(persisted, "written", False):
                    report_path = report_prop_item.proposal.target_path

        # Commit the approved change with its governance records, so the next
        # --change starts from a clean tree and history carries the approval.
        commit_paths = list(target_files)
        if task_id:
            commit_paths.append(f".ai/tasks/{task_id}.md")
        if report_path:
            commit_paths.append(report_path)
        subject = instruction.strip().splitlines()[0][:72] if instruction.strip() else "change"
        if change_cfg is not None:
            tests_line = f"Tests: passed ({' '.join(change_cfg.test_command)})"
        else:
            tests_line = "Tests: not configured"
        message = (f"tabu: {subject}\n\n"
                   f"Task: {task_id or '-'}\n"
                   f"Approved-by: {actor} at {now_iso}\n"
                   f"Diff-digest: {proposal.diff_digest}\n"
                   f"{tests_line}\n")
        sha, failure = commit_applied_change(target, commit_paths, message)
        change_info["commit"] = sha or None
        if failure:
            warn = (f"Change applied but not committed ({failure}); "
                    "commit it before the next --change.")
            print(sanitize_terminal(f"Warning: {warn}"), file=sys.stderr)
            product_res = ProductResult(
                status=product_res.status, code=product_res.code,
                message=product_res.message, target=product_res.target,
                pipeline=product_res.pipeline, change_result=change_info,
                warnings=product_res.warnings + (warn,),
            )

        return product_res

    finally:
        # 8. Clean up worktree unconditionally
        cleanup_worktree(target, worktree)
        # Safety net: whichever path ended the run (a return without its own
        # closing, an exception, Ctrl-C), a candidate Task never stays active.
        if is_auto_task and task_id and _candidate_task_is_active(target, task_id):
            _finalize_candidate_task(target, task_id, status="CANCELLED",
                                     reason_code="RUN_ABORTED")
