"""The WorkerAdapter boundary (M3.3).

One small protocol that every future worker implements: a coding CLI driven
as a subprocess, a model reached through a provider API, a local process. The
same interface must serve all three, so it exposes only what Tabularium needs to
hand over bounded work and normalize what comes back.

WHAT IS DELIBERATELY NOT HERE

No planning method, no memory method, no tool-loop method, no chat-history
method, and no provider-specific knobs. Those belong to agent frameworks;
adopting them here would move governance decisions inside the execution
abstraction, which is precisely what the control plane must keep outside.

CANCELLATION

`cancel` marks the boundary only. Real cancellation transport — signalling a
subprocess, aborting an HTTP stream — belongs to the concrete adapters built
in M3.5 and M3.6. It is declared now so that a worker which cannot be
cancelled has to say so through `CANCELLABLE` rather than by silently
ignoring a cancel.

"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .model import (EMPTY_PAYLOAD, MISSING_CAPABILITY, UNSUPPORTED_CATEGORY,
                    WorkerDescriptor, WorkerRequest, WorkerResult)


@runtime_checkable
class WorkerAdapter(Protocol):
    """The whole worker boundary.

    An adapter translates a WorkerRequest into whatever its execution
    environment actually supports, and translates the outcome back into a
    WorkerResult. It never decides whether the work is permitted.
    """

    @property
    def descriptor(self) -> WorkerDescriptor:
        """Identity and declared mechanics of this worker."""

    def accepts(self, request: WorkerRequest) -> tuple[bool, tuple[str, ...]]:
        """Whether this worker can take the request, and why not if it cannot.

        Returns (accepted, reason_codes). A refusal is a structured answer,
        not an exception: "this worker cannot do this" is an ordinary outcome.
        """

    def execute(self, request: WorkerRequest) -> WorkerResult:
        """Perform the work and return a normalized result.

        Must not mutate the request or anything reachable from its payload.
        """

    def cancel(self, request_id: str) -> bool:
        """Request cancellation. Returns whether cancellation was accepted."""


def check_acceptance(descriptor: WorkerDescriptor,
                     request: WorkerRequest) -> tuple[bool, tuple[str, ...]]:
    """Shared acceptance check, so every adapter refuses for the same reasons.

    Deterministic and side-effect free. Reason codes are Tabularium-owned and stable
    across adapters, so a refusal means the same thing whichever worker
    produced it.
    """
    codes: list[str] = []
    if request.required_category is not None and \
            request.required_category != descriptor.category:
        codes.append(UNSUPPORTED_CATEGORY)
    missing = sorted(set(request.required_capabilities)
                     - set(descriptor.capabilities))
    if missing:
        codes.extend(f"{MISSING_CAPABILITY}:{cap}" for cap in missing)
    if request.payload is None:
        codes.append(EMPTY_PAYLOAD)
    return (not codes, tuple(codes))
