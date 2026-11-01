"""Read-only dashboard runner with approval sheets (Faz A + Faz B).

The dashboard itself only reads. Approval acts (Faz B, DEC-2026-0008 A2)
open explicit sheets that re-display the content under decision and require
typed confirmation; every sheet executes through the same functions the CLI
calls (`tabularium.engine.tui.widgets`). `curses` is imported lazily so the package
stays importable (and testable) where no terminal exists; without a TTY the
dashboard prints once as plain text.
"""

from __future__ import annotations

import sys

from .model import build_snapshot
from .render import render_dashboard
from . import runner
from . import widgets
from . import widgets


def render_once(raw_target=None, instruction: str | None = None) -> str:
    """Build one snapshot and render it. Writes nothing."""
    return render_dashboard(build_snapshot(raw_target, instruction))


def run(raw_target=None, instruction: str | None = None) -> int:
    """Run the dashboard. Returns a process exit code."""
    if not sys.stdout.isatty():
        sys.stdout.write(render_once(raw_target, instruction))
        return 0
    try:
        import curses
    except ImportError:
        sys.stdout.write(render_once(raw_target, instruction))
        return 0
    return curses.wrapper(_main, raw_target, instruction)


def _say(stdscr, curses, lines: list[str]) -> None:
    stdscr.erase()
    for row, line in enumerate(lines[: curses.LINES - 1]):
        try:
            stdscr.addstr(row, 0, line[: curses.COLS - 1])
        except curses.error:
            break
    stdscr.addstr(min(len(lines), curses.LINES - 1), 0, "-- press any key --")
    stdscr.refresh()
    stdscr.getkey()


def _ask(stdscr, curses, prompt: str) -> str:
    stdscr.erase()
    stdscr.addstr(0, 0, prompt[: curses.COLS - 1])
    stdscr.refresh()
    curses.echo()
    try:
        typed = stdscr.getstr(2, 0, 256)
    except (KeyboardInterrupt, curses.error):
        typed = b""
    finally:
        curses.noecho()
    try:
        return typed.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        return ""


def flow_task_approve(stdscr, curses, raw_target) -> None:
    from tabularium import task as task_surface

    task_id = _ask(stdscr, curses, "Task id to approve: ").strip()
    if not task_id:
        return
    shown = task_surface.task_show(raw_target, task_id)
    actor = _ask(stdscr, curses, "Recorded by (default USER): ").strip() or "USER"
    _say(stdscr, curses, widgets.render_task_approve_sheet(
        task_id, shown.status, actor).splitlines())
    confirmation = _ask(stdscr, curses, "Type `approve` to record: ")
    result = widgets.approve_task(raw_target, task_id, actor=actor,
                                  confirmation=confirmation)
    if getattr(result, "code", None):
        _say(stdscr, curses, [f"Not recorded: {result.code}",
                              getattr(result, "message", "")])
    else:
        _say(stdscr, curses, [f"Approved {task_id}."])


def flow_continue(stdscr, curses, raw_target):
    pending = widgets.probe_pending_approval(raw_target)
    if getattr(pending, "status", None) != "AWAITING_APPROVAL":
        _say(stdscr, curses, ["No pending approval.",
                              getattr(pending, "message", "")])
        return None
    summary = widgets.summarize_request(
        getattr(pending, "approval_request", None))
    actor = _ask(stdscr, curses, "Decided by (required): ").strip()
    timestamp = _ask(stdscr, curses, "Decision time with UTC offset (required): ").strip()
    _say(stdscr, curses, widgets.render_execution_sheet(
        summary, actor, timestamp).splitlines())
    decision = _ask(stdscr, curses, "Type `approve` or `reject`: ")
    result = widgets.resolve_pending_approval(
        raw_target, decision_word=decision, actor=actor, timestamp=timestamp)
    _say(stdscr, curses, [f"Outcome: {getattr(result, 'status', '?')}",
                          getattr(result, "message", "")])
    if getattr(result, "status", None) == "COMPLETED":
        return result
    return None


def flow_persist(stdscr, curses, raw_target, result) -> None:
    from tabularium.execution import build_execution_report_proposal

    item = build_execution_report_proposal(raw_target or ".", result)
    if item is None or not getattr(item, "ok", False):
        _say(stdscr, curses, ["No persistable report proposal."])
        return
    proposal = item.proposal
    _say(stdscr, curses, widgets.render_persistence_sheet(
        proposal.record_id, proposal.target_path,
        proposal.content_digest).splitlines())
    confirmation = _ask(stdscr, curses, "Type the digest prefix to authorize: ")
    actor = _ask(stdscr, curses, "Authorized by (required): ").strip()
    timestamp = _ask(stdscr, curses,
                     "Authorization timestamp with UTC offset (Enter for now): ").strip()
    applied = widgets.authorize_persistence(
        raw_target, proposal, confirmation=confirmation, actor=actor,
        timestamp=timestamp or None)
    if applied is not None and getattr(applied, "written", False):
        _say(stdscr, curses, [f"Persisted {proposal.record_id}."])
    else:
        _say(stdscr, curses, ["Persistence not authorized; nothing was written."])


def flow_onboard(stdscr, curses, raw_target) -> None:
    declarations = {
        "title": _ask(stdscr, curses, "Project title: ").strip(),
        "purpose": _ask(stdscr, curses, "Purpose: ").strip(),
        "scope": _ask(stdscr, curses, "Scope: ").strip(),
    }
    if not all(declarations.values()):
        _say(stdscr, curses, ["Onboarding cancelled; title, purpose, and scope are required."])
        return
    from tabularium.onboarding import onboard_project

    draft = onboard_project(raw_target, dict(declarations))
    if draft.proposal is None:
        _say(stdscr, curses, [f"Onboarding: {draft.status}"])
        return
    targets = list(draft.proposal.files)
    actor = _ask(stdscr, curses, "Authorized by (required): ").strip()
    timestamp = _ask(stdscr, curses,
                     "Authorization timestamp with UTC offset (required): ").strip()
    _say(stdscr, curses, widgets.render_onboarding_sheet(
        targets, actor, timestamp).splitlines())
    confirmation = _ask(stdscr, curses, "Type `approve` to authorize: ")
    result = widgets.authorize_onboarding(
        raw_target, dict(declarations), confirmation=confirmation,
        actor=actor, timestamp=timestamp)
    _say(stdscr, curses, [f"Onboarding: {result.status}"])


def flow_run(stdscr, curses, raw_target):
    """Start one governed run in the background and follow its events.

    Returns the completed result when one arrives, else None. Stop (q)
    detaches the view; the execution continues and stays resumable.
    """
    import time

    from tabularium.explain import preflight

    instruction = _ask(stdscr, curses, "Instruction to run: ").strip()
    if not instruction:
        return None
    workflow = _ask(stdscr, curses,
                    "Workflow single/dag [single]: ").strip() or "single"
    flight = preflight(instruction, raw_target, workflow=workflow)
    gates = [f"  {g.gate}: {g.outcome}" + (f" — {g.detail}" if g.detail else "")
             for g in flight.gates]
    _say(stdscr, curses, [f"Preflight: {instruction}", *gates])
    if not widgets.confirm_word(
            _ask(stdscr, curses, "Type `run` to start (same governed path as `tabu run`): "),
            "run"):
        return None
    handle = runner.start_run(raw_target, instruction, workflow=workflow)
    _say(stdscr, curses, ["Run started in the background.",
                          "q detaches the view (the run continues)."])
    stdscr.nodelay(True)
    try:
        while handle.running:
            stdscr.erase()
            lines = ["Live events (q detaches):",
                     *runner.read_events(handle)]
            for row, line in enumerate(lines[: curses.LINES - 1]):
                try:
                    stdscr.addstr(row, 0, line[: curses.COLS - 1])
                except curses.error:
                    break
            stdscr.refresh()
            try:
                key = stdscr.getkey()
            except curses.error:
                key = None
            if key in ("q", "Q", "\x1b"):
                runner.detach(handle)
                _say(stdscr, curses, ["Detached: the execution continues to a "
                                      "checkpointable boundary.",
                                      "Resume with C (continue) anytime."])
                return None
            time.sleep(0.5)
    finally:
        stdscr.nodelay(False)
    result = runner.wait(handle, timeout=5)
    if isinstance(result, Exception):
        _say(stdscr, curses, [f"Run failed to start: {result}"])
        return None
    action = runner.next_action_for(getattr(result, "status", None))
    if action == "approval-sheet":
        _say(stdscr, curses, ["Run suspended at an approval boundary."])
        return flow_continue(stdscr, curses, raw_target)
    _say(stdscr, curses, [f"Outcome: {getattr(result, 'status', '?')}",
                          getattr(result, "message", "")])
    if getattr(result, "status", None) == "COMPLETED":
        return result
    return None


def _main(stdscr, raw_target=None, instruction: str | None = None) -> int:
    try:
        curses = sys.modules["curses"]
    except KeyError:
        return 1
    completed = None
    stdscr.clear()
    while True:
        text = render_once(raw_target, instruction)
        stdscr.erase()
        for row, line in enumerate(text.splitlines()[: curses.LINES - 1]):
            try:
                stdscr.addstr(row, 0, line[: curses.COLS - 1])
            except curses.error:
                break
        stdscr.refresh()
        key = stdscr.getkey()
        if key in ("q", "Q", "\x1b"):
            return 0
        if key == "A":
            flow_task_approve(stdscr, curses, raw_target)
        elif key == "C":
            outcome = flow_continue(stdscr, curses, raw_target)
            completed = outcome if outcome is not None else completed
        elif key == "P":
            if completed is None:
                _say(stdscr, curses, ["Nothing completed in this session yet. "
                                      "Resolve a pending approval (C) first."])
            else:
                flow_persist(stdscr, curses, raw_target, completed)
        elif key == "O":
            flow_onboard(stdscr, curses, raw_target)
        elif key == "R":
            outcome = flow_run(stdscr, curses, raw_target)
            completed = outcome if outcome is not None else completed
        # Any other key (incl. "r") refreshes on the next loop iteration.
