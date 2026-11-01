"""A deterministic mock worker (M3.3). TEST AND DEMONSTRATION ONLY.

Its whole purpose is to prove that the WorkerAdapter boundary is usable
without any provider coupling — no network, no filesystem, no model, no
credentials, no vendor SDK. If the boundary needed any of those to be
exercised, the boundary would be wrong.

This worker is not a product and must never be routed real work.
"""

from __future__ import annotations

from .adapter import check_acceptance
from .model import (COMPLETED, REJECTED, STRUCTURED_OUTPUT, WorkerDescriptor,
                    WorkerRequest, WorkerResult)


class MockWorker:
    """Echoes a deterministic summary of what it was handed.

    Deterministic by construction: no clock, no randomness, no generated ids.
    The same request always yields the same result, which is what makes it
    usable in conformance tests.
    """

    def __init__(self, worker_id: str = "mock:echo",
                 category: str = "LOCAL",
                 capabilities: tuple[str, ...] = (STRUCTURED_OUTPUT,)) -> None:
        self._descriptor = WorkerDescriptor(worker_id=worker_id,
                                            category=category,
                                            capabilities=capabilities)
        self._cancelled: set[str] = set()

    @property
    def descriptor(self) -> WorkerDescriptor:
        return self._descriptor

    def accepts(self, request: WorkerRequest) -> tuple[bool, tuple[str, ...]]:
        return check_acceptance(self._descriptor, request)

    def execute(self, request: WorkerRequest) -> WorkerResult:
        accepted, codes = self.accepts(request)
        if not accepted:
            return WorkerResult(
                request_id=request.request_id,
                worker_id=self._descriptor.worker_id,
                status=REJECTED,
                limitations=("request refused before execution",),
                diagnostics=tuple({"code": code} for code in codes))

        # Reads the payload; never writes to it, never reaches past it.
        payload = request.payload
        sections = tuple(payload.section_names()) if hasattr(
            payload, "section_names") else ()
        return WorkerResult(
            request_id=request.request_id,
            worker_id=self._descriptor.worker_id,
            status=COMPLETED,
            output=f"mock executed intent={request.intent!r} "
                   f"task={request.task_id or 'NONE'} "
                   f"sections={','.join(sections)}",
            usage={"payload_sections": len(sections)},
            limitations=("mock worker; no real execution occurred",))

    def cancel(self, request_id: str) -> bool:
        # The boundary exists; real cancellation transport arrives with the
        # concrete adapters in M3.5/M3.6.
        self._cancelled.add(request_id)
        return True
