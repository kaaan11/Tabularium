"""Worker vocabulary: category, capability, request, result (M3.3).

Six terms are kept distinct throughout this package, and collapsing any two
of them is the failure this module exists to prevent:

    Skill     how a class of work should be performed        (M3.4)
    Worker    an execution environment that performs work    (here)
    Provider  a service/access path to models                (M3.6, adapters)
    Model     the selected intelligence, an attribute of a request
    Runtime   workflow/checkpoint/interrupt sequencing       (deferred)
    Tool      external action capability                     (deferred)

A HUMAN is NOT a worker category. A worker is an execution environment; a
human is an authority and approval actor. An approval interrupt suspends
execution and routes a decision to the human authority — that is a governance
transition, not a work item dispatched to an executor. Putting it in this
enum would place authority inside the execution abstraction.

No vendor, product, or provider name appears in this module. Codex CLI,
Claude Code, and any API provider belong to concrete adapters built later.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- worker category --------------------------------------------------------
#
# What KIND of execution environment performs the work. Deliberately coarse:
# this is not a product list and must not grow one entry per tool.

CLI_CODING = "CLI_CODING"   # a coding CLI driven as a subprocess or over ACP
API_MODEL = "API_MODEL"     # a model reached through a provider API
LOCAL = "LOCAL"             # a local process or locally hosted model

WORKER_CATEGORIES = (CLI_CODING, API_MODEL, LOCAL)


# --- worker capability ------------------------------------------------------
#
# What a worker CAN DO, mechanically. These are declared mechanics, never
# quality claims: "this worker can write files" is observable, "this worker is
# better at Python" is a capability claim that requires recorded evidence.
# Nothing here ranks a worker.

WRITES_FILES = "WRITES_FILES"
READS_FILES = "READS_FILES"
RUNS_COMMANDS = "RUNS_COMMANDS"
NETWORK_ACCESS = "NETWORK_ACCESS"
STREAMS_OUTPUT = "STREAMS_OUTPUT"
CANCELLABLE = "CANCELLABLE"
STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"

WORKER_CAPABILITIES = (WRITES_FILES, READS_FILES, RUNS_COMMANDS,
                       NETWORK_ACCESS, STREAMS_OUTPUT, CANCELLABLE,
                       STRUCTURED_OUTPUT)


# --- worker result status ---------------------------------------------------

COMPLETED = "COMPLETED"
FAILED = "FAILED"
REJECTED = "REJECTED"      # the worker declined the request before executing
CANCELLED = "CANCELLED"    # execution stopped before producing a full result

RESULT_STATUSES = (COMPLETED, FAILED, REJECTED, CANCELLED)


# --- request rejection codes ------------------------------------------------

UNSUPPORTED_CATEGORY = "UNSUPPORTED_CATEGORY"
MISSING_CAPABILITY = "MISSING_CAPABILITY"
EMPTY_PAYLOAD = "EMPTY_PAYLOAD"


@dataclass(frozen=True)
class WorkerDescriptor:
    """Identity and declared mechanics of one worker.

    `capabilities` states what the worker can mechanically do. It carries no
    claim about how well it does anything, and selection policy does not live
    here — choosing a worker is Tabularium governance, not a worker property.
    """

    worker_id: str
    category: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.category not in WORKER_CATEGORIES:
            raise ValueError(
                f"unknown worker category {self.category!r}; "
                f"expected one of {', '.join(WORKER_CATEGORIES)}")
        unknown = sorted(set(self.capabilities) - set(WORKER_CAPABILITIES))
        if unknown:
            raise ValueError(f"unknown worker capabilities: {', '.join(unknown)}")

    def supports(self, *required: str) -> bool:
        return set(required).issubset(self.capabilities)


@dataclass(frozen=True)
class WorkerRequest:
    """One bounded unit of work handed across the worker boundary.

    The request carries a PROJECTED PAYLOAD, never a project root and never a
    whole Context Package. A worker cannot widen its own context: everything
    it may see about the project is already inside `payload`, which the
    projector produced under a budget.

    Deliberately absent, and not to be added casually: credentials, provider
    configuration, a filesystem root, persistent memory, chat history, and a
    tool loop. Credentials in particular never travel in a request — they are
    an adapter/environment concern (immutable principle 6).

    `model_requirements` expresses what the work NEEDS (for example a minimum
    context window), not which model to use. Naming a model here would
    collapse Worker into Model.
    """

    request_id: str                      # caller-provided; never generated here
    intent: str                          # what this execution is for
    payload: Any                         # ProjectedPayload from the projector
    task_id: str | None = None
    required_category: str | None = None
    required_capabilities: tuple[str, ...] = ()
    model_requirements: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    governance: dict[str, Any] = field(default_factory=dict)
    correlation: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.required_category is not None and \
                self.required_category not in WORKER_CATEGORIES:
            raise ValueError(
                f"unknown required worker category {self.required_category!r}")
        if not self.request_id:
            # Correlation identity is the caller's, so that a request is
            # reproducible and projection stays free of generated ids.
            raise ValueError("request_id must be provided by the caller")


@dataclass(frozen=True)
class WorkerResult:
    """Normalized outcome of one worker execution.

    A WorkerResult is NOT Evidence, NOT a Decision, NOT a Report, and NOT an
    approval. It records what a worker returned. Whether any of it acquires
    durable value is decided later by the persistence gate, by governance —
    never by the worker and never by this type.

    Normalization is Tabularium's: every worker returns this shape regardless of how
    its underlying transport reports success, failure, or usage.
    """

    request_id: str
    worker_id: str
    status: str
    output: str = ""
    artifacts: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValueError(
                f"unknown worker result status {self.status!r}; "
                f"expected one of {', '.join(RESULT_STATUSES)}")

    @property
    def succeeded(self) -> bool:
        return self.status == COMPLETED
