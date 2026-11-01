"""Worker abstraction (M3.3).

The boundary between Tabularium governance and future execution environments. This
package defines what a worker is, what may be handed to one, and what comes
back. It executes nothing real: the only worker here is a deterministic mock
that proves the interface holds without provider coupling.

Tabularium owns: whether work may proceed, what context a worker may see, which
worker is selected, and what happens to a result.
A worker owns: performing the bounded work it was handed.

This package must not import `tabularium.engine.context_loader`. Workers receive a
projected payload, never a Context Package and never a project root.
"""

from .adapter import WorkerAdapter, check_acceptance
from .mock import MockWorker
from .model import (API_MODEL, CANCELLED, CLI_CODING, COMPLETED, FAILED,
                    LOCAL, REJECTED, RESULT_STATUSES, WORKER_CAPABILITIES,
                    WORKER_CATEGORIES, WorkerDescriptor, WorkerRequest,
                    WorkerResult)

__all__ = ["WorkerAdapter", "check_acceptance", "MockWorker",
           "WorkerDescriptor", "WorkerRequest", "WorkerResult",
           "WORKER_CATEGORIES", "WORKER_CAPABILITIES", "RESULT_STATUSES",
           "CLI_CODING", "API_MODEL", "LOCAL",
           "COMPLETED", "FAILED", "REJECTED", "CANCELLED"]
