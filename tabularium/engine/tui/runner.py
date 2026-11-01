"""Background governed runs with a live event view (Faz C, DEC-2026-0009 A2).

A run executes the SAME `run_instruction` entrypoint the CLI uses — worker
selection policy unchanged, no routing, no fallback — inside a worker thread
while the dashboard polls an append-only JSONL trace plus the read-only
checkpoint inspect. Telemetry observes only and can never change what the
pipeline decides (M5.1 contract).

Stop means detach: threads are never killed. Detaching stops the view; the
execution continues to a checkpointable boundary and stays resumable via
`continue`. The UI must say so, and it does.
"""

from __future__ import annotations

import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tabularium.storage import (CHECKPOINT_DIR_UNSAFE, CheckpointDirUnsafe,
                          prepare_trace_storage)


def next_action_for(status: str | None) -> str:
    """Map a finished background run to its UI follow-up. Pure."""
    if status == "AWAITING_APPROVAL":
        return "approval-sheet"
    if status == "COMPLETED":
        return "offer-persist"
    return "done"


def summarize_trace(trace: Any, limit: int = 12) -> list[str]:
    """One bounded line per recent event. Pure presentation."""
    events = list(getattr(trace, "events", ()) or ())
    lines: list[str] = []
    for event in events[-limit:]:
        reason = ",".join(getattr(event, "reason_codes", ()) or ())
        suffix = f" [{reason}]" if reason else ""
        lines.append(
            f"#{getattr(event, 'sequence', '?')} "
            f"{getattr(event, 'event_type', '?')} "
            f"({getattr(event, 'component', '?')}){suffix}")
    return lines if lines else ["(no events yet)"]


@dataclass
class RunHandle:
    target: str
    instruction: str
    trace_path: str
    detached: bool = False
    _thread: Any = field(default=None, repr=False)
    _collector: Any = field(default=None, repr=False)
    _result: Any = field(default=None, repr=False)
    _done: Any = field(default_factory=threading.Event, repr=False)

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    @property
    def result(self) -> Any:
        return self._result


def default_trace_path() -> Path:
    root = Path(tempfile.gettempdir()) / "tabularium-os" / "traces"
    stamp = int(time.time() * 1000)
    return root / f"run-{stamp}.jsonl"


def start_run(raw_target, instruction: str, *, task_id: str | None = None,
              worker: str | None = None, model: str | None = None,
              adapter: Any = None, trace_path: str | Path | None = None,
              workflow: str | None = None,
              ) -> RunHandle:
    """Start one governed run in a background thread. Returns immediately."""
    from tabularium.execution import run_instruction
    from tabularium.engine.observability import JsonlCollector

    target = str(raw_target or ".")
    path = Path(trace_path) if trace_path is not None else default_trace_path()
    prepare_trace_storage(path)
    collector = JsonlCollector(path, project_root=target)
    handle = RunHandle(target=target, instruction=instruction,
                       trace_path=str(path), _collector=collector)

    def _execute() -> None:
        try:
            handle._result = run_instruction(
                instruction, target, task_id=task_id, worker=worker,
                model=model, adapter=adapter, collector=collector,
                workflow=workflow)
        except Exception as exc:  # noqa: BLE001 - record, never propagate
            handle._result = exc
        finally:
            handle._done.set()

    thread = threading.Thread(target=_execute, name="tabu-tui-run",
                              daemon=True)
    handle._thread = thread
    thread.start()
    return handle


def detach(handle: RunHandle) -> None:
    """Stop watching a run. The execution continues; checkpoints persist."""
    handle.detached = True


def wait(handle: RunHandle, timeout: float | None = None) -> Any:
    """Block until the run finishes; returns its result (or exception)."""
    handle._done.wait(timeout)
    return handle._result


def read_events(handle: RunHandle) -> list[str]:
    """Current live event lines for a run. Never raises into the UI."""
    try:
        trace = handle._collector.read()
    except Exception:  # noqa: BLE001 - BEST_EFFORT telemetry
        return ["(trace unavailable)"]
    return summarize_trace(trace)
