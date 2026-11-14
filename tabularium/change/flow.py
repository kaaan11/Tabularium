"""Explicit stage orchestration for the governed change flow."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tabularium.change.workers import select_change_worker
from tabularium.change.testing import run_configured_tests
from tabularium.change.approval import request_change_approval, authorize_and_apply
from tabularium.change.persistence import persist_approved_change
from tabularium.change.preflight import load_change_preflight


def execute_change_flow(
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
    public_api: Any,
) -> Any:
    """Contain a CLI worker by default, then gate its worktree diff and ask once."""
    from tabularium.execution import ProductResult, _from_pipeline

    # Resolve calls through the compatibility facade so existing public patch targets remain effective.
    _API_WORKER_NAMES = public_api._API_WORKER_NAMES
    _persist_execution_report = public_api._persist_execution_report
    get_git_user_name = public_api.get_git_user_name
    _finalize_candidate_task = public_api._finalize_candidate_task
    create_worktree = public_api.create_worktree
    WorktreeError = public_api.WorktreeError
    NativeCLIWorker = public_api.NativeCLIWorker
    OpenCodeCLIWorker = public_api.OpenCodeCLIWorker
    WorkerIsolation = public_api.WorkerIsolation
    WORKER_SANDBOX_UNAVAILABLE = public_api.WORKER_SANDBOX_UNAVAILABLE
    ModelTarget = public_api.ModelTarget
    ExecutionPlan = public_api.ExecutionPlan
    start_execution = public_api.start_execution
    _worker_isolation_refusal = public_api._worker_isolation_refusal
    compute_worktree_diff = public_api.compute_worktree_diff
    parse_unified_diff = public_api.parse_unified_diff
    unsupported_diff_reason = public_api.unsupported_diff_reason
    PatchProposal = public_api.PatchProposal
    evaluate_patch = public_api.evaluate_patch
    MAX_FILES = public_api.MAX_FILES
    apply_preflight_reason = public_api.apply_preflight_reason
    cleanup_worktree = public_api.cleanup_worktree
    _candidate_task_is_active = public_api._candidate_task_is_active

    # 0. The API worker returns text and writes no files, while this path reads
    # the change from the worktree diff. Refuse it before any provider call.
    choice = (worker_name or "").strip().casefold()
    adapter_category = getattr(getattr(adapter, "descriptor", None), "category", "")
    if adapter_category == "API_MODEL" or (adapter is None and choice in _API_WORKER_NAMES):
        return ProductResult(
            status="REFUSED",
            code="CHANGE_WORKER_UNSUPPORTED",
            message=("--change needs a worker that edits files: use --worker native "
                     "(Codex CLI) or --worker opencode. The API worker only returns "
                     "text; run without --change to use it."),
            target=str(target),
        )

    # 1. Target, interrupted-apply, and test-configuration preflight.
    preflight_state, preflight_refusal = load_change_preflight(
        target, allow_untested, public_api=public_api
    )
    if preflight_refusal:
        code, message = preflight_refusal
        return ProductResult(
            status="REFUSED",
            code=code,
            message=message,
            target=str(target),
        )
    base_commit, journal, change_cfg, cfg_digest = preflight_state

    warnings: list[str] = []
    worker_isolation = {"status": "DISABLED" if allow_unconfined_worker else "ENFORCED",
                        "override": allow_unconfined_worker}
    if allow_unconfined_worker:
        warning = "Worker isolation: DISABLED for this run (--allow-unconfined-worker); worker has host user access."
        warnings.append(warning)
        print(warning, file=sys.stderr)

    def finish_worker_result(result):
        """Attach the isolation record and persist every post-worker outcome."""
        change_info = result.change_result
        if isinstance(change_info, dict):
            change_info = dict(change_info)
            change_info.setdefault("worker_isolation", dict(worker_isolation))
        else:
            change_info = {
                "decision": result.code or result.status,
                "worker_isolation": dict(worker_isolation),
            }
        result = ProductResult(
            status=result.status,
            code=result.code,
            message=result.message,
            target=result.target,
            pipeline=result.pipeline,
            approval_request=result.approval_request,
            warnings=result.warnings,
            change_result=change_info,
        )
        actor = get_git_user_name(target) or os.environ.get("USER", "unknown")
        timestamp = datetime.now(timezone.utc).isoformat()
        _persist_execution_report(target, result, actor, timestamp,
                                  report_persister=report_persister)
        return result

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
        # 2. Select and configure the worker to operate only in the worktree.
        selected_worker, selection_error = select_change_worker(
            worker_name, choice, model, adapter, worktree_dir, public_api=public_api
        )
        if selection_error:
            code, message = selection_error
            if is_auto_task and task_id:
                _finalize_candidate_task(target, task_id, status="CANCELLED", reason_code=code)
            return ProductResult(
                status="REFUSED",
                code=code,
                message=message,
                target=str(target),
                warnings=tuple(warnings),
            )

        isolation = None
        if not allow_unconfined_worker and type(selected_worker) not in (NativeCLIWorker, OpenCodeCLIWorker):
            if is_auto_task and task_id:
                _finalize_candidate_task(target, task_id, status="CANCELLED",
                                         reason_code=WORKER_SANDBOX_UNAVAILABLE)
            return _worker_isolation_refusal(
                target, "this in-process adapter has no supported contained subprocess transport; "
                        "use a built-in CLI worker or explicitly --allow-unconfined-worker",
                warnings, report_persister=report_persister, task_id=task_id)
        if type(selected_worker) in (NativeCLIWorker, OpenCodeCLIWorker):
            isolation = None if allow_unconfined_worker else WorkerIsolation((str(target.resolve()),))
            selected_worker.isolation = isolation

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
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code="WORKER_UNAVAILABLE",
                message=("The selected worker could not be started: "
                         f"{type(exc).__name__}: {exc}"),
                target=str(target),
                change_result={
                    "decision": "WORKER_UNAVAILABLE",
                    "task_id": task_id or "",
                    "execution_id": execution_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                warnings=tuple(warnings),
            ))

        if isolation is not None and isolation.error is not None:
            if is_auto_task and task_id:
                _finalize_candidate_task(target, task_id, status="CANCELLED",
                                         reason_code=WORKER_SANDBOX_UNAVAILABLE)
            return _worker_isolation_refusal(target, isolation.error.detail, warnings,
                                              pipeline_result, report_persister, task_id)

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
            return finish_worker_result(res)

        # 3. Worktree diff
        try:
            raw_diff = compute_worktree_diff(worktree, base_commit)
        except WorktreeError as exc:
            if is_auto_task and task_id:
                _finalize_candidate_task(target, task_id, status="CANCELLED", reason_code=exc.code)
            return finish_worker_result(ProductResult(
                status="REFUSED", code=exc.code, message=str(exc),
                target=str(target), pipeline=pipeline_result,
                warnings=tuple(warnings)))
        if not raw_diff.strip():
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="NO_CHANGES_PRODUCED"
                )
            return finish_worker_result(ProductResult(
                status="COMPLETED",
                code="NO_CHANGES_PRODUCED",
                message="The worker produced no change.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            ))

        # 4. Gate evaluation
        try:
            file_patches = parse_unified_diff(raw_diff)
        except Exception as exc:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="DIFF_SYNTAX_INVALID"
                )
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code="DIFF_SYNTAX_INVALID",
                message=f"Diff syntax parse error: {exc}",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            ))

        reason = unsupported_diff_reason(raw_diff, len(file_patches))
        if reason:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="DIFF_UNSUPPORTED"
                )
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code="DIFF_UNSUPPORTED",
                message=f"The change cannot be applied as shown ({reason}); nothing was applied.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            ))

        if not file_patches:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="NO_CHANGES_PRODUCED"
                )
            return finish_worker_result(ProductResult(
                status="COMPLETED",
                code="NO_CHANGES_PRODUCED",
                message="The worker produced no change.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            ))

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
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code=code,
                message=f"Change proposal failed security gate ({code}): {details}",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            ))

        # A change set applies all of its files or none of them; its size is bounded.
        if len(target_files) > MAX_FILES:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="CHANGESET_TOO_LARGE"
                )
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code="CHANGESET_TOO_LARGE",
                message=(
                    f"The change touches {len(target_files)} files; at most {MAX_FILES} "
                    f"can be applied together."
                ),
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            ))

        reason = next((r for fp in file_patches
                       if (r := apply_preflight_reason(target, worktree, fp))), "")
        if reason:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="PATCH_NOT_APPLICABLE"
                )
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code="PATCH_NOT_APPLICABLE",
                message=f"Not asking for approval: {reason}. Nothing was applied.",
                target=str(target),
                pipeline=pipeline_result,
                warnings=tuple(warnings),
            ))

        # 5. Single human confirmation
        is_interactive = confirm is not None or sys.stdin.isatty()
        if not is_interactive:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="APPROVAL_REQUIRED"
                )
            change_info = {
                "worker_isolation": worker_isolation,
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "AWAITING_APPROVAL",
                "apply_result": None,
            }
            return finish_worker_result(ProductResult(
                status="AWAITING_APPROVAL",
                code="APPROVAL_REQUIRED",
                message=("Applying a change needs an interactive terminal. Nothing was "
                         "applied and the proposal was not kept; re-run the command "
                         "in a terminal to review and approve it."),
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            ))

        # 5b. Run isolated tests on the project copy before asking for approval.
        test_info: dict[str, Any]
        test_duration = 0.0
        if change_cfg is not None:
            def cancel_test_candidate(reason_code: str) -> None:
                if is_auto_task and task_id:
                    _finalize_candidate_task(
                        target, task_id, status="CANCELLED", reason_code=reason_code
                    )

            test_info, test_duration, test_failure = run_configured_tests(
                target,
                change_cfg,
                file_patches,
                cancel_candidate=cancel_test_candidate,
                public_api=public_api,
            )
            if test_failure is not None:
                code, message = test_failure
                if test_info is not None:
                    change_info = {
                        "worker_isolation": worker_isolation,
                        "proposal": proposal,
                        "binding": proposal.binding(),
                        "diff_digest": proposal.diff_digest,
                        "gate_result": "ALLOWED",
                        "decision": "REFUSED",
                        "apply_result": None,
                        "tests": test_info,
                    }
                    return finish_worker_result(ProductResult(
                        status="REFUSED",
                        code=code,
                        message=message,
                        target=str(target),
                        pipeline=pipeline_result,
                        change_result=change_info,
                        warnings=tuple(warnings),
                    ))
                return finish_worker_result(ProductResult(
                    status="REFUSED",
                    code=code,
                    message=message,
                    target=str(target),
                    pipeline=pipeline_result,
                    warnings=tuple(warnings),
                ))
        else:
            no_cfg_warn = ("Tests: NOT RUN (no .ai/change.yaml; "
                           "untested change allowed by --allow-untested)")
            warnings.append(no_cfg_warn)
            test_info = {
                "status": "NOT_CONFIGURED",
                "command": [],
                "duration_s": 0.0,
                "timed_out": False,
                "truncated": False,
                "untested_override": True,
            }

        if change_cfg is not None:
            print(f"Tests: PASSED ({test_duration:.2f} s)")
        else:
            print(no_cfg_warn)

        approved = request_change_approval(
            confirm, target_files, proposal, public_api=public_api
        )

        if not approved:
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code="APPROVAL_REJECTED"
                )
            change_info = {
                "worker_isolation": worker_isolation,
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "REJECTED",
                "apply_result": None,
                "tests": test_info,
            }
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code="APPROVAL_REJECTED",
                message="Change application rejected by user.",
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            ))

        actor, now_iso, target_changed, apply_res = authorize_and_apply(
            target,
            proposal,
            base_commit,
            cfg_digest,
            journal,
            target_files,
            worktree_dir,
            pre_apply_hook=pre_apply_hook,
            custom_verifier=custom_verifier,
            public_api=public_api,
        )
        if target_changed:
            code = "TARGET_CHANGED"
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code=code, actor=actor, timestamp=now_iso
                )
            change_info = {
                "worker_isolation": worker_isolation,
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "APPROVED",
                "apply_result": None,
                "tests": test_info,
            }
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code=code,
                message="Target project changed after tests/approval; nothing was applied.",
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            ))

        # 6. Apply every file of the change set, or none of them
        if not apply_res.applied:
            code = apply_res.reason_codes[0] if apply_res.reason_codes else "PATCH_APPLY_FAILED"
            if is_auto_task and task_id:
                _finalize_candidate_task(
                    target, task_id, status="CANCELLED", reason_code=code, actor=actor, timestamp=now_iso
                )
            change_info = {
                "worker_isolation": worker_isolation,
                "proposal": proposal,
                "binding": proposal.binding(),
                "diff_digest": proposal.diff_digest,
                "gate_result": "ALLOWED",
                "decision": "APPROVED",
                "apply_result": apply_res.status,
                "tests": test_info,
            }
            return finish_worker_result(ProductResult(
                status="REFUSED",
                code=code,
                message=f"Change application failed ({code}): {', '.join(apply_res.reason_codes)}",
                target=str(target),
                pipeline=pipeline_result,
                change_result=change_info,
                warnings=tuple(warnings),
            ))

        return persist_approved_change(
            target,
            instruction,
            task_id,
            is_auto_task,
            actor,
            now_iso,
            proposal,
            apply_res,
            test_info,
            worker_isolation,
            pipeline_result,
            warnings,
            change_cfg,
            allow_unconfined_worker,
            report_persister,
            public_api=public_api,
        )

    finally:
        # 8. Clean up worktree unconditionally
        cleanup_worktree(target, worktree)
        # Safety net: whichever path ended the run (a return without its own
        # closing, an exception, Ctrl-C), a candidate Task never stays active.
        if is_auto_task and task_id and _candidate_task_is_active(target, task_id):
            _finalize_candidate_task(target, task_id, status="CANCELLED",
                                     reason_code="RUN_ABORTED")
