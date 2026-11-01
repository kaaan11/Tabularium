"""Read-only terminal dashboard (Faz A).

STATUS: READONLY dashboard + APPROVAL SHEETS (Faz B, DEC-2026-0008 A2)
+ BACKGROUND RUNS (Faz C, DEC-2026-0009 A2).
The dashboard renders the existing read-only surfaces (`status`,
`task list`, `explain`). Approval sheets (task approve, continue, persist,
onboard) require typed confirmation and execute through the same functions
the CLI calls. New work runs in a background thread through the same
`run_instruction` entrypoint with a live JSONL event view; stop detaches,
never kills. Never auto-approves, never repairs. Standard library only.
"""

from .app import run
from .model import (
    CapabilityRow,
    DashboardSnapshot,
    GateRow,
    TaskRow,
    build_snapshot,
)
from .render import render_dashboard
from . import runner
from . import widgets

__all__ = [
    "CapabilityRow",
    "DashboardSnapshot",
    "GateRow",
    "TaskRow",
    "build_snapshot",
    "render_dashboard",
    "run",
    "runner",
    "widgets",
]
