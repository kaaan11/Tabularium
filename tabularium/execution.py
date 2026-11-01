"""Thin product entrypoints for the existing governed execution path."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tabularium.engine.context_loader.confinement import ProjectScope
from tabularium.engine.context_loader.discovery import TERMINAL_TASK_STATUSES, discover
from tabularium.engine.execution import (ExecutionPlan, PipelineResult,
                               inspect_execution, resume_execution,
                               start_execution)
from tabularium.engine.runtime import HumanApprovalResolution
from tabularium.engine.workers.api import APIWorker, ModelTarget
from tabularium.engine.workers.cli import (NativeCLIWorker, OpenCodeCLIWorker,
                                 find_native_executable,
                                 find_opencode_executable)

from .diagnostics import (DEGRADED, NOT_INITIALIZED, NOT_ONBOARDED,
                          INVALID_PROJECT, status_project)
from .storage import (CHECKPOINT_DIR_UNSAFE, CheckpointDirUnsafe,
                      prepare_checkpoint_storage,
                      verify_checkpoint_storage_if_exists,
                      write_secure_file)


NO_CONTINUATION = "NO_CONTINUATION"
WORKER_CONFIGURATION_MISSING = "WORKER_CONFIGURATION_MISSING"
WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
WORKER_SELECTION_REQUIRED = "WORKER_SELECTION_REQUIRED"
WORKER_SELECTION_INVALID = "WORKER_SELECTION_INVALID"
API_CREDENTIAL_REQUIRED = "API_CREDENTIAL_REQUIRED"
TASK_SELECTION_REQUIRED = "TASK_SELECTION_REQUIRED"
TASK_REQUIRED = "TASK_REQUIRED"
PROJECT_PATH_INVALID = "PROJECT_PATH_INVALID"
WORKFLOW_SELECTION_INVALID = "WORKFLOW_SELECTION_INVALID"

WORKFLOWS = ("single", "dag")

# Human decision (Phase F): an explicitly supplied model combined with the
# Native worker is WARNED about, never silently ignored and never rejected.
# Execution proceeds unchanged; the model simply does not reach a native
# backend, and the operator is told why.
MODEL_IGNORED_BY_NATIVE_WARNING = (
    "Warning: --model applies to API/model execution only; the selected "
    "native worker does not use it.")


@dataclass(frozen=True)
class ProductResult:
    """User-facing projection of a pipeline attempt; no new runtime state."""

    status: str
    code: str
    message: str
    target: str
    pipeline: PipelineResult | None = None
    approval_request: Any = None
    # Non-fatal operator-facing notices (e.g. an ignored --model). Human
    # output only; the machine-readable contract of every command is unchanged.
    warnings: tuple[str, ...] = ()
    change_result: Any = None


def run_instruction(instruction: str, path: str | Path | None = None,
                    *, task_id: str | None = None,
                    model: str | None = None,
                    worker: str | None = None,
                    adapter: Any = None,
                    collector: Any = None,
                    workflow: str | None = None,
                    change: bool = False,
                    confirm: Callable[[str], bool] | None = None,
                    custom_verifier: Any = None,
                    pre_apply_hook: Any = None) -> ProductResult:
    """Run one instruction through M2 → M3 → M4 using one selected worker.

    The optional adapter seam exists for deterministic product tests. Worker
    selection is product policy; the M4 pipeline still receives one explicit
    adapter and performs no routing or fallback.
    """
    target, error = _target(path)
    if error:
        return error
    if not isinstance(instruction, str) or not instruction.strip():
        return _refusal(target, "INSTRUCTION_REQUIRED",
                        "An instruction is required.")

    workflow_value = (workflow if workflow is not None else "single")
    if not isinstance(workflow_value, str):
        workflow_value = ""
    workflow_value = workflow_value.strip().lower()
    if workflow_value not in WORKFLOWS:
        # Explicit opt-in only (DEC-2026-0010): an unknown orchestration is
        # refused before any gate runs, never guessed.
        return _refusal(target, WORKFLOW_SELECTION_INVALID,
                        f"Unknown workflow '{workflow}'; choose "
                        "'single' or 'dag'.")

    try:
        health = status_project(target)
    except (ImportError, OSError, RuntimeError, ValueError):
        return _refusal(target, INVALID_PROJECT,
                        "The project could not be inspected. Run `tabu doctor` "
                        "and correct the reported project issue.")
    gate = _health_gate(target, health)
    if gate:
        return gate

    if not change:
        chosen_task, task_error = _choose_task(target, task_id)
        if task_error:
            return task_error
    else:
        chosen_task = task_id

    selected_model = (model or os.environ.get("TABU_MODEL") or "").strip()
    execution_id = _execution_id(target)
    checkpoint = _checkpoint_path(target)
    try:
        prepare_checkpoint_storage(checkpoint)
    except CheckpointDirUnsafe as exc:
        return _refusal(target, CHECKPOINT_DIR_UNSAFE, str(exc))

    if change:
        from tabularium.change_path import run_change_path

        return run_change_path(
            target=target,
            instruction=instruction,
            task_id=chosen_task,
            execution_id=execution_id,
            checkpoint_path=str(checkpoint),
            worker_name=worker,
            model=selected_model,
            adapter=adapter,
            collector=collector,
            workflow=workflow_value,
            confirm=confirm,
            custom_verifier=custom_verifier,
            pre_apply_hook=pre_apply_hook,
        )

    selected, selection_error = _select_worker(
        target, worker, selected_model, adapter)
    if selection_error:
        return selection_error
    selected_worker = selected
    warnings: tuple[str, ...] = ()
    if selected_model and \
            getattr(selected_worker.descriptor, "category", "") == "CLI_CODING":
        warnings = (MODEL_IGNORED_BY_NATIVE_WARNING,)
    resolved_model = getattr(selected_worker, "model_target", None)
    if selected_model and resolved_model is None:
        resolved_model = ModelTarget(selected_model)
    plan = ExecutionPlan(
        project_root=str(target),
        task_id=chosen_task,
        intent=instruction.strip(),
        execution_id=execution_id,
        checkpoint_path=str(checkpoint),
        workflow=workflow_value,
        adapter=selected_worker,
        model_target=(resolved_model if getattr(selected_worker.descriptor, "category", "")
                      == "API_MODEL" else None),
    )
    try:
        # `collector` is optional observability (M5.1): None keeps the
        # long-standing default path byte-identical; a collector only
        # observes and can never change what the pipeline decides.
        result = start_execution(plan, collector)
    except ImportError as exc:
        return _refusal(target, WORKER_UNAVAILABLE,
                        "The selected worker/runtime is unavailable. "
                        "Install the existing Tabularium runtime extra and configure "
                        "the selected worker.",
                        detail=str(exc))
    except (OSError, RuntimeError, ValueError) as exc:
        return _refusal(target, WORKER_UNAVAILABLE,
                        "The selected worker could not be started. "
                        "Configure the selected worker and try again.",
                        detail=str(exc))
    if not result.executed:
        # Human decision (Phase I, D): a preparation refusal never entered
        # runtime, so it must not replace the locator of the last execution
        # that did — `continue` resolves work through that marker alone.
        return _from_pipeline(target, result, warnings=warnings)
    try:
        remember_execution(target, execution_id)
    except OSError:
        # The checkpoint remains the runtime's authority. A locator write
        # failure is surfaced as no continuation later, never as a project
        # mutation or a fabricated completion.
        pass
    return _from_pipeline(target, result, warnings=warnings)


def continue_execution(path: str | Path | None = None, *,
                       resolution: Any = None) -> ProductResult:
    """Resume the existing checkpointed approval boundary, if one exists."""
    target, error = _target(path)
    if error:
        return error
    try:
        health = status_project(target)
    except (ImportError, OSError, RuntimeError, ValueError):
        return _refusal(target, INVALID_PROJECT,
                        "The project could not be inspected. Run `tabu doctor` "
                        "and correct the reported project issue.")
    gate = _health_gate(target, health)
    if gate:
        return gate

    checkpoint = _checkpoint_path(target)
    try:
        verify_checkpoint_storage_if_exists(checkpoint)
    except CheckpointDirUnsafe as exc:
        return _refusal(target, CHECKPOINT_DIR_UNSAFE, str(exc))

    try:
        state = inspect_execution(_execution_id_from_checkpoint(checkpoint),
                                  str(checkpoint)) if checkpoint.exists() else None
    except (ImportError, OSError, RuntimeError, ValueError):
        state = None
    if state is None:
        return _refusal(target, NO_CONTINUATION,
                        "There is no valid Tabularium work to continue. Run "
                        '`tabu run "<instruction>"` first.')
    if getattr(state, "runtime_status", "") != "AWAITING_APPROVAL":
        return _refusal(target, NO_CONTINUATION,
                        "There is no pending Tabularium approval to continue. Run "
                        '`tabu run "<instruction>"` for new work.')
    if resolution is None:
        return ProductResult("AWAITING_APPROVAL", "APPROVAL_REQUIRED",
                             "Human approval is required before this work "
                             "can continue.", str(target),
                             approval_request=state.approval_request)
    try:
        prepare_checkpoint_storage(checkpoint)
    except CheckpointDirUnsafe as exc:
        return _refusal(target, CHECKPOINT_DIR_UNSAFE, str(exc))

    try:
        result = resume_execution(state.execution_id, str(checkpoint),
                                  resolution, task_id=state.task_id)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        return _refusal(target, WORKER_UNAVAILABLE,
                        "The existing execution state could not be resumed. "
                        "Run `tabu doctor` and try again.", detail=str(exc))
    return _from_pipeline(target, result)


def make_resolution(request: Any, decision: str, actor: str,
                    decided_at: str) -> HumanApprovalResolution:
    """Build the existing approval value from explicit human input."""
    return HumanApprovalResolution(
        approval_request_id=_request_value(request, "request_id"),
        execution_id=_request_value(request, "execution_id"),
        task_id=_request_value(request, "task_id"),
        decision=decision,
        decided_by=actor,
        decided_at=decided_at,
    )


def _target(raw: str | Path | None) -> tuple[Path, ProductResult | None]:
    try:
        target = Path(raw or ".").expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        target = Path(raw or ".")
    if not target.is_dir():
        return target, _refusal(target, PROJECT_PATH_INVALID,
                                "The project path is not an existing directory.")
    return target, None


def _health_gate(target: Path, health) -> ProductResult | None:
    if health.overall_state == NOT_INITIALIZED:
        return _refusal(target, NOT_INITIALIZED,
                        f"Project is not initialized. Run `tabu init {target}`.")
    if health.overall_state == NOT_ONBOARDED:
        return _refusal(target, NOT_ONBOARDED,
                        f"Project is not onboarded. Run `tabu onboard {target}`.")
    if health.overall_state not in {"READY", DEGRADED}:
        return _refusal(target, INVALID_PROJECT,
                        f"Project health is {health.overall_state}. Run "
                        f"`tabu doctor {target}`.")
    return None


_UNUSABLE_TASK_DETAIL_LIMIT = 5
_TERMINAL_TASK_STATES = "COMPLETED/CANCELLED/ARCHIVED"
# Output-boundary bound for one surfaced diagnostic fragment. Mirrors the
# established per-diagnostic detail bound already used for worker-facing
# context diagnostics in tabularium/engine/execution/pipeline.py (_context_gate).
_DIAGNOSTIC_MAX_CHARS = 160


def _sanitize_fragment(text: str, *,
                       limit: int = _DIAGNOSTIC_MAX_CHARS) -> str:
    """Make one user-derived diagnostic fragment terminal-safe and bounded.

    Every non-printable character (C0/C1 controls, DEL, Unicode line and
    format separators included) becomes its visible ``ascii()`` escape, so a
    record can never inject terminal control sequences or fabricate extra
    output lines. Whitespace runs collapse deterministically and the fragment
    is hard-bounded to ``limit``. Escaping runs BEFORE truncation, so no live
    escape sequence can be split. This is the user-facing representation only;
    internal validation evidence is never rewritten.

    The default limit is the established Task-diagnostic bound; callers may
    pass a larger bound when the evidence was already stored under that larger
    bound elsewhere (worker failure detail uses the worker-side 300).
    """
    if not isinstance(text, str):
        return ""
    escaped = "".join(ch if ch.isprintable() else ascii(ch)[1:-1]
                      for ch in text)
    collapsed = " ".join(escaped.split())
    if len(collapsed) > limit:
        return collapsed[:limit - 3].rstrip() + "..."
    return collapsed


def _plural_count(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {plural}"


def _classify_task_records(repo):
    """Split discovered Task records without altering usability semantics.

    Classification reuses the existing predicates only: the repository-level
    `record_usable` decides selection eligibility exactly as before, and the
    file-level `usable` tells identity ambiguity apart from record defects.
    No record is repaired, normalized, or chosen among ambiguous identities.
    """
    active: list = []
    terminal: list = []
    unusable: list = []
    for rec in repo.family_records("Task"):
        if repo.record_usable(rec):
            (terminal if rec.status in TERMINAL_TASK_STATUSES
             else active).append(rec)
        elif rec.usable:
            # Individually valid file, but its declared id is claimed by more
            # than one file; repository-level usability refuses a winner.
            unusable.append((rec, f"canonical id {rec.declared_id} is claimed "
                                  f"by multiple files; no winner is chosen"))
        else:
            unusable.append((rec, rec.defect_reason or "record is unusable"))
    return active, terminal, unusable


def _task_required_refusal(target: Path, terminal: list, unusable: list):
    """Fail-closed zero-active-Task refusal with bounded evidence detail."""
    lines = ["No active Task is available for this run."]
    if not terminal and not unusable:
        lines.append(
            "No usable Task record was found under .ai/tasks/. Create "
            ".ai/tasks/TASK-YYYY-NNNN.md by hand so the filename equals the "
            "id; see .ai/tasks/README.md for the record format.")
    else:
        if unusable:
            lines.append(_plural_count(len(unusable),
                                       "unusable Task record:",
                                       "unusable Task records:"))
            lines.extend(f"- {_sanitize_fragment(rec.rel_path)}: "
                         f"{_sanitize_fragment(reason)}"
                         for rec, reason in
                         unusable[:_UNUSABLE_TASK_DETAIL_LIMIT])
            hidden = len(unusable) - _UNUSABLE_TASK_DETAIL_LIMIT
            if hidden > 0:
                lines.append(f"- (and {hidden} more unusable Task records)")
            lines.append("Tabularium never repairs Task records; correct them so "
                         "they satisfy the Task contract.")
        if terminal:
            counted = _plural_count(len(terminal), "Task record is",
                                    "Task records are")
            lines.append(f"{counted} in a terminal state "
                         f"({_TERMINAL_TASK_STATES}) and cannot be activated.")
    lines.append("Create or select an active Task, then retry.")
    return _refusal(target, TASK_REQUIRED, "\n".join(lines))


def _choose_task(target: Path, requested: str | None):
    if requested:
        return requested, None
    try:
        repo = discover(target, ProjectScope(target))
    except (OSError, RuntimeError, ValueError):
        # A discovery/runtime failure is not evidence that no Task exists;
        # stay fail-closed without asserting absence or exposing internals.
        return None, _refusal(
            target, TASK_REQUIRED,
            "No active Task is available for this run. Task records could "
            "not be inspected because project discovery failed; run "
            f"`tabu doctor {_sanitize_fragment(str(target))}` and retry.")
    if any(family == "Task" for family, _label in repo.unreadable_dirs):
        return None, _refusal(
            target, TASK_REQUIRED,
            "No active Task is available for this run. Task storage is "
            "unreadable: .ai/tasks/ exists but its contents cannot be "
            "listed, so Task records cannot be inspected. Restore read "
            "access to .ai/tasks/, then retry.")
    active, terminal, unusable = _classify_task_records(repo)
    candidates = sorted(c.declared_id for c in active if c.declared_id)
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, _task_required_refusal(target, terminal, unusable)
    return None, _refusal(target, TASK_SELECTION_REQUIRED,
                          "More than one active Task is available. Re-run "
                          "with `--task TASK-YYYY-NNNN`.")


def _checkpoint_path(target: Path) -> Path:
    identity = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:20]
    root = Path(tempfile.gettempdir()) / "tabularium-os" / "checkpoints"
    return root / f"{identity}.db"


def _execution_id(target: Path) -> str:
    return f"run-{int(time.time() * 1000)}-{hashlib.sha1(str(target).encode()).hexdigest()[:8]}"


def _execution_id_from_checkpoint(path: Path) -> str:
    # The checkpoint file can contain several LangGraph rows. The existing
    # runtime is the authority for inspection; use the latest known id only
    # when it is available through the stable product sidecar name.
    marker = path.with_suffix(".execution")
    try:
        return marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def remember_execution(target: str | Path, execution_id: str) -> None:
    """Record only the operational execution locator beside the checkpoint."""
    marker = _checkpoint_path(Path(target).expanduser().resolve()).with_suffix(
        ".execution")
    write_secure_file(marker, execution_id)


_WORKER_ERROR_EVENT = "WORKER_ERROR_EVENT"
_MAX_SURFACED_WORKER_ERRORS = 2
# Render bound for surfaced worker failure evidence. It matches the worker's
# own stored-detail bound (_ERROR_DETAIL_LIMIT in
# tabularium/engine/workers/cli/worker.py): rendering is never asked to show more than
# was already stored, and the stored bound itself never grows here.
_WORKER_ERROR_RENDER_LIMIT = 300

# B6-A2: whitelisted transport facts for generic native-worker failures.
# Every fact is derived ONLY from the worker's trusted internal diagnostics
# (never from provider output) and renders as one of these fixed sentences;
# the only dynamic byte content is a validated integer exit code. The
# canonical order below is fixed regardless of diagnostic order.
#
# Two independent reasons a stored EXIT_CODE may not be an ordinary exit:
# a TIMED_OUT outcome means the transport killed the process, and on POSIX a
# NEGATIVE poll() value is signal termination (including -9 from the known
# CANCELLED/EXITED race). Both are kill/signal artefacts and must never be
# presented as ordinary exits, whatever outcome they were stored under.
_NATIVE_TIMEOUT_FACT = "Worker execution timed out."
_NATIVE_STDERR_FACT = "Worker wrote stderr output; contents withheld."
_MAX_NATIVE_FAILURE_FACTS = 3
# Canonical positive ASCII decimal only: no sign, no leading zeros, no
# surrounding whitespace. [0-9] is explicit — \d would also match non-ASCII
# digit forms that are not canonical transport representations.
_NATIVE_EXIT_CODE = re.compile(r"[1-9][0-9]*")


def _native_exit_fact(detail: str) -> str:
    """The exit fact for one EXIT_CODE diagnostic detail, or "".

    Accepts ONLY a canonical ASCII decimal representation of a strictly
    positive integer; anything else — zero, negative (signal death),
    empty, "None", floats, underscores, whitespace padding, non-ASCII
    digits, arbitrary text — fails closed to "" without raising. After the
    full match, int() cannot fail.
    """
    text = str(detail)
    if not _NATIVE_EXIT_CODE.fullmatch(text):
        return ""
    return f"Worker process exited with code {int(text)}."


def _native_failure_facts(result: PipelineResult) -> list[str]:
    """Whitelisted, deduped, canonically ordered transport facts.

    Reads only three diagnostic codes stored by our own native worker:
    TRANSPORT_OUTCOME, EXIT_CODE, STDERR_PRESENT. Everything else — including
    every detail string — stays internal. At most one fact per category and
    at most `_MAX_NATIVE_FAILURE_FACTS` facts total.
    """
    outcome = ""
    exit_code_seen = False
    exit_fact = ""
    stderr_present = False
    for diagnostic in getattr(result, "diagnostics", ()) or ():
        if not isinstance(diagnostic, dict):
            continue
        code = diagnostic.get("code")
        if code == "TRANSPORT_OUTCOME" and not outcome:
            outcome = str(diagnostic.get("detail", ""))
        elif code == "EXIT_CODE" and not exit_code_seen:
            exit_code_seen = True
            exit_fact = _native_exit_fact(str(diagnostic.get("detail", "")))
        elif code == "STDERR_PRESENT":
            stderr_present = True
    facts: list[str] = []
    if outcome == "TIMED_OUT":
        facts.append(_NATIVE_TIMEOUT_FACT)
    elif outcome == "EXITED" and exit_fact:
        facts.append(exit_fact)
    if stderr_present:
        facts.append(_NATIVE_STDERR_FACT)
    return facts[:_MAX_NATIVE_FAILURE_FACTS]


def _worker_error_lines(result: PipelineResult) -> list[str]:
    """Terminal-safe worker failure evidence, whitelisted to one code.

    ONLY `WORKER_ERROR_EVENT` is surfaced. EXIT_CODE, USAGE, STDERR_PRESENT,
    WORKER_FAILED and every other runtime diagnostic stay internal — this is
    deliberately not a diagnostic dump. Details are deduplicated, bounded by
    the worker-side stored-detail bound, and capped in number so a worker
    cannot turn a failure into an unbounded terminal list.
    """
    details: list[str] = []
    for diagnostic in getattr(result, "diagnostics", ()) or ():
        if not isinstance(diagnostic, dict):
            continue
        if diagnostic.get("code") != _WORKER_ERROR_EVENT:
            continue
        detail = _sanitize_fragment(str(diagnostic.get("detail", "")),
                                    limit=_WORKER_ERROR_RENDER_LIMIT)
        if detail and detail not in details:
            details.append(detail)
            if len(details) == _MAX_SURFACED_WORKER_ERRORS:
                break
    return details


def _with_worker_errors(message: str, result: PipelineResult) -> str:
    """Attach what the worker itself reported, without changing the reason.

    Structured WORKER_ERROR_EVENT evidence stays the primary detailed
    section; the static B6-A2 transport facts (timeout / genuine exit code /
    stderr-was-written presence) follow it, each already terminal-safe and
    bounded by construction.
    """
    details = _worker_error_lines(result)
    facts = _native_failure_facts(result)
    if not details and not facts:
        return message
    lines = [message]
    if details:
        worker_id = _sanitize_fragment(
            str((result.worker_result or {}).get("worker_id", "")))
        header = (f"Worker {worker_id} reported:" if worker_id
                  else "The worker reported:")
        lines.append(header)
        lines.extend(f"- {d}" for d in details)
    lines.extend(facts)
    return "\n".join(lines)


_TASK_STORAGE_LABEL = ".ai/tasks"
# Loader diagnostic codes read here. Only our own discovery layer emits them;
# the labels in `sources` are the loader's fixed bounded family-directory
# names, never filesystem error text.
_STORAGE_UNREADABLE_CODE = "RECORD_DIRECTORY_UNREADABLE"
_ROOT_TASK_UNRESOLVED_CODE = "ROOT_TASK_UNRESOLVED"


def _context_not_ready_guidance(result: PipelineResult) -> str:
    """Bounded recovery guidance for a CONTEXT_PACKAGE_NOT_READY refusal.

    The stable product error code is unchanged; this only separates an
    explicit-Task refusal caused by unlistable record storage from one where
    the id genuinely does not resolve to a usable Task. Reads only
    whitelisted codes from our own loader, so no raw OSError or arbitrary
    path ever reaches this text.
    """
    labels: list[str] = []
    root_unresolved = False
    for diagnostic in getattr(result, "diagnostics", ()) or ():
        if not isinstance(diagnostic, dict):
            continue
        code = diagnostic.get("code")
        if code == _STORAGE_UNREADABLE_CODE:
            for source in diagnostic.get("sources") or ():
                label = _sanitize_fragment(str(source))
                if label and label not in labels:
                    labels.append(label)
        elif code == _ROOT_TASK_UNRESOLVED_CODE:
            root_unresolved = True
    if _TASK_STORAGE_LABEL in labels:
        lines = [
            "The requested Task could not be inspected because Task storage "
            f"is unreadable: {_TASK_STORAGE_LABEL}/ exists but its contents "
            "cannot be listed.",
        ]
        others = [label for label in labels if label != _TASK_STORAGE_LABEL]
        if others:
            lines.append("Record directories that could not be listed: "
                         + ", ".join(others[:2]) + ".")
        lines.append("Restore read access, then retry.")
        return "\n".join(lines)
    if labels:
        return ("Tabularium did not run the request. Record directories that "
                "could not be listed: " + ", ".join(labels[:3])
                + ". Restore read access, then retry.")
    if root_unresolved:
        return ("Tabularium did not run the request. The requested --task id did "
                "not resolve to exactly one usable Task record. Verify the "
                "id, the filename, and the required frontmatter fields, "
                "then retry.")
    return ("Tabularium did not run the request. Review the reason and try "
            "the indicated remediation.")


_RUNTIME_START_FAILED_CODE = "RUNTIME_START_FAILED"
# Our own lazy runtime loader raises ImportError with this fixed vocabulary
# when the governed runtime dependency itself is absent. Matching on it keeps
# other RUNTIME_START_FAILED causes (checkpoint, transport) from receiving
# install advice they do not need.
_RUNTIME_DEPENDENCY_MARKERS = ("ImportError:", "LangGraph")

# Bounded recovery guidance for the three provider-class failures that
# previously fell through to the generic remediation. The machine-readable
# reason codes are unchanged; only the human-facing message becomes specific.
# Every sentence is STATIC: no provider prose, no diagnostic detail, and no
# retry/fallback behavior is implied or performed here.
_RATE_LIMITED_GUIDANCE = (
    "The selected model's provider rate limit was reached. Wait and retry "
    "later; respect the provider's limits.")
_CONTEXT_WINDOW_EXCEEDED_GUIDANCE = (
    "The request exceeded the selected model's context window. Reduce the "
    "supplied context or input, then retry.")
_CONTENT_REJECTED_GUIDANCE = (
    "The provider rejected the request content under its policy. Review the "
    "instruction and supplied content against the provider's policy, then "
    "retry.")


def _runtime_start_guidance(result: PipelineResult) -> str:
    """Bounded recovery guidance for a RUNTIME_START_FAILED refusal.

    The stable machine code is unchanged. When the failure is our own
    dependency-missing ImportError, the refusal names the documented install
    syntax and the doctor capability that reports the same fact; any other
    cause keeps the generic remediation.
    """
    for diagnostic in getattr(result, "diagnostics", ()) or ():
        if not isinstance(diagnostic, dict):
            continue
        if diagnostic.get("code") != _RUNTIME_START_FAILED_CODE:
            continue
        detail = str(diagnostic.get("detail", ""))
        if all(marker in detail for marker in _RUNTIME_DEPENDENCY_MARKERS):
            return "\n".join([
                "Tabularium did not run the request: the governed runtime "
                "dependency is not installed.",
                'Install it with: pip install -e ".[runtime]" from a '
                'source checkout, or pip install '
                '"<release-wheel>[runtime]" from a downloaded artifact.',
                "`tabu doctor` lists this as the optional runtime capability.",
            ])
    return ("Tabularium did not run the request. Review the reason and try "
            "the indicated remediation.")


def _policy_violation_guidance(result: PipelineResult) -> str:
    """Format diagnostic details for POLICY_VIOLATION into user-facing message."""
    violations: list[str] = []
    for diagnostic in getattr(result, "diagnostics", ()) or ():
        if isinstance(diagnostic, dict):
            detail = str(diagnostic.get("detail", "")).strip()
            code = diagnostic.get("code")
            if detail and code != "POLICY_VIOLATION":
                violations.append(detail)
    lines: list[str] = []
    if violations:
        for item in violations[:3]:
            if item.startswith("Policy "):
                lines.append(_sanitize_fragment(item))
            elif ":" in item:
                policy, _, rest = item.partition(":")
                lines.append(f"Policy {_sanitize_fragment(policy.strip())}: {_sanitize_fragment(rest.strip())}")
            else:
                lines.append(f"Policy {_sanitize_fragment(item)}")
        overflow = len(violations) - 3
        if overflow > 0:
            lines.append(f"(+{overflow} more)")
    lines.append("Review the reason and try the indicated remediation.")
    return "\n".join(lines)


def _from_pipeline(target: Path, result: PipelineResult,
                   warnings: tuple[str, ...] = ()) -> ProductResult:
    if result.status == "AWAITING_APPROVAL":
        return ProductResult(result.status, "APPROVAL_REQUIRED",
                             "Human approval is required before this work "
                             "can complete.", str(target), result,
                             result.approval_request, warnings)
    if result.status == "COMPLETED":
        return ProductResult(result.status, "COMPLETED", "Work completed.",
                             str(target), result, None, warnings)
    code = result.reason_codes[0] if result.reason_codes else "EXECUTION_FAILED"
    if code == "NATIVE_WORKER_UNAVAILABLE":
        product_code = WORKER_UNAVAILABLE
        message = "The selected native worker is unavailable. Install or " \
                  "configure a supported session-authenticated CLI."
    elif code == "LITELLM_UNAVAILABLE":
        product_code = WORKER_UNAVAILABLE
        message = "The selected API worker is unavailable. Install the API " \
                  "extra and try again."
    elif code == "AUTHENTICATION_ERROR":
        product_code = API_CREDENTIAL_REQUIRED
        message = "The API worker needs provider credentials in the operator " \
                  "environment; no credential is required for the native worker."
    elif code == "RATE_LIMITED":
        product_code = code
        message = _RATE_LIMITED_GUIDANCE
    elif code == "CONTEXT_WINDOW_EXCEEDED":
        product_code = code
        message = _CONTEXT_WINDOW_EXCEEDED_GUIDANCE
    elif code == "CONTENT_REJECTED":
        product_code = code
        message = _CONTENT_REJECTED_GUIDANCE
    elif code in {"PROVIDER_UNAVAILABLE",
                  "INVALID_REQUEST", "TIMEOUT", "TRANSPORT_ERROR",
                  "UNKNOWN_PROVIDER_ERROR"}:
        product_code = "WORKER_EXECUTION_FAILED"
        message = "The selected worker could not complete the request. " \
                  "Check its configuration and try again."
    elif code == "CONTEXT_PACKAGE_NOT_READY":
        product_code = code
        message = _context_not_ready_guidance(result)
    elif code == _RUNTIME_START_FAILED_CODE:
        product_code = code
        message = _runtime_start_guidance(result)
    elif code == "WORKER_PERMISSION_DENIED":
        product_code = code
        perm_detail = ""
        for diagnostic in getattr(result, "diagnostics", ()) or ():
            if isinstance(diagnostic, dict) and diagnostic.get("code") == "WORKER_PERMISSION_DENIED":
                perm_detail = str(diagnostic.get("detail", ""))
                break
        message = perm_detail or "Worker permission denied."
    elif code == "POLICY_VIOLATION":
        product_code = code
        message = _policy_violation_guidance(result)
    else:
        product_code = code
        message = "Tabularium did not run the request. Review the reason and try " \
                  "the indicated remediation."
    return ProductResult(result.status, product_code,
                         _with_worker_errors(message, result), str(target),
                         result, None, warnings)


def _refusal(target: Path, code: str, message: str, detail: str = ""):
    return ProductResult("REFUSED", code, message, str(target))


def _select_worker(target: Path, requested: str | None, model: str,
                   adapter: Any):
    """Choose exactly one adapter; never retry or switch after selection."""
    if adapter is not None:
        if requested:
            return None, _refusal(target, WORKER_SELECTION_INVALID,
                                  "A test adapter cannot be combined with "
                                  "an explicit worker selection.")
        return adapter, None

    choice = (requested or "").strip().casefold()
    if choice in {"native", "codex", "codex-cli"}:
        executable = find_native_executable()
        if not executable:
            return None, _refusal(target, WORKER_UNAVAILABLE,
                                  "The selected native worker is unavailable. "
                                  "Install or configure the existing Codex CLI.")
        return NativeCLIWorker(executable=executable), None
    if choice in {"opencode", "opencode-cli"}:
        executable = find_opencode_executable()
        if not executable:
            return None, _refusal(target, WORKER_UNAVAILABLE,
                                  "The selected OpenCode worker is unavailable. "
                                  "Install or configure the OpenCode CLI.")
        return OpenCodeCLIWorker(executable=executable, model=model or None), None
    if choice in {"api", "api-model", "litellm"}:
        if not model:
            return None, _refusal(target, WORKER_CONFIGURATION_MISSING,
                                  "The API worker requires a resolved model. "
                                  "Set TABU_MODEL or pass --model.")
        return APIWorker(model_target=ModelTarget(model)), None
    if choice:
        return None, _refusal(target, WORKER_SELECTION_INVALID,
                              f"Unknown worker {requested!r}. Choose native, opencode, or api.")

    # Omitted worker: native/session-authenticated first, then OpenCode, then API.
    # Neither path falls back to the other after execution begins.
    executable = find_native_executable()
    if executable:
        return NativeCLIWorker(executable=executable), None
    opencode_bin = find_opencode_executable()
    if opencode_bin:
        return OpenCodeCLIWorker(executable=opencode_bin, model=model or None), None
    if model:
        return APIWorker(model_target=ModelTarget(model)), None
    return None, _refusal(target, WORKER_SELECTION_REQUIRED,
                          "No session-authenticated native/opencode worker is available "
                          "and no API model is configured. Choose --worker native, "
                          "--worker opencode, or configure TABU_MODEL for --worker api.")


def _request_value(request: Any, name: str) -> str:
    if isinstance(request, dict):
        return str(request.get(name) or "")
    return str(getattr(request, name, "") or "")


def build_execution_report_proposal(target: Path | str,
                                    result: ProductResult,
                                    *,
                                    title: str | None = None,
                                    author: str = "USER") -> Any | None:
    """Build a deterministic Report PersistenceProposal from a COMPLETED execution.

    Returns a PreparedProposal containing a PersistenceProposal, or None if the
    execution did not complete or has no pipeline output.
    """
    if result.status != "COMPLETED":
        return None
    pipeline = result.pipeline
    raw_output = pipeline.output if pipeline is not None else ""
    if not raw_output and not getattr(result, "change_result", None):
        return None
    output_text = raw_output or result.message or "Execution completed."

    from datetime import datetime, timezone
    from tabularium.engine.continuation.model import ContinuationPlan, ExecutionOutcome, ReportSpec
    from tabularium.engine.continuation.planner import build_proposals

    target_path = Path(target).resolve()
    execution_id = pipeline.execution_id if pipeline else f"run-{int(datetime.now(timezone.utc).timestamp()*1000)}"
    task_id = getattr(pipeline, "task_id", "") if pipeline else ""
    worker_id = (getattr(pipeline, "worker_id", "") if pipeline else "") or (
        (pipeline.worker_result or {}).get("worker_id", "") if pipeline else ""
    )
    worker_category = (getattr(pipeline, "worker_category", "") if pipeline else "") or (
        (pipeline.worker_result or {}).get("worker_category", "") if pipeline else ""
    )

    outcome = ExecutionOutcome(
        execution_id=execution_id,
        task_id=task_id,
        worker_id=worker_id,
        worker_category=worker_category,
        runtime_status=result.status,
        output=output_text,
    )
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report_title = title or f"Execution Report for {outcome.execution_id}"

    validation_text = "Validated by Tabularium governed pipeline execution."
    change_info = getattr(result, "change_result", None)
    if change_info and isinstance(change_info, dict):
        validation_text += (
            f"\n\n### Change Path Record\n"
            f"- Proposal Binding: {change_info.get('binding', '')}\n"
            f"- Diff Digest: {change_info.get('diff_digest', '')}\n"
            f"- Gate Result: {change_info.get('gate_result', '')}\n"
            f"- Decision: {change_info.get('decision', '')}\n"
            f"- Apply Result: {change_info.get('apply_result', '')}"
        )

    spec = ReportSpec(
        title=report_title,
        outcome=output_text[:1000] if len(output_text) > 1000 else output_text,
        created_by=author,
        created_at=now_iso,
        validation=validation_text,
        limitations="None recorded.",
    )
    plan = ContinuationPlan(report=spec)
    proposals = build_proposals(outcome, plan, str(target_path))
    if proposals and proposals[0].ok:
        return proposals[0]
    return None


def apply_execution_report_proposal(target: Path | str,
                                    proposal: Any,
                                    *,
                                    authorized_by: str,
                                    authorized_at: str,
                                    note: str = "") -> Any:
    """Apply an authorized Report proposal through the 9-condition persistence gate.

    Writes atomized Markdown to .ai/reports/ and returns a PersistenceResult.
    """
    from tabularium.engine.persistence.model import PersistenceAuthorization
    from tabularium.engine.persistence.writer import apply_authorized_proposal

    target_path = Path(target).resolve()
    inner = getattr(proposal, "proposal", proposal)
    auth = PersistenceAuthorization(
        proposal_id=inner.proposal_id,
        execution_id=inner.execution_id,
        target_path=inner.target_path,
        operation=inner.operation,
        content_digest=inner.content_digest,
        authorized_by=authorized_by,
        authorized_at=authorized_at,
        binding=inner.binding(),
        note=note or "Execution report persistence authorization",
    )
    return apply_authorized_proposal(inner, str(target_path), auth)

