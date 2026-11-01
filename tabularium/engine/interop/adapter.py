"""WorkerAdapter-compatible interop transport seam (M6.1)."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Protocol, runtime_checkable

from tabularium.engine.observability import Tracer
from tabularium.engine.observability import events as EV
from tabularium.engine.workers.adapter import check_acceptance
from tabularium.engine.workers.model import (API_MODEL, COMPLETED, FAILED, REJECTED,
                                   STRUCTURED_OUTPUT, WorkerDescriptor,
                                   WorkerRequest, WorkerResult)

from .mapping import deserialize_result, request_from_worker
from .model import (CAPABILITY_NOT_TRUSTED, DUPLICATE_RESULT,
                    MALFORMED_RESPONSE, PROTOCOL_ERROR, REMOTE_COMPLETED,
                    REMOTE_FAILED, REMOTE_FAILED_STATUS,
                    REMOTE_STATUS_NOT_COMPLETED, TIMEOUT, UNAVAILABLE,
                    WRONG_EXECUTION_ID, WRONG_TASK_ID, InteropError,
                    InteropRequest, InteropResult, RemoteCapabilityView,
                    RemoteIdentity)


class TransportFailure(Exception):
    """Explicit transport failure; no retry is implied."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail[:300]


@runtime_checkable
class InteropTransport(Protocol):
    def send(self, request: InteropRequest) -> Any:
        """Send one request; return an InteropResult or plain mapping."""

    def cancel(self, interop_request_id: str) -> bool:
        """Request cancellation, if the transport supports it."""


class InteropAdapter:
    """A Tabularium WorkerAdapter whose implementation happens to cross a boundary.

    The adapter owns normalization and correlation checks. It never calls Tabularium
    tools, persistence, continuation, or approval code, and never maps remote
    claims into one of those authority types.
    """

    def __init__(self, transport: InteropTransport,
                 remote_identity: RemoteIdentity,
                 *, worker_id: str = "interop:remote",
                 category: str = API_MODEL,
                 capabilities: tuple[str, ...] = (STRUCTURED_OUTPUT,),
                 remote_capabilities: tuple[str, ...] = (),
                 trusted_capabilities: tuple[str, ...] = (),
                 required_remote_capability: str | None = None,
                 timeout_s: float | None = None,
                 collector: Any = None) -> None:
        self.transport = transport
        self.remote_identity = remote_identity
        self.remote_capability_view = RemoteCapabilityView(
            claimed=tuple(remote_capabilities),
            trusted=tuple(trusted_capabilities))
        self.required_remote_capability = required_remote_capability
        self.timeout_s = timeout_s
        self.collector = collector
        self._descriptor = WorkerDescriptor(worker_id=worker_id,
                                            category=category,
                                            capabilities=capabilities)
        self._seen_results: set[str] = set()

    @property
    def descriptor(self) -> WorkerDescriptor:
        return self._descriptor

    @property
    def usable_remote_capabilities(self) -> tuple[str, ...]:
        return self.remote_capability_view.usable

    def accepts(self, request: WorkerRequest) -> tuple[bool, tuple[str, ...]]:
        accepted, codes = check_acceptance(self._descriptor, request)
        reasons = list(codes)
        required = self.required_remote_capability
        if required and required not in self.usable_remote_capabilities:
            reasons.append(f"{CAPABILITY_NOT_TRUSTED}:{required}")
        return not reasons, tuple(reasons)

    def execute(self, request: WorkerRequest) -> WorkerResult:
        accepted, codes = self.accepts(request)
        if not accepted:
            return self._failure(request, REJECTED, codes,
                                 "request refused before remote execution")

        interop_request_id = request.request_id + ":interop"
        try:
            envelope = request_from_worker(
                request, self.remote_identity, interop_request_id,
                requested_capability=self.required_remote_capability,
                timeout_s=self.timeout_s)
        except (TypeError, ValueError) as exc:
            return self._failure(request, FAILED, (MALFORMED_RESPONSE,),
                                 str(exc))

        tracer = Tracer(self.collector, envelope.execution_id, envelope.task_id)
        started = time.monotonic()
        tracer.mark(EV.INTEROP_REQUESTED, component=EV.INTEROP,
                    attributes=self._trace_attributes(envelope))
        try:
            raw = self.transport.send(envelope)
            result = raw if isinstance(raw, InteropResult) else deserialize_result(raw)
            self._check_correlation(envelope, result)
            fingerprint = self._fingerprint(result)
            if fingerprint in self._seen_results:
                return self._transport_failure(
                    request, envelope, tracer, started, DUPLICATE_RESULT,
                    "duplicate remote result ignored")
            self._seen_results.add(fingerprint)
        except TransportFailure as exc:
            return self._transport_failure(request, envelope, tracer, started,
                                           exc.code, exc.detail)
        except TimeoutError as exc:
            return self._transport_failure(request, envelope, tracer, started,
                                           TIMEOUT, str(exc) or "remote timeout")
        except (ConnectionError, OSError) as exc:
            return self._transport_failure(request, envelope, tracer, started,
                                           UNAVAILABLE, str(exc))
        except (TypeError, ValueError, KeyError) as exc:
            return self._transport_failure(request, envelope, tracer, started,
                                           MALFORMED_RESPONSE, str(exc))
        except BaseException as exc:  # noqa: BLE001 - transport boundary
            return self._transport_failure(request, envelope, tracer, started,
                                           PROTOCOL_ERROR, type(exc).__name__)

        if result.status != REMOTE_COMPLETED:
            code = REMOTE_FAILED if result.status == REMOTE_FAILED_STATUS else REMOTE_STATUS_NOT_COMPLETED
            detail = result.error.detail if result.error else result.status
            return self._transport_failure(request, envelope, tracer, started,
                                           code, detail)

        worker_result = WorkerResult(
            request_id=request.request_id,
            worker_id=self._descriptor.worker_id,
            status=COMPLETED,
            output=result.output,
            artifacts=tuple(a.to_dict() for a in result.artifacts),
            usage={"remote_payload_chars": envelope.payload_chars,
                   "remote_payload_tokens": envelope.payload_tokens,
                   "remote_task_status": result.remote_task_status,
                   "remote_endpoint": self.remote_identity.endpoint},
            limitations=("remote output is untrusted and is not authority, "
                         "project truth, a Decision, or Evidence",),
            diagnostics=({"code": "INTEROP_TRANSPORT",
                          "detail": "remote result normalized by Tabularium"},))
        tracer.mark(EV.INTEROP_COMPLETED, component=EV.INTEROP,
                    duration_ms=_elapsed_ms(started),
                    attributes={**self._trace_attributes(envelope),
                                "status": result.status})
        return worker_result

    def cancel(self, request_id: str) -> bool:
        try:
            return bool(self.transport.cancel(request_id + ":interop"))
        except Exception:  # noqa: BLE001 - cancellation is best effort
            return False

    def _check_correlation(self, request: InteropRequest,
                           result: InteropResult) -> None:
        if result.interop_request_id != request.interop_request_id:
            raise TransportFailure(PROTOCOL_ERROR, "interop request id mismatch")
        if result.execution_id != request.execution_id:
            raise TransportFailure(WRONG_EXECUTION_ID,
                                   "remote result execution id mismatch")
        if result.task_id != request.task_id:
            raise TransportFailure(WRONG_TASK_ID,
                                   "remote result task id mismatch")

    def _fingerprint(self, result: InteropResult) -> str:
        body = json.dumps(result.to_dict(), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def _trace_attributes(self, request: InteropRequest) -> dict[str, Any]:
        return {"remote_endpoint": self.remote_identity.endpoint,
                "remote_agent_id": self.remote_identity.agent_id,
                "remote_worker": self.remote_identity.worker,
                "payload_chars": request.payload_chars,
                "payload_tokens": request.payload_tokens,
                "requested_capability": request.requested_capability or ""}

    def _transport_failure(self, request: WorkerRequest,
                           envelope: InteropRequest, tracer: Tracer,
                           started: float, code: str,
                           detail: str) -> WorkerResult:
        tracer.mark(EV.INTEROP_FAILED, component=EV.INTEROP,
                    status=EV.ERROR, duration_ms=_elapsed_ms(started),
                    reason_codes=(code,),
                    attributes={**self._trace_attributes(envelope),
                                "status": "FAILED"})
        return self._failure(request, FAILED, (code,), detail)

    def _failure(self, request: WorkerRequest, status: str,
                 codes: tuple[str, ...], detail: str) -> WorkerResult:
        return WorkerResult(
            request_id=request.request_id,
            worker_id=self._descriptor.worker_id,
            status=status,
            limitations=("remote transport did not produce an accepted result",),
            diagnostics=tuple({"code": code, "detail": detail[:300]}
                              for code in codes))


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
