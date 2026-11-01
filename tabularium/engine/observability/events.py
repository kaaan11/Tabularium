"""The closed event taxonomy (M5.1).

Every name here corresponds to a boundary that ACTUALLY EXISTS in the frozen
M4 implementation. Nothing was invented from memory: the pipeline stages come
from `tabularium/engine/execution/pipeline.py`, the runtime nodes from
`tabularium/engine/runtime/langgraph_runtime.py`, the tool cycle from
`tabularium/engine/tools/adapter.py`, and the continuation/persistence outcomes from
`tabularium/engine/continuation/flow.py` and `tabularium/engine/persistence/writer.py`.

TWO NAMING RULES, both about not lying:

1. `*_REQUESTED` / `*_STARTED` describe an ATTEMPT. `*_COMPLETED`, `*_WRITTEN`,
   `*_RESOLVED`, and `*_EXECUTED` describe a CONFIRMED OUTCOME and may only be
   emitted after the authoritative subsystem has already reported success.
   There are deliberately no optimistic events that could later become false.

2. No synonyms. One boundary, one name. `EXECUTION_REJECTED` is a human
   rejecting the work; `EXECUTION_FAILED` is the machinery breaking; they are
   different facts and never collapse.
"""

from __future__ import annotations

# --- preparation (M4.2 pipeline, before any worker) -------------------------

EXECUTION_STARTED = "EXECUTION_STARTED"
CONTEXT_LOADED = "CONTEXT_LOADED"              # M2 loader returned a package
CONTEXT_GOVERNED = "CONTEXT_GOVERNED"          # readiness/actionability gates
SKILL_APPLIED = "SKILL_APPLIED"                # effective constraints resolved
CONTEXT_PROJECTED = "CONTEXT_PROJECTED"        # budgeted payload built
WORKER_PREPARED = "WORKER_PREPARED"            # WorkerRequest constructed
PREPARATION_REJECTED = "PREPARATION_REJECTED"  # never reached a worker

# --- execution (M4.1 runtime) -----------------------------------------------

WORKER_STARTED = "WORKER_STARTED"
WORKER_COMPLETED = "WORKER_COMPLETED"
WORKER_FAILED = "WORKER_FAILED"

# --- human approval (M4.3) --------------------------------------------------

APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
APPROVAL_RESOLVED = "APPROVAL_RESOLVED"

# --- governed tools (M4.6) --------------------------------------------------

TOOL_REQUESTED = "TOOL_REQUESTED"
TOOL_AUTHORIZATION_REQUIRED = "TOOL_AUTHORIZATION_REQUIRED"
TOOL_DENIED = "TOOL_DENIED"
TOOL_EXECUTED = "TOOL_EXECUTED"
TOOL_FAILED = "TOOL_FAILED"

# --- durable value (M4.5 continuation + M4.4 persistence) -------------------

CONTINUATION_PLANNED = "CONTINUATION_PLANNED"
PERSISTENCE_PROPOSED = "PERSISTENCE_PROPOSED"
PERSISTENCE_AUTHORIZED = "PERSISTENCE_AUTHORIZED"
PERSISTENCE_WRITTEN = "PERSISTENCE_WRITTEN"
PERSISTENCE_DENIED = "PERSISTENCE_DENIED"
PERSISTENCE_FAILED = "PERSISTENCE_FAILED"

# --- interoperability transport (M6.1) ------------------------------------

INTEROP_REQUESTED = "INTEROP_REQUESTED"
INTEROP_COMPLETED = "INTEROP_COMPLETED"
INTEROP_FAILED = "INTEROP_FAILED"

# --- optional memory pilot (M6.2) -----------------------------------------

MEMORY_INDEXED = "MEMORY_INDEXED"
MEMORY_QUERY = "MEMORY_QUERY"
MEMORY_RESULT = "MEMORY_RESULT"
MEMORY_FAILED = "MEMORY_FAILED"

# --- remote worker lifecycle (M6.3) ---------------------------------------

REMOTE_WORKER_REQUESTED = "REMOTE_WORKER_REQUESTED"
REMOTE_WORKER_COMPLETED = "REMOTE_WORKER_COMPLETED"
REMOTE_WORKER_FAILED = "REMOTE_WORKER_FAILED"
REMOTE_WORKER_TIMEOUT = "REMOTE_WORKER_TIMEOUT"
REMOTE_WORKER_CANCELLED = "REMOTE_WORKER_CANCELLED"

# --- terminal execution states (mirror the frozen M4 statuses) --------------

EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
EXECUTION_REJECTED = "EXECUTION_REJECTED"
EXECUTION_FAILED = "EXECUTION_FAILED"
EXECUTION_CANCELLED = "EXECUTION_CANCELLED"

EVENT_TYPES = (
    EXECUTION_STARTED, CONTEXT_LOADED, CONTEXT_GOVERNED, SKILL_APPLIED,
    CONTEXT_PROJECTED, WORKER_PREPARED, PREPARATION_REJECTED,
    WORKER_STARTED, WORKER_COMPLETED, WORKER_FAILED,
    APPROVAL_REQUESTED, APPROVAL_RESOLVED,
    TOOL_REQUESTED, TOOL_AUTHORIZATION_REQUIRED, TOOL_DENIED, TOOL_EXECUTED,
    TOOL_FAILED,
    CONTINUATION_PLANNED, PERSISTENCE_PROPOSED, PERSISTENCE_AUTHORIZED,
    PERSISTENCE_WRITTEN, PERSISTENCE_DENIED, PERSISTENCE_FAILED,
    INTEROP_REQUESTED, INTEROP_COMPLETED, INTEROP_FAILED,
    MEMORY_INDEXED, MEMORY_QUERY, MEMORY_RESULT, MEMORY_FAILED,
    REMOTE_WORKER_REQUESTED, REMOTE_WORKER_COMPLETED, REMOTE_WORKER_FAILED,
    REMOTE_WORKER_TIMEOUT, REMOTE_WORKER_CANCELLED,
    EXECUTION_COMPLETED, EXECUTION_REJECTED, EXECUTION_FAILED,
    EXECUTION_CANCELLED,
)

# Events that may only be emitted AFTER the authoritative subsystem confirmed
# the outcome. A test asserts this set is never emitted speculatively.
CONFIRMED_OUTCOME_EVENTS = (
    WORKER_COMPLETED, WORKER_FAILED, APPROVAL_RESOLVED, TOOL_EXECUTED,
    TOOL_FAILED, TOOL_DENIED, PERSISTENCE_WRITTEN, PERSISTENCE_DENIED,
    PERSISTENCE_FAILED, EXECUTION_COMPLETED, EXECUTION_REJECTED,
    EXECUTION_FAILED, EXECUTION_CANCELLED, PREPARATION_REJECTED,
    REMOTE_WORKER_COMPLETED, REMOTE_WORKER_FAILED, REMOTE_WORKER_TIMEOUT,
    REMOTE_WORKER_CANCELLED,
)

TERMINAL_EVENTS = (
    EXECUTION_COMPLETED, EXECUTION_REJECTED, EXECUTION_FAILED,
    EXECUTION_CANCELLED, PREPARATION_REJECTED,
)

# --- components (which subsystem emitted the event) -------------------------

PIPELINE = "pipeline"
LOADER = "context_loader"
PROJECTION = "projection"
SKILLS = "skills"
RUNTIME = "runtime"
WORKER = "worker"
APPROVAL = "approval"
TOOLS = "tools"
CONTINUATION = "continuation"
PERSISTENCE = "persistence"
INTEROP = "interop"
MEMORY = "memory"
REMOTE_WORKER = "remote_worker"

COMPONENTS = (PIPELINE, LOADER, PROJECTION, SKILLS, RUNTIME, WORKER, APPROVAL,
              TOOLS, CONTINUATION, PERSISTENCE, INTEROP, MEMORY,
              REMOTE_WORKER)

# --- event status (descriptive only; never an authority value) --------------

OK = "OK"
ERROR = "ERROR"
PENDING = "PENDING"

EVENT_STATUSES = (OK, ERROR, PENDING)


# Maps a frozen M4 terminal status onto its trace event. The mapping is
# explicit rather than derived, so a new M4 status cannot silently fall through
# to a wrong event.
TERMINAL_STATUS_EVENTS = {
    "COMPLETED": EXECUTION_COMPLETED,
    "REJECTED": EXECUTION_REJECTED,
    "FAILED": EXECUTION_FAILED,
    "CANCELLED": EXECUTION_CANCELLED,
    "PREPARATION_REJECTED": PREPARATION_REJECTED,
}
