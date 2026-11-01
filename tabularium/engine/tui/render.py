"""Pure dashboard rendering (Faz A).

No IO, no curses import: every function maps a snapshot to plain text lines,
so the whole view layer is unit-testable without a terminal.
"""

from __future__ import annotations

from .model import DashboardSnapshot


def _checklist(snapshot: DashboardSnapshot) -> list[str]:
    lines = ["Capabilities:"]
    if not snapshot.capabilities:
        lines.append("  (none reported)")
        return lines
    for cap in snapshot.capabilities:
        mark = "ok" if cap.available else "--"
        lines.append(f"  [{mark}] {cap.name}")
    return lines


def _tasks(snapshot: DashboardSnapshot) -> list[str]:
    lines = [f"Tasks ({len(snapshot.tasks)}):"]
    if not snapshot.tasks:
        lines.append("  No Task records. Create one with: tabu task create --title ... --objective ...")
        return lines
    for task in snapshot.tasks:
        lines.append(f"  {task.id or '?'} [{task.status or task.state}] {task.title or ''}".rstrip())
    return lines


def _preflight(snapshot: DashboardSnapshot) -> list[str]:
    if not snapshot.instruction:
        return []
    lines = [f"Preflight: {snapshot.instruction}"]
    if not snapshot.gates:
        lines.append("  (no gates reported)")
        return lines
    for gate in snapshot.gates:
        detail = f" — {gate.detail}" if gate.detail else ""
        lines.append(f"  {gate.gate}: {gate.outcome}{detail}")
    return lines


def render_dashboard(snapshot: DashboardSnapshot) -> str:
    """Render the full read-only dashboard as plain text."""
    selection = snapshot.task_selection or "none"
    sections = [
        [f"Tabularium {snapshot.tabularium_version or 'unknown'} — {snapshot.overall_state} (read-only)"],
        [f"Task selection: {selection}"],
        _checklist(snapshot),
        _tasks(snapshot),
        _preflight(snapshot),
        ["Keys: q quit · r refresh · A approve · C continue · P persist · O onboard · R run"],
    ]
    out: list[str] = []
    for i, section in enumerate(sections):
        if not section:
            continue
        if i:
            out.append("")
        out.extend(section)
    return "\n".join(out) + "\n"
