"""Deterministic local memory/source fixture for M6.2 conformance tests."""

from __future__ import annotations

from .model import MemoryRecord, SourceSnapshot
from .store import InMemoryMemoryStore

PROJECT_ID = "fixture-project"


class FixtureSourceResolver:
    def __init__(self, snapshots: tuple[SourceSnapshot, ...] = ()) -> None:
        self._snapshots = {(item.source_kind, item.source_id): item
                           for item in snapshots}

    def resolve(self, source_kind: str, source_id: str) -> SourceSnapshot | None:
        return self._snapshots.get((source_kind, source_id))

    def put(self, snapshot: SourceSnapshot) -> None:
        self._snapshots[(snapshot.source_kind, snapshot.source_id)] = snapshot

    def remove(self, source_kind: str, source_id: str) -> None:
        self._snapshots.pop((source_kind, source_id), None)


def build_fixture() -> tuple[InMemoryMemoryStore, FixtureSourceResolver]:
    resolver = FixtureSourceResolver((
        SourceSnapshot("TASK-2026-0001", "TASK", "task-v1"),
        SourceSnapshot("HND-2026-0001", "HANDOFF", "handoff-v1"),
        SourceSnapshot("EVD-2026-0001", "EVIDENCE", "evidence-v1"),
    ))
    store = InMemoryMemoryStore()
    for record in (
        MemoryRecord("mem-task", PROJECT_ID, "TASK", "TASK-2026-0001",
                     "task-v1", "The task continues through governed execution.",
                     tags=("execution", "task")),
        MemoryRecord("mem-handoff", PROJECT_ID, "HANDOFF", "HND-2026-0001",
                     "handoff-v1", "The previous session handed off the review context.",
                     tags=("continuation",)),
        MemoryRecord("mem-evidence", PROJECT_ID, "EVIDENCE", "EVD-2026-0001",
                     "evidence-v1", "A source-bound observation is available for review.",
                     tags=("review",)),
    ):
        store.index(record)
    return store, resolver
