"""Trace collectors (M5.1).

FAILURE POLICY: **BEST_EFFORT**, and the choice is deliberate.

Observability describes execution; it must never be able to change it. If a
collector could raise into a production path, then turning tracing on would
alter what the system does — an observer that changes the observed outcome is
worse than no observer, and would quietly hand a trace backend veto power over
governed execution.

So `emit` never propagates. But a failure is not swallowed invisibly either:
every collector records its own errors in `failures`, and `healthy` reports
whether anything was lost. The trace becomes visibly incomplete rather than
silently wrong.

STRICT mode is deliberately NOT implemented. It would mean "abort a governed
execution because logging failed", which contradicts the invariant above, and
nothing in the current architecture needs it. Recorded as a deferred question
rather than guessed at.

Ordering is owned by the collector: it assigns the monotonic `sequence` that
`ExecutionTrace.ordered` sorts on, per execution, so timestamps are never the
ordering authority.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import events as E
from . import model as M


@runtime_checkable
class TraceCollector(Protocol):
    """What a production path needs from any collector.

    Deliberately tiny, and deliberately WRITE-ONLY from the caller's side.
    There is no `approve`, `authorize`, `decide`, or any read-back method that
    a production path could consult to make a decision — a collector can only
    be told things, never asked.
    """

    def emit(self, event: M.TraceEvent) -> None:
        """Record one event. MUST NOT raise."""
        ...

    def next_sequence(self, execution_id: str) -> int:
        """The next monotonic sequence number for this execution."""
        ...


class NoOpCollector:
    """Zero side effects. The default everywhere.

    With this installed, execution is byte-for-byte what it is with no
    observability at all: nothing is stored, nothing is computed, nothing can
    fail.
    """

    enabled = False

    def emit(self, event: M.TraceEvent) -> None:
        return None

    def next_sequence(self, execution_id: str) -> int:
        return 0

    @property
    def failures(self) -> tuple[dict[str, str], ...]:
        return ()

    @property
    def healthy(self) -> bool:
        return True


class InMemoryCollector:
    """Deterministic in-process collection, for tests and inspection.

    Thread-safe because a sequence counter shared across threads that is not
    guarded produces duplicate ordering keys, which would make "deterministic
    ordering" quietly false under any concurrent use.
    """

    enabled = True

    def __init__(self) -> None:
        self._events: list[M.TraceEvent] = []
        self._sequences: dict[str, int] = {}
        self._failures: list[dict[str, str]] = []
        self._lock = threading.Lock()

    # -- collection ---------------------------------------------------------

    def emit(self, event: M.TraceEvent) -> None:
        try:
            if not isinstance(event, M.TraceEvent):
                raise TypeError(f"not a TraceEvent: {type(event).__name__}")
            with self._lock:
                self._events.append(event)
        except Exception as exc:  # noqa: BLE001 - BEST_EFFORT, never propagate
            self._record_failure(exc)

    def next_sequence(self, execution_id: str) -> int:
        try:
            with self._lock:
                nxt = self._sequences.get(execution_id, 0) + 1
                self._sequences[execution_id] = nxt
                return nxt
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc)
            return 0

    # -- inspection ---------------------------------------------------------

    @property
    def events(self) -> tuple[M.TraceEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def trace(self, execution_id: str) -> M.ExecutionTrace:
        matching = [e for e in self.events if e.execution_id == execution_id]
        task_id = next((e.task_id for e in matching if e.task_id), "")
        return M.ExecutionTrace(execution_id=execution_id, task_id=task_id,
                                events=tuple(matching))

    def execution_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for event in self.events:
            if event.execution_id not in seen:
                seen.append(event.execution_id)
        return tuple(seen)

    def of_type(self, *event_types: str) -> tuple[M.TraceEvent, ...]:
        wanted = set(event_types)
        return tuple(e for e in self.events if e.event_type in wanted)

    @property
    def failures(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            return tuple(self._failures)

    @property
    def healthy(self) -> bool:
        return not self._failures

    def _record_failure(self, exc: BaseException) -> None:
        try:
            self._failures.append({"error": type(exc).__name__,
                                   "detail": str(exc)[:120]})
        except Exception:  # noqa: BLE001 - nothing left to do safely
            pass


class JsonlCollector:
    """Append-only JSONL at a CALLER-SUPPLIED operational path.

    Operational telemetry, never project truth: the path must live OUTSIDE
    `.ai/`, and this refuses to construct otherwise. A trace file inside the
    Project Context would sit alongside M1 records and start to look like one.

    Append-only and one JSON object per line, so a dead process leaves a valid
    prefix rather than a corrupt file, and a resumed process continues after
    it. No rotation, no compression, no database — those are later policy.
    """

    enabled = True

    def __init__(self, path: str | Path, project_root: str | Path | None = None
                 ) -> None:
        self.path = Path(path)
        if project_root is not None and _inside_ai_dir(self.path,
                                                       Path(project_root)):
            raise ValueError(
                "a trace file must not live inside .ai/: traces are "
                "operational observability, not project truth")
        self._sequences: dict[str, int] = {}
        self._failures: list[dict[str, str]] = []
        self._lock = threading.Lock()

    def emit(self, event: M.TraceEvent) -> None:
        try:
            if not isinstance(event, M.TraceEvent):
                raise TypeError(f"not a TraceEvent: {type(event).__name__}")
            line = json.dumps(event.to_dict(), sort_keys=True,
                              separators=(",", ":"))
            with self._lock:
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 - BEST_EFFORT, never propagate
            self._record_failure(exc)

    def next_sequence(self, execution_id: str) -> int:
        """Continue numbering after whatever is already on disk.

        Re-reading lets a resumed process append without colliding with the
        sequence numbers a previous process already wrote.
        """
        try:
            with self._lock:
                if execution_id not in self._sequences:
                    self._sequences[execution_id] = self._highest_on_disk(
                        execution_id)
                nxt = self._sequences[execution_id] + 1
                self._sequences[execution_id] = nxt
                return nxt
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc)
            return 0

    def read(self, execution_id: str | None = None) -> M.ExecutionTrace:
        """Read back a trace. Malformed lines are skipped, never fatal."""
        collected: list[M.TraceEvent] = []
        try:
            if self.path.is_file():
                with open(self.path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = M.TraceEvent.from_dict(json.loads(line))
                        except ValueError:
                            continue
                        if event is None:
                            continue
                        if execution_id and event.execution_id != execution_id:
                            continue
                        collected.append(event)
        except OSError as exc:
            self._record_failure(exc)
        task_id = next((e.task_id for e in collected if e.task_id), "")
        return M.ExecutionTrace(execution_id=execution_id or "",
                                task_id=task_id, events=tuple(collected))

    def _highest_on_disk(self, execution_id: str) -> int:
        highest = 0
        for event in self.read(execution_id).events:
            highest = max(highest, event.sequence)
        return highest

    @property
    def failures(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            return tuple(self._failures)

    @property
    def healthy(self) -> bool:
        return not self._failures

    def _record_failure(self, exc: BaseException) -> None:
        try:
            self._failures.append({"error": type(exc).__name__,
                                   "detail": str(exc)[:120]})
        except Exception:  # noqa: BLE001
            pass


NO_OP = NoOpCollector()


def resolve(collector: Any) -> Any:
    """Normalize any caller value into something safe to emit into.

    Anything that is not a usable collector becomes the no-op, so a bad
    argument degrades observability rather than breaking an execution.
    """
    if collector is None:
        return NO_OP
    if hasattr(collector, "emit") and hasattr(collector, "next_sequence"):
        return collector
    return NO_OP


class Tracer:
    """Binds a collector to one execution so instrumentation stays one line.

    Exists to keep the footprint inside frozen M4 code small: a production path
    writes `tracer.mark(EVENT, ...)` rather than repeating the execution and
    task ids at every call site. It holds no state that execution depends on,
    and every method is best-effort.
    """

    __slots__ = ("collector", "execution_id", "task_id")

    def __init__(self, collector: Any, execution_id: str,
                 task_id: str = "") -> None:
        self.collector = resolve(collector)
        self.execution_id = execution_id or ""
        self.task_id = task_id or ""

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.collector, "enabled", False))

    def mark(self, event_type: str, **fields) -> None:
        if not self.enabled:
            return
        fields.setdefault("task_id", self.task_id)
        emit(self.collector, event_type, self.execution_id, **fields)

    def terminal(self, execution_status: str, **fields) -> None:
        """Emit the terminal event for a frozen M4 status, if one maps.

        The parameter is deliberately NOT called `status`: callers also pass
        the event-level `status` (OK/ERROR/PENDING), and the two are different
        vocabularies that must not collide.

        An unmapped status emits nothing rather than guessing: a trace that
        invents a terminal fact is worse than a trace that lacks one.
        """
        event_type = E.TERMINAL_STATUS_EVENTS.get(execution_status)
        if event_type is not None:
            self.mark(event_type, **fields)


def _inside_ai_dir(path: Path, project_root: Path) -> bool:
    try:
        ai_dir = (project_root / ".ai").resolve()
        candidate = path.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return candidate == ai_dir or ai_dir in candidate.parents


def emit(collector: Any, event_type: str, execution_id: str, **fields) -> None:
    """Build and emit one event. Never raises into a production path.

    The single entry point production code uses, so sequence assignment,
    sanitization, and the best-effort guarantee are applied uniformly.
    """
    target = resolve(collector)
    if not getattr(target, "enabled", False):
        return
    try:
        sequence = target.next_sequence(execution_id)
        event = M.TraceEvent(
            event_id=f"{execution_id}:{sequence}",
            execution_id=execution_id, event_type=event_type,
            sequence=sequence, **fields)
        target.emit(event)
    except Exception:  # noqa: BLE001 - observability must not alter execution
        recorder = getattr(target, "_record_failure", None)
        if callable(recorder):
            try:
                recorder(RuntimeError(f"failed to emit {event_type}"))
            except Exception:  # noqa: BLE001
                pass
