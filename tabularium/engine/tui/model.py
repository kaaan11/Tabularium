"""Read-only dashboard data model (Faz A).

Builds plain snapshots from the production read-only surfaces only:
`status_project`, `task_list`, and (optionally) `preflight`. Performs no
writes; the loaders themselves are read-only by construction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityRow:
    name: str
    available: bool


@dataclass(frozen=True)
class TaskRow:
    id: str | None
    status: str | None
    title: str | None
    state: str


@dataclass(frozen=True)
class GateRow:
    gate: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class DashboardSnapshot:
    tabularium_version: str | None
    overall_state: str
    capabilities: tuple[CapabilityRow, ...]
    task_selection: str | None
    tasks: tuple[TaskRow, ...]
    gates: tuple[GateRow, ...]
    instruction: str | None = None


def build_snapshot(raw_target=None, instruction: str | None = None) -> DashboardSnapshot:
    """Gather one read-only dashboard snapshot. Writes nothing."""
    from tabularium.diagnostics import status_project
    from tabularium import task as task_surface

    health = status_project(raw_target)
    capabilities = tuple(
        CapabilityRow(name=cap.name, available=cap.available)
        for cap in health.capabilities
    )
    listing = task_surface.task_list(raw_target)
    tasks = tuple(
        TaskRow(id=t.id, status=t.status, title=t.title, state=t.state)
        for t in listing.tasks
    )
    gates: tuple[GateRow, ...] = ()
    if instruction:
        from tabularium.explain import preflight

        flight = preflight(instruction, raw_target)
        gates = tuple(
            GateRow(gate=g.gate, outcome=g.outcome, detail=g.detail)
            for g in flight.gates
        )
    return DashboardSnapshot(
        tabularium_version=health.tabularium_version,
        overall_state=health.overall_state,
        capabilities=capabilities,
        task_selection=health.task_selection,
        tasks=tasks,
        gates=gates,
        instruction=instruction,
    )
