"""LangGraph production runtime (M4.1).

THIS IS THE ONLY MODULE THAT KNOWS LANGGRAPH EXISTS.

Callers use `start` / `resume` / `inspect_execution` with Tabularium objects and get
Tabularium objects back. They never construct a `StateGraph`, a `Command`, an
`interrupt`, or a checkpointer, and no framework type appears in any returned
value.

LangGraph owns scheduling, suspend/resume mechanics, and checkpoint storage.
Every governance decision lives in `steps.py`, which imports no framework.
Swapping runtimes later means rewriting this file and nothing else.

LangGraph is imported LAZILY, on first use, so the M2 loader, projection,
skills, and the workers keep working when the runtime dependency is absent.

Dependency: tabularium/engine/requirements-runtime.txt (never tabularium/engine/requirements.txt).
Selected in M3.7.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

import sqlite3
import traceback

from . import model as M
from . import steps as S

_CHECKPOINTER_FRAME_MARKERS = (
    "langgraph/checkpoint",
    "langgraph_checkpoint",
    "aiosqlite",
    "sqlite_saver",
)


def _is_checkpoint_fault(exc: BaseException) -> bool:
    """A sqlite fault is Tabularium's checkpoint only if the checkpointer raised it.

    A worker touching its own database must not be reported as a corrupted
    checkpoint: that sends the operator to delete a healthy file.
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return any(
        any(marker in frame.filename.replace("\\", "/")
            for marker in _CHECKPOINTER_FRAME_MARKERS)
        for frame in traceback.extract_tb(exc.__traceback__)
    )

# The single state key. LangGraph state is a plain dict carrying one
# serialized ExecutionState, so nothing framework-shaped reaches Tabularium code.
STATE_KEY = "tabularium"


def _load_langgraph():
    """Import LangGraph on demand, or raise a clear dependency error."""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(
            "the governed runtime requires LangGraph; install the Tabularium "
            "'runtime' extra (pyproject optional-dependencies). Every other "
            "Tabularium path works without it.") from exc
    return SqliteSaver, StateGraph, START, END, Command, interrupt


def _build_graph(adapter_provider: Callable[[], Any]):
    """The production graph.

        START -> validate -> execute_worker -> approval_boundary
              -> (interrupt when required) -> complete -> END

    `adapter_provider` is a callable resolved at execution time rather than a
    bound adapter, so a resumed execution is not permanently coupled to the
    worker instance that started it.
    """
    _, StateGraph, START, END, _, interrupt = _load_langgraph()

    def _state(payload: dict) -> M.ExecutionState:
        return M.ExecutionState.from_dict(payload.get(STATE_KEY) or {})

    def _emit(state: M.ExecutionState) -> dict:
        return {STATE_KEY: state.to_dict()}

    def validate_node(payload: dict) -> dict:
        return _emit(S.validate_input(_state(payload)))

    def execute_node(payload: dict) -> dict:
        state = _state(payload)
        if state.is_terminal:
            return _emit(state)
        return _emit(S.execute_worker(state, adapter_provider()))

    def approval_node(payload: dict) -> dict:
        """Record the approval boundary and COMMIT it.

        Deliberately separate from the node that suspends: `interrupt()`
        discards the return value of the node that raises it, so interrupting
        here would lose the approval request and leave a checkpoint claiming
        AWAITING_APPROVAL with nothing actually pending. The request must be
        durable BEFORE execution suspends.
        """
        state = _state(payload)
        if state.is_terminal or not state.approval_required:
            return _emit(state)
        return _emit(S.approval_boundary(state))

    def wait_node(payload: dict) -> dict:
        state = _state(payload)
        if state.runtime_status != M.AWAITING_APPROVAL:
            return _emit(state)

        # The runtime suspends here. Whatever the caller later supplies is
        # returned by `interrupt`; TABULARIUM decides what it MEANS.
        raw = interrupt(dict(state.approval_request or {}))
        return _emit(S.resolve_approval(state, raw))

    def complete_node(payload: dict) -> dict:
        state = _state(payload)
        if state.is_terminal:
            return _emit(state)
        return _emit(S.complete(state))

    builder = StateGraph(dict)
    builder.add_node("validate", validate_node)
    builder.add_node("execute", execute_node)
    builder.add_node("approval", approval_node)
    builder.add_node("wait", wait_node)
    builder.add_node("complete", complete_node)
    builder.add_edge(START, "validate")
    builder.add_edge("validate", "execute")
    builder.add_edge("execute", "approval")
    builder.add_edge("approval", "wait")
    builder.add_edge("wait", "complete")
    builder.add_edge("complete", END)
    return builder


class LangGraphRuntime:
    """The Tabularium-facing runtime.

    `checkpoint_path` is supplied by the caller and is the ONLY location this
    runtime writes to. It is operational state, never project truth, and it
    must not live inside a project's `.ai/` tree.
    """

    def __init__(self, checkpoint_path: str,
                 adapter_provider: Callable[[], Any] | None = None) -> None:
        if not checkpoint_path or not str(checkpoint_path).strip():
            raise ValueError(
                "checkpoint_path is required: the runtime never chooses a "
                "storage location for the caller")
        self.checkpoint_path = str(checkpoint_path)
        self._adapter_provider = adapter_provider

    # -- public API ---------------------------------------------------------

    def start(self, execution_id: str, request: Any,
              approval_required: bool = False,
              adapter: Any = None) -> M.RuntimeResult:
        """Begin an execution from an ALREADY PREPARED WorkerRequest.

        The runtime does not build governed context: it neither runs the M2
        loader, nor reads `.ai/`, nor projects, nor selects a skill, worker, or
        model. Everything upstream of this call is the control plane's job.

        `execution_id` is caller-provided; no identity is generated here, and
        it is not a Task, project, worker, or approval identity.
        """
        if not execution_id or not str(execution_id).strip():
            return self._refuse("", M.INVALID_RUNTIME_INPUT,
                                "execution_id is required and is never generated")

        state = _state_from_request(execution_id, request, approval_required)
        if state is None:
            return self._refuse(execution_id, M.INVALID_RUNTIME_INPUT,
                                "a prepared WorkerRequest is required")
        return self._invoke(execution_id, {STATE_KEY: state.to_dict()}, adapter)

    def resume(self, execution_id: str, resolution: Any,
               adapter: Any = None) -> M.RuntimeResult:
        """Continue a suspended execution with a VALIDATED HUMAN resolution.

        Resuming is not approving. `resolution` must be a
        `HumanApprovalResolution` (or its dict form) carrying who decided,
        what, when, and over what scope, bound to the pending request. It is
        validated by Tabularium-owned logic BEFORE the framework is asked to
        continue, so LangGraph never inspects authority semantics and every
        ambiguous input fails closed.
        """
        current = self.inspect_execution(execution_id)
        if current is None:
            return self._refuse(execution_id, M.EXECUTION_NOT_FOUND,
                                "no checkpoint for this execution id")
        if current.runtime_status != M.AWAITING_APPROVAL:
            return self._refuse(execution_id, M.NOTHING_TO_RESUME,
                                f"execution is {current.runtime_status}")

        # GUARD — LangGraph 1.2.11 defect. `Command(resume=None)` raises an
        # internal UnboundLocalError in `langgraph/pregel/_loop.py`
        # (`resume_is_map` referenced before assignment) rather than resuming
        # or refusing cleanly. It fails closed, but as an opaque traceback, so
        # every invalid resolution is judged by Tabularium BEFORE the framework is
        # reached. Harmless on fixed versions; regression-tested.
        verdict = S.resolve_approval(current, resolution)
        if (verdict.approval_resolution or {}).get("decision") != "APPROVE":
            # Denied, cancelled, or refused: persist the outcome without
            # asking LangGraph to continue at all.
            return self._write_terminal(execution_id, verdict)

        return self._invoke(execution_id, None, adapter,
                            resume_value=verdict.approval_resolution)

    def inspect_execution(self, execution_id: str) -> M.ExecutionState | None:
        """Read persisted runtime state. Never mutates the checkpoint."""
        SqliteSaver, *_ = _load_langgraph()
        try:
            with SqliteSaver.from_conn_string(self.checkpoint_path) as saver:
                graph = _build_graph(self._resolve_adapter).compile(
                    checkpointer=saver)
                snapshot = graph.get_state(self._config(execution_id))
        except Exception:  # noqa: BLE001 - inspection never raises for callers
            return None
        values = getattr(snapshot, "values", None) or {}
        stored = values.get(STATE_KEY)
        if not stored:
            return None
        state = M.ExecutionState.from_dict(stored)
        # A pending interrupt means the execution is suspended, whatever the
        # last written status said.
        if getattr(snapshot, "interrupts", ()) and not state.is_terminal:
            state = replace(state, runtime_status=M.AWAITING_APPROVAL)
        return state

    def cancel(self, execution_id: str) -> M.RuntimeResult:
        """Mark a non-terminal execution CANCELLED.

        This is runtime bookkeeping, not transport cancellation: an in-flight
        worker process is not interrupted, which stays transport-specific.
        """
        current = self.inspect_execution(execution_id)
        if current is None:
            return self._refuse(execution_id, M.EXECUTION_NOT_FOUND,
                                "no checkpoint for this execution id")
        return self._write_terminal(execution_id, S.cancel(current))

    # -- internals ----------------------------------------------------------

    def _resolve_adapter(self):
        return self._adapter_provider() if self._adapter_provider else None

    def _config(self, execution_id: str) -> dict:
        return {"configurable": {"thread_id": execution_id}}

    def _invoke(self, execution_id: str, payload: dict | None, adapter: Any,
                resume_value: Any = None) -> M.RuntimeResult:
        SqliteSaver, _, _, _, Command, _ = _load_langgraph()
        provider = (lambda: adapter) if adapter is not None \
            else self._resolve_adapter
        try:
            with SqliteSaver.from_conn_string(self.checkpoint_path) as saver:
                graph = _build_graph(provider).compile(checkpointer=saver)
                message = payload if resume_value is None \
                    else Command(resume=resume_value)
                try:
                    raw = graph.invoke(message,
                                       config=self._config(execution_id))
                except Exception as exc:  # noqa: BLE001 - Tabularium outcome
                    if _is_checkpoint_fault(exc):
                        return self._refuse(execution_id, M.CHECKPOINT_ERROR,
                                            _sanitize(exc))
                    return self._refuse(execution_id, M.RUNTIME_ERROR,
                                        _sanitize(exc))
        except ImportError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Distinguished from RUNTIME_ERROR on purpose: an unusable
            # checkpoint store is an operator/configuration problem, not a
            # failure of the execution itself.
            return self._refuse(execution_id, M.CHECKPOINT_ERROR,
                                _sanitize(exc))

        state = M.ExecutionState.from_dict(raw.get(STATE_KEY) or {})
        if raw.get("__interrupt__") and not state.is_terminal:
            state = replace(state, runtime_status=M.AWAITING_APPROVAL)
        return M.result_from_state(state)

    def _write_terminal(self, execution_id: str,
                        state: M.ExecutionState) -> M.RuntimeResult:
        """Persist a terminal outcome without continuing the graph.

        Used when Tabularium refuses a resolution: the framework is never asked to
        continue, so no defect path and no accidental completion is possible.
        """
        SqliteSaver, *_ = _load_langgraph()
        try:
            with SqliteSaver.from_conn_string(self.checkpoint_path) as saver:
                graph = _build_graph(self._resolve_adapter).compile(
                    checkpointer=saver)
                graph.update_state(self._config(execution_id),
                                   {STATE_KEY: state.to_dict()})
        except Exception as exc:  # noqa: BLE001
            return M.result_from_state(
                state.with_diagnostic(M.CHECKPOINT_ERROR, _sanitize(exc)))
        return M.result_from_state(state)

    def _refuse(self, execution_id: str, code: str,
                detail: str) -> M.RuntimeResult:
        return M.RuntimeResult(execution_id=execution_id,
                               runtime_status=M.FAILED,
                               reason_codes=(code,),
                               diagnostics=({"code": code, "detail": detail},))


def _state_from_request(execution_id: str, request: Any,
                        approval_required: bool) -> M.ExecutionState | None:
    """Build runtime state from a prepared WorkerRequest.

    The payload is carried by reference and never rewritten, so what the
    worker sees is exactly what the projector produced.
    """
    for attribute in ("request_id", "intent", "payload"):
        if not hasattr(request, attribute):
            return None
    return M.ExecutionState(
        execution_id=execution_id,
        request_id=request.request_id,
        task_id=getattr(request, "task_id", "") or "",
        intent=request.intent,
        payload=request.payload,
        required_category=getattr(request, "required_category", None),
        required_capabilities=tuple(
            getattr(request, "required_capabilities", ()) or ()),
        governance=dict(getattr(request, "governance", {}) or {}),
        approval_required=bool(approval_required))


def _sanitize(exc: BaseException, limit: int = 200) -> str:
    """Bounded, secret-scrubbed detail. Framework prose is never a reason code."""
    hints = ("sk-", "api_key", "api-key", "authorization", "bearer ", "token=",
             "secret", "password")
    text = " ".join(str(exc).split())
    if any(hint in text.lower() for hint in hints):
        return f"{type(exc).__name__} (detail withheld)"
    return f"{type(exc).__name__}: {text}"[:limit]
