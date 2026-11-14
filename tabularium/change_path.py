"""Governed worktree change path public compatibility surface.

Internal stages live under :mod:`tabularium.change`; public names and patch
targets remain available here for callers and existing tests.
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

from tabularium.project_lock import serialized_execution
from tabularium.change_config import (
    ChangeConfig, ChangeConfigError, TESTS_NOT_CONFIGURED,
    TESTS_NOT_CONFIGURED_MESSAGE, config_digest, load_change_config,
)
from tabularium.terminal import sanitize_terminal
from tabularium.engine.code_gate.changeset import MAX_FILES, apply_changeset_transactional
from tabularium.engine.code_gate import (
    PatchAuthorization, PatchProposal, evaluate_patch, parse_unified_diff,
)
from tabularium.engine.code_gate.isolated_run import SandboxError, run_tests_on_copy
from tabularium.engine.execution.model import ExecutionPlan, PipelineResult
from tabularium.engine.execution.pipeline import start_execution
from tabularium.engine.workers.api import ModelTarget
from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker, find_opencode_executable
from tabularium.engine.workers.cli.worker import NativeCLIWorker, find_native_executable
from tabularium.engine.workers.cli.isolated_process import WorkerIsolation, WORKER_SANDBOX_UNAVAILABLE
from tabularium.change.preflight import (
    resolve_test_command, get_git_user_name, check_git_clean, apply_preflight_reason,
    apply_journal_path, unsupported_diff_reason, _UNSUPPORTED_DIFF_MARKERS,
    load_change_preflight,
)
from tabularium.change.worktree import (
    _SAFE_GIT_CONFIG, WorktreeError, Worktree, create_worktree, cleanup_worktree,
    _worktree_git, compute_worktree_diff,
)
from tabularium.change.persistence import (
    _persist_execution_report, _finalize_candidate_task, _candidate_task_is_active,
    commit_applied_change,
)
from tabularium.change.workers import _API_WORKER_NAMES
from tabularium.change.flow import execute_change_flow


def _worker_isolation_refusal(target, detail, warnings=(), pipeline=None, report_persister=None,
                              task_id=None):
    """Keep setup refusals observable in the result AND the execution report."""
    from tabularium.execution import ProductResult
    result = ProductResult(
        status="REFUSED", code=WORKER_SANDBOX_UNAVAILABLE, target=str(target), pipeline=pipeline,
        message=f"Worker isolation unavailable: {detail}. No unconfined fallback was used.",
        warnings=tuple(warnings),
        change_result={"decision": "REFUSED", "task_id": task_id, "worker_isolation": {
            "status": "UNAVAILABLE", "reason_code": WORKER_SANDBOX_UNAVAILABLE, "detail": detail}},
    )
    actor = get_git_user_name(target) or "USER"
    now = datetime.now(timezone.utc).isoformat()
    _persist_execution_report(target, result, actor, now,
                              report_persister=report_persister,
                              note="Worker isolation refusal")
    return result


@serialized_execution("target")
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
    allow_untested: bool = False,
    allow_unconfined_worker: bool = False,
) -> Any:
    """Contain a CLI worker by default, then gate its worktree diff and ask once."""
    return execute_change_flow(
        target, instruction, task_id=task_id, execution_id=execution_id,
        checkpoint_path=checkpoint_path, worker_name=worker_name, model=model,
        adapter=adapter, collector=collector, workflow=workflow, confirm=confirm,
        custom_verifier=custom_verifier, pre_apply_hook=pre_apply_hook,
        report_persister=report_persister, allow_untested=allow_untested,
        allow_unconfined_worker=allow_unconfined_worker, public_api=sys.modules[__name__],
    )
