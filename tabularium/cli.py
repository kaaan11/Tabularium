"""Canonical ``tabu`` product command-line interface."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .execution import (ProductResult, continue_execution, make_resolution,
                        run_instruction)
from .explain import render_human as render_explain_human
from . import explain
from .init import InitError, InitResult, initialize, render_human, render_json
from . import task as task_surface
from .diagnostics import HealthResult, doctor_project, render_human as render_diagnostic_human, render_json as render_diagnostic_json, status_project
from .onboarding import (
    OnboardingResult,
    build_authorizations,
    onboard_project,
    prompt_declarations,
    render_human as render_onboarding_human,
    render_json as render_onboarding_json,
    render_proposal_preview,
)
from .version import get_version
from .terminal import sanitize_terminal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tabu",
        description="Tabularium\nGoverned context and execution for AI-assisted project work.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    init_parser = commands.add_parser(
        "init",
        help="Initialize Tabularium in a project",
        description="Initialize Tabularium in a project.",
    )
    init_parser.add_argument("path", nargs="?", help="project directory (default: current directory)")
    init_parser.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable result object",
    )
    onboard_parser = commands.add_parser(
        "onboard",
        help="Review and record project identity",
        description="Discover bounded project signals, then request explicit human onboarding declarations.",
    )
    onboard_parser.add_argument("path", nargs="?", help="project directory (default: current directory)")
    onboard_parser.add_argument(
        "--json",
        action="store_true",
        help="emit a read-only machine-readable proposal; never prompt or write",
    )
    for name, help_text, description in (
        ("status", "Show current Tabularium/project state", "Show a concise, read-only project state summary."),
        ("doctor", "Diagnose Tabularium/project health", "Run deterministic, read-only health checks."),
    ):
        diagnostic_parser = commands.add_parser(name, help=help_text, description=description)
        diagnostic_parser.add_argument("path", nargs="?", help="project directory (default: current directory)")
        diagnostic_parser.add_argument("--json", action="store_true", help="emit one machine-readable result object")
    run_parser = commands.add_parser(
        "run", help="Run one instruction through governed Tabularium execution",
        description="Run one instruction through the existing governed execution path.",
    )
    run_parser.add_argument("instruction", help="instruction to send to the worker")
    run_parser.add_argument("path", nargs="?", help="project directory (default: current directory)")
    run_parser.add_argument("--task", help="explicit active Task id when the project has more than one")
    run_parser.add_argument("--worker", help="worker path: native, opencode, or api (default: native/opencode when available)")
    run_parser.add_argument("--model", help="resolved model, used with "
                            "--worker api or opencode (or set TABU_MODEL); not used by "
                            "the native worker")
    run_parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                            help="preflight only: report what this run would "
                                 "do without invoking a worker or writing "
                                 "anything")
    run_parser.add_argument("--json", dest="dry_run_json",
                            action="store_true",
                            help="machine-readable result; valid ONLY "
                                 "together with --dry-run (no JSON contract "
                                 "exists for a real run)")
    run_parser.add_argument("--persist", action="store_true",
                            help="propose and record an execution Report in .ai/reports upon completion")
    run_parser.add_argument("--change", action="store_true", default=False,
                            help="allow worker to modify codebase via a governed worktree diff, security gate, and single human confirmation")
    run_parser.add_argument("--workflow", default=None,
                            help="execution orchestration: single (default) or "
                                 "dag (governed multi-agent, DEC-2026-0010); "
                                 "unknown values are refused")
    explain_parser = commands.add_parser(
        "explain", help="Explain what a run would do, without running it",
        description="Evaluate every pre-runtime gate of governed execution and "
                    "report the outcome; never invokes a worker and never "
                    "writes anything.")
    explain_parser.add_argument("instruction",
                                help="instruction that would be sent to the worker")
    explain_parser.add_argument("path", nargs="?",
                                help="project directory (default: current directory)")
    explain_parser.add_argument("--task",
                                help="explicit active Task id when the project "
                                     "has more than one")
    explain_parser.add_argument("--worker",
                                help="worker path: native, opencode, or api (default: "
                                     "native/opencode when available)")
    explain_parser.add_argument("--model",
                                help="resolved API model, used with --worker "
                                     "api (or set TABU_MODEL); not used by the "
                                     "native worker")
    explain_parser.add_argument("--json", action="store_true",
                                help="emit one machine-readable result object")
    continue_parser = commands.add_parser(
        "continue", help="Continue pending Tabularium work",
        description="Continue the most recent pending governed execution.",
    )
    continue_parser.add_argument("path", nargs="?", help="project directory (default: current directory)")
    continue_parser.add_argument("--persist", action="store_true",
                                 help="propose and record an execution Report in .ai/reports upon completion")
    tui_parser = commands.add_parser(
        "tui", help="Open the terminal dashboard (reads, approvals, runs)",
        description="Render status, Task records, and an optional preflight. "
                    "Approval sheets (task approve, continue, persist, onboard) "
                    "require typed confirmation and call the same functions as "
                    "the CLI. R starts one governed run in a background thread "
                    "with a live event view; stop detaches, never kills. "
                    "Never auto-approves.",
    )
    tui_parser.add_argument("path", nargs="?", help="project directory (default: current directory)")
    tui_parser.add_argument("--instruction",
                            help="optional instruction to preflight in the dashboard")
    task_parser = commands.add_parser(
        "task", help="Inspect stored Task records",
        description="Read-only inspection of the project's Task records "
                    "through the production loader.")
    task_commands = task_parser.add_subparsers(dest="task_command",
                                              metavar="TASK-COMMAND")
    for name, help_text, description in (
        ("list", "List stored Task records",
         "List a bounded summary of every discovered Task record."),
        ("show", "Show one Task record",
         "Show one Task record exactly as the production loader reads it."),
        ("validate", "Validate Task records",
         "Validate one or all Task records against the run-consumption "
         "contract. The first positional argument is a Task id when it has "
         "the canonical form TASK-YYYY-NNNN, otherwise the project "
         "directory."),
        ("create", "Create one canonical Task record",
         "Allocate an id, validate the canonical Task against the frozen "
         "contract, and write it atomically without ever overwriting."),
        ("start", "Start work on a Task",
         "Advance a Task to IN_PROGRESS state."),
        ("approve", "Approve a Task",
         "Record explicit human approval for a Task."),
        ("complete", "Complete a Task",
         "Mark an active Task as COMPLETED."),
        ("transition", "Transition Task lifecycle state",
         "Transition a Task to a target lifecycle state."),
    ):
        sub_parser = task_commands.add_parser(name, help=help_text,
                                             description=description)
        if name in ("show", "start", "complete"):
            sub_parser.add_argument("task_id", help="Task id (TASK-YYYY-NNNN)")
        if name == "approve":
            sub_parser.add_argument("task_id", help="Task id (TASK-YYYY-NNNN)")
            sub_parser.add_argument("--by", default="USER",
                                    help="approval authority name (default: USER)")
        if name == "transition":
            sub_parser.add_argument("task_id", help="Task id (TASK-YYYY-NNNN)")
            sub_parser.add_argument("status",
                                    help="target status (e.g. IN_PROGRESS, REVIEW, COMPLETED, ARCHIVED)")
            sub_parser.add_argument("--force", action="store_true",
                                    help="allow jumping non-adjacent lifecycle states")
        if name == "validate":
            sub_parser.add_argument("task_id", nargs="?",
                                    help="validate one Task id instead of all")
        if name == "create":
            sub_parser.add_argument("--title", required=True,
                                    help="Task title")
            sub_parser.add_argument("--objective", required=True,
                                    help="Task objective")
            sub_parser.add_argument("--type", dest="type_", default=None,
                                    help="kind of work (frozen v1.10 enum)")
            sub_parser.add_argument("--priority", default=None,
                                    help="importance/urgency (frozen enum)")
            sub_parser.add_argument("--risk", default=None,
                                    help="potential impact (frozen enum)")
            sub_parser.add_argument("--uncertainty-level",
                                    dest="uncertainty_level", default=None,
                                    help="how little is understood "
                                         "(frozen enum)")
            sub_parser.add_argument("--plan", default=None,
                                    help="planned approach text")
            sub_parser.add_argument("--print", dest="print_only",
                                    action="store_true",
                                    help="print the would-be canonical Task; "
                                         "validates but writes nothing and "
                                         "reserves no id")
        sub_parser.add_argument("path", nargs="?",
                                help="project directory (default: current directory)")
        sub_parser.add_argument("--json", action="store_true",
                                help="emit one machine-readable result object")
    return parser


def _emit(result: InitResult, as_json: bool) -> None:
    output = render_json(result) if as_json else render_human(result)
    stream = sys.stdout if result.status in {"INITIALIZED", "ALREADY_INITIALIZED"} else sys.stderr
    print(output, file=stream)


def _emit_onboarding(result: OnboardingResult, as_json: bool) -> None:
    output = render_onboarding_json(result) if as_json else render_onboarding_human(result)
    stream = sys.stdout if result.status in {"ONBOARDED", "ALREADY_ONBOARDED", "PROPOSAL_READY"} else sys.stderr
    print(output, file=stream)


def _git_user_name(target: str | Path | None) -> str:
    try:
        proc = subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(target or "."),
            capture_output=True,
            check=False,
        )
        name = proc.stdout.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    # Read from the target repository's config, which may be untrusted: only a
    # short printable name becomes a default (it is shown and recorded).
    if not name or len(name) > 100 or not name.isprintable():
        return ""
    return name


def _run_onboarding(args: argparse.Namespace) -> int:
    try:
        initial = onboard_project(args.path)
    except InitError as exc:
        failed = OnboardingResult(
            "FAILED", str(args.path or "."), code=exc.code, message=exc.message
        )
        _emit_onboarding(failed, args.json)
        return 2 if exc.code in {"TARGET_NOT_FOUND", "TARGET_NOT_DIRECTORY", "PERMISSION_DENIED"} else 1

    if args.json or initial.status != "PROPOSAL_READY":
        _emit_onboarding(initial, args.json)
        return 0 if initial.status in {"ALREADY_ONBOARDED", "PROPOSAL_READY"} else 1

    _emit_onboarding(initial, False)
    declarations = prompt_declarations()
    if declarations is None:
        print("Project onboarding cancelled; no files were changed.", file=sys.stderr)
        return 1

    draft = onboard_project(
        args.path,
        declarations={
            "title": declarations.title,
            "purpose": declarations.purpose,
            "scope": declarations.scope,
            "constraints": declarations.constraints,
            "test_command": declarations.test_command,
        },
    )
    if draft.proposal is None:
        _emit_onboarding(draft, args.json)
        return 1

    if args.json:
        _emit_onboarding(draft, True)
    else:
        print()
        print(sanitize_terminal(render_proposal_preview(draft.proposal)))

    try:
        answer = input("Continue to authorization details? [y/N]: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in {"y", "yes"}:
        print("Authorization not provided; no files were changed.", file=sys.stderr)
        return 1

    git_name = _git_user_name(args.path)
    name_prompt = (
        f"Authorization name (Enter for {git_name}): "
        if git_name
        else "Authorization name (required): "
    )
    time_prompt = "Authorization timestamp with UTC offset (Enter for now): "
    try:
        raw_name = input(name_prompt).strip()
        raw_time = input(time_prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("Authorization not provided; no files were changed.", file=sys.stderr)
        return 1

    authorized_by = raw_name if raw_name else git_name
    if not authorized_by:
        print("Authorization not provided; no files were changed.", file=sys.stderr)
        return 1

    if not raw_time:
        authorized_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        authorized_at = raw_time

    result = onboard_project(
        args.path,
        declarations={
            "title": declarations.title,
            "purpose": declarations.purpose,
            "scope": declarations.scope,
            "constraints": declarations.constraints,
            "test_command": declarations.test_command,
        },
        authorizations=build_authorizations(draft.proposal, authorized_by, authorized_at),
    )
    _emit_onboarding(result, False)
    return 0 if result.status in {"ONBOARDED", "ALREADY_ONBOARDED"} else 1


def _emit_diagnostic(result: HealthResult, as_json: bool) -> None:
    output = render_diagnostic_json(result) if as_json else render_diagnostic_human(result)
    stream = sys.stdout if as_json or result.exit_code == 0 else sys.stderr
    print(output, file=stream)


def _run_diagnostic(args: argparse.Namespace) -> int:
    result = status_project(args.path) if args.command == "status" else doctor_project(args.path)
    _emit_diagnostic(result, args.json)
    return result.exit_code


def _emit_task(as_json: bool, render_human_fn, render_json_fn, result) -> int:
    output = render_json_fn(result) if as_json else render_human_fn(result)
    stream = sys.stdout if as_json or result.exit_code == 0 else sys.stderr
    print(output, file=stream)
    return result.exit_code


def _split_validate_positionals(task_id, path):
    """Deterministic positional disambiguation for `task validate`.

    A lone positional is a Task id exactly when it has the frozen canonical
    form; any other value is the project directory. Both positionals given
    are taken in order. Documented in the subcommand help.
    """
    if task_id is not None and path is None and \
            not task_surface._is_canonical_task_id(str(task_id).strip()):
        return None, str(task_id)
    return task_id, path


def _run_task(args: argparse.Namespace) -> int:
    if args.task_command == "list":
        return _emit_task(args.json, task_surface.render_human,
                          task_surface.render_json, task_surface.task_list(args.path))
    if args.task_command == "show":
        return _emit_task(args.json, task_surface.render_human_show,
                          task_surface.render_json_show,
                          task_surface.task_show(args.path, args.task_id))
    if args.task_command == "validate":
        validate_id, validate_path = _split_validate_positionals(
            getattr(args, "task_id", None), args.path)
        return _emit_task(args.json, task_surface.render_human_validate,
                          task_surface.render_json_validate,
                          task_surface.task_validate(validate_path, validate_id))
    if args.task_command == "create":
        return _emit_task(args.json, task_surface.render_human_create,
                          task_surface.render_json_create,
                          task_surface.task_create(
                              args.path,
                              title=args.title,
                              objective=args.objective,
                              type_=getattr(args, "type_", None),
                              priority=args.priority,
                              risk=args.risk,
                              uncertainty_level=getattr(args, "uncertainty_level",
                                                        None),
                              plan=args.plan,
                              print_only=args.print_only))
    if args.task_command == "start":
        return _emit_task(args.json, task_surface.render_human_transition,
                          task_surface.render_json_transition,
                          task_surface.task_start(args.path, args.task_id))
    if args.task_command == "approve":
        return _emit_task(args.json, task_surface.render_human_transition,
                          task_surface.render_json_transition,
                          task_surface.task_approve(args.path, args.task_id,
                                                   by=getattr(args, "by", "USER")))
    if args.task_command == "complete":
        return _emit_task(args.json, task_surface.render_human_transition,
                          task_surface.render_json_transition,
                          task_surface.task_complete(args.path, args.task_id))
    if args.task_command == "transition":
        return _emit_task(args.json, task_surface.render_human_transition,
                          task_surface.render_json_transition,
                          task_surface.task_transition(args.path, args.task_id, args.status,
                                                      force=getattr(args, "force", False)))
    raise SystemExit("tabu task requires a subcommand (list, show, validate, create, start, approve, complete, transition)")


def _emit_warnings(result: ProductResult) -> None:
    """Operator-facing notices belong on stderr, after stdout is flushed."""
    for warning in result.warnings:
        print(sanitize_terminal(warning), file=sys.stderr)


def _emit_execution(result: ProductResult) -> None:
    print("Tabularium")
    print(f"Project: {result.target}")
    pipeline = result.pipeline
    if pipeline is not None:
        worker = (pipeline.worker_result or {}).get("worker_id")
        if worker:
            print(sanitize_terminal(f"Worker: {worker}"))
        metrics = pipeline.metrics
        if metrics is not None:
            governed = metrics.governed_context.get("estimated_tokens")
            payload = metrics.payload_size.get("estimated_tokens")
            reduction = metrics.reduction_pct
            if governed is not None:
                print(f"Context: {governed:,} governed estimated tokens")
            if payload is not None:
                print(f"Worker payload: {payload:,} estimated tokens")
            if reduction is not None:
                print(f"Reduction: {reduction}%")
    print(f"Status: {result.status.lower()}")
    if result.status == "COMPLETED" and pipeline is not None:
        if pipeline.output:
            print()
            print(sanitize_terminal(pipeline.output))
        # Warnings ride stderr on every terminal path, including success.
        sys.stdout.flush()
        _emit_warnings(result)
        return
    if result.approval_request is not None:
        request = result.approval_request
        if isinstance(request, dict):
            action = request.get("action", "continue past the approval boundary")
            scope = (request.get("detail") or {}).get("scope") or {}
        else:
            action = getattr(request, "action", "continue past the approval boundary")
            scope = getattr(request, "detail", {}).get("scope", {})
        print(sanitize_terminal(f"Approval required: {action}"))
        if scope:
            print(sanitize_terminal(f"Requested scope: {scope}"))
    # Stream discipline: stdout above was emitted first; flush it before the
    # stderr switch so merged redirections (2>&1, pipes) keep the program's
    # semantic order. Ownership stays unchanged — failures still belong on
    # stderr.
    sys.stdout.flush()
    _emit_warnings(result)
    if result.code:
        print(f"Reason: {result.code}", file=sys.stderr)
    print(sanitize_terminal(result.message), file=sys.stderr)


def _approval_prompt(result: ProductResult) -> ProductResult:
    request = result.approval_request
    print("Approval is required for the requested action.")
    try:
        decision = input("Decision [approve/reject]: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        decision = ""
    if decision not in {"approve", "a", "reject", "r"}:
        return ProductResult("AWAITING_APPROVAL", "APPROVAL_REQUIRED",
                             "Approval was not recorded. Run `tabu continue` "
                             "when ready.", result.target,
                             result.pipeline, request)
    try:
        actor = input("Decided by (required): ").strip()
        decided_at = input("Decision time with UTC offset (required): ").strip()
    except (EOFError, KeyboardInterrupt):
        actor = decided_at = ""
    if not actor or not decided_at:
        return ProductResult("AWAITING_APPROVAL", "APPROVAL_REQUIRED",
                             "Approval was not recorded: actor and timestamp "
                             "are required.", result.target,
                             result.pipeline, request)
    resolution = make_resolution(
        request, "APPROVE" if decision in {"approve", "a"} else "REJECT",
        actor, decided_at)
    return continue_execution(result.target, resolution=resolution)


def _handle_persistence(target_path: str | Path | None, result: ProductResult) -> None:
    from datetime import datetime, timezone
    from tabularium.execution import build_execution_report_proposal, apply_execution_report_proposal

    proposal_item = build_execution_report_proposal(target_path or ".", result)
    if proposal_item is None or not getattr(proposal_item, "ok", False):
        return
    proposal = proposal_item.proposal
    print()
    print(f"Report proposal ready: {proposal.record_id} -> {proposal.target_path}")
    print(f"Content SHA-256 digest: {proposal.content_digest}")
    try:
        ans = input("Authorize persistence to project truth (.ai/reports)? [y/N]: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        ans = "n"
    if ans not in {"y", "yes"}:
        print("Persistence not authorized; no files were written.", file=sys.stderr)
        return
    try:
        actor = input("Authorized by (required, e.g. human:user): ").strip()
        timestamp = input("Authorization timestamp with UTC offset (or Enter for now): ").strip()
    except (EOFError, KeyboardInterrupt):
        actor = timestamp = ""
    if not actor:
        print("Authorized actor is required; no files were written.", file=sys.stderr)
        return
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    persist_res = apply_execution_report_proposal(
        target_path or ".", proposal, authorized_by=actor, authorized_at=timestamp
    )
    if getattr(persist_res, "written", False):
        print(f"Successfully persisted report {proposal.record_id} to {proposal.target_path}")
    else:
        print(f"Persistence denied: {', '.join(getattr(persist_res, 'reason_codes', ()))}", file=sys.stderr)


def _run_execution(args: argparse.Namespace) -> int:
    if getattr(args, "dry_run", False):
        if getattr(args, "dry_run_json", False):
            # Decision C: no public run JSON. The only machine-readable run
            # surface is the dry-run preflight itself.
            return _emit_explain(explain.preflight(
                args.instruction, args.path, task_id=args.task,
                model=args.model, worker=args.worker,
                workflow=getattr(args, "workflow", None),
                command="dry-run"),
                as_json=True)
        return _emit_explain(explain.preflight(
            args.instruction, args.path, task_id=args.task,
            model=args.model, worker=args.worker,
            workflow=getattr(args, "workflow", None),
            command="dry-run"),
            as_json=False)
    from tabularium.engine.workers.cli.transport import worker_env_passthrough
    passed, refused = worker_env_passthrough()
    if passed:
        print(f"Worker environment (TABU_WORKER_ENV): {', '.join(passed)}", file=sys.stderr)
    for entry in refused:
        print(f"Warning: TABU_WORKER_ENV ignored {entry}", file=sys.stderr)
    result = run_instruction(args.instruction, args.path, task_id=args.task,
                             model=args.model, worker=args.worker,
                             workflow=getattr(args, "workflow", None),
                             change=getattr(args, "change", False))
    # A --change result carries its own single decision and report; the
    # runtime approval sheet and --persist prompt do not apply to it.
    is_change = getattr(result, "change_result", None) is not None
    if result.status == "AWAITING_APPROVAL" and not is_change:
        _emit_execution(result)
        result = _approval_prompt(result)
    _emit_execution(result)
    if result.status == "COMPLETED" and getattr(args, "persist", False) and not is_change:
        _handle_persistence(args.path, result)
    return 0 if result.status == "COMPLETED" else 1


def _run_continue(args: argparse.Namespace) -> int:
    result = continue_execution(args.path)
    if result.status == "AWAITING_APPROVAL":
        _emit_execution(result)
        result = _approval_prompt(result)
    _emit_execution(result)
    if result.status == "COMPLETED" and getattr(args, "persist", False):
        _handle_persistence(args.path, result)
    return 0 if result.status == "COMPLETED" else 1


def _run_tui(args: argparse.Namespace) -> int:
    from tabularium.engine.tui.app import run as run_dashboard

    return run_dashboard(args.path, getattr(args, "instruction", None))


def _emit_explain(result: explain.ExplainResult, as_json: bool) -> int:
    output = (explain.render_json(result) if as_json
              else explain.render_human(result))
    stream = sys.stdout if as_json or result.exit_code == 0 else sys.stderr
    for warning in result.warnings:
        print(sanitize_terminal(warning), file=sys.stderr)
    print(output, file=stream)
    return result.exit_code


def _run_explain(args: argparse.Namespace) -> int:
    return _emit_explain(explain.preflight(
        args.instruction, args.path, task_id=args.task, model=args.model,
        worker=args.worker, command="explain"), as_json=args.json)


def _parse_args(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
    """Accept the project path after options on every supported Python.

    Before 3.12.7 argparse leaves `tabu explain "x" --task T PATH` with PATH
    unrecognized: the optional `path` positional was already consumed (empty)
    before `--task`. Ubuntu 24.04 ships 3.12.3, so a single trailing word is
    assigned to an unset `path` here; anything else is still an error.
    """
    args, extras = parser.parse_known_args(argv)
    if (len(extras) == 1 and not extras[0].startswith("-")
            and getattr(args, "path", "") is None):
        args.path = extras[0]
    elif extras:
        parser.error(f"unrecognized arguments: {' '.join(extras)}")
    return args


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = _parse_args(parser, argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "onboard":
        return _run_onboarding(args)
    if args.command in {"status", "doctor"}:
        return _run_diagnostic(args)
    if args.command == "task":
        if not getattr(args, "task_command", None):
            parser.error("tabu task requires a subcommand "
                         "(list, show, validate, create)")
        return _run_task(args)
    if args.command == "run":
        if getattr(args, "dry_run_json", False) and \
                not getattr(args, "dry_run", False):
            parser.error("--json under run requires --dry-run")
        return _run_execution(args)
    if args.command == "explain":
        return _run_explain(args)
    if args.command == "continue":
        return _run_continue(args)
    if args.command == "tui":
        return _run_tui(args)
    if args.command != "init":
        parser.error(f"unknown command: {args.command}")

    try:
        result = initialize(args.path)
    except InitError as exc:
        result = InitResult("FAILED", str(args.path or "."), code=exc.code, message=exc.message)
        _emit(result, args.json)
        return 2 if exc.code in {"TARGET_NOT_FOUND", "TARGET_NOT_DIRECTORY", "PERMISSION_DENIED"} else 1
    _emit(result, args.json)
    return 0 if result.status in {"INITIALIZED", "ALREADY_INITIALIZED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
