"""Execution report, task outcome, and approved-change commit persistence."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _persist_execution_report(target, result, actor, timestamp, *,
                              report_persister=None, note="Change execution report persistence"):
    """Persist one change-path result report, if the report proposal is valid."""
    from tabularium.execution import (apply_execution_report_proposal,
                                      build_execution_report_proposal)

    if report_persister is not None:
        report_persister(target, result, actor, timestamp)
        return ""

    report_prop_item = build_execution_report_proposal(target, result, author=actor)
    if report_prop_item and report_prop_item.ok:
        persisted = apply_execution_report_proposal(
            target,
            report_prop_item.proposal,
            authorized_by=actor,
            authorized_at=timestamp,
            note=note,
        )
        if getattr(persisted, "written", False):
            return report_prop_item.proposal.target_path
    return ""


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


def persist_approved_change(
    target: Path,
    instruction: str,
    task_id: str | None,
    is_auto_task: bool,
    actor: str,
    timestamp: str,
    proposal: Any,
    apply_result: Any,
    test_info: dict[str, Any],
    worker_isolation: dict[str, Any],
    pipeline_result: Any,
    warnings: list[str],
    change_config: Any,
    allow_unconfined_worker: bool,
    report_persister: Any,
    *,
    public_api: Any,
) -> Any:
    """Record an approved apply, persist its report, and commit its records."""
    from tabularium.execution import ProductResult

    change_info = {
        "worker_isolation": worker_isolation,
        "proposal": proposal,
        "binding": proposal.binding(),
        "diff_digest": proposal.diff_digest,
        "gate_result": "ALLOWED",
        "decision": "APPROVED",
        "apply_result": apply_result.status,
        "tests": test_info,
    }
    product_result = ProductResult(
        status="COMPLETED",
        code="COMPLETED",
        message="Change applied successfully.",
        target=str(target),
        pipeline=pipeline_result,
        change_result=change_info,
        warnings=tuple(warnings),
    )

    if is_auto_task and task_id:
        public_api._finalize_candidate_task(
            target, task_id, status="COMPLETED", actor=actor, timestamp=timestamp
        )

    report_path = public_api._persist_execution_report(
        target, product_result, actor, timestamp, report_persister=report_persister
    )

    commit_paths = list(proposal.target_files)
    if task_id:
        commit_paths.append(f".ai/tasks/{task_id}.md")
    if report_path:
        commit_paths.append(report_path)
    subject = instruction.strip().splitlines()[0][:72] if instruction.strip() else "change"
    if change_config is not None:
        tests_line = f"Tests: passed ({' '.join(change_config.test_command)})"
    else:
        tests_line = "Tests: not run (no .ai/change.yaml; --allow-untested)"
    message = (f"tabu: {subject}\n\n"
               f"Task: {task_id or '-'}\n"
               f"Approved-by: {actor} at {timestamp}\n"
               f"Diff-digest: {proposal.diff_digest}\n"
               f"{tests_line}\n"
               f"Worker-isolation: {'disabled (--allow-unconfined-worker)' if allow_unconfined_worker else 'bubblewrap (network allowed)'}\n")
    sha, failure = public_api.commit_applied_change(target, commit_paths, message)
    change_info["commit"] = sha or None
    if failure:
        warning = (f"Change applied but not committed ({failure}); "
                   "commit it before the next --change.")
        print(public_api.sanitize_terminal(f"Warning: {warning}"), file=sys.stderr)
        product_result = ProductResult(
            status=product_result.status,
            code=product_result.code,
            message=product_result.message,
            target=product_result.target,
            pipeline=product_result.pipeline,
            change_result=change_info,
            warnings=product_result.warnings + (warning,),
        )

    return product_result

