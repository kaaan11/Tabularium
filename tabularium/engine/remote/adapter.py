"""WorkerAdapter implementation for the Tabularium remote lifecycle (M6.3)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, replace
from typing import Any

from tabularium.engine.interop.mapping import deserialize_result, request_from_worker
from tabularium.engine.interop.model import InteropResult
from tabularium.engine.observability import Tracer
from tabularium.engine.observability import events as EV
from tabularium.engine.observability.model import contains_secret
from tabularium.engine.workers.adapter import check_acceptance
from tabularium.engine.workers.model import (API_MODEL, CANCELLED as WORKER_CANCELLED,
                                   COMPLETED as WORKER_COMPLETED,
                                   FAILED as WORKER_FAILED,
                                   REJECTED, STRUCTURED_OUTPUT,
                                   WorkerDescriptor, WorkerRequest,
                                   WorkerResult)

from .ledger import (InMemoryRemoteLedger, LedgerRecord, RemoteLedger,
                     RemoteLedgerFailure)
from .model import (CANCELLED, COMPLETED, DUPLICATE_RESULT, FAILED, IN_FLIGHT,
                    IDENTITY_MISMATCH, LATE_RESULT, PAYLOAD_MISMATCH,
                    REQUEST_TERMINAL, REMOTE_CANCEL_CONFIRMED,
                    REMOTE_CANCEL_REQUESTED, REMOTE_CANCEL_UNKNOWN,
                    REMOTE_FAILED, REMOTE_MALFORMED, REMOTE_TIMEOUT,
                    REMOTE_UNAVAILABLE, REQUESTED, TIMEOUT, UNKNOWN,
                    LEDGER_LOCK_FAILED, LEDGER_WRITE_FAILED, LEDGER_CORRUPT,
                    LEDGER_IDENTITY_MISMATCH,
                    RemoteCancellation, RemoteEnvelope, RemoteLifecycleResult,
                    RemoteWorkerConfig, payload_digest)
from .transport import RemoteTransportFailure


class RemoteWorkerAdapter:
    """Adapt one explicit remote target into the existing WorkerAdapter seam."""

    def __init__(self, transport: Any, config: RemoteWorkerConfig, *,
                 ledger: RemoteLedger | None = None,
                 category: str = API_MODEL,
                 capabilities: tuple[str, ...] = (STRUCTURED_OUTPUT,),
                 collector: Any = None) -> None:
        self.transport = transport
        self.config = config
        self.ledger = ledger or InMemoryRemoteLedger()
        self.collector = collector
        self._descriptor = WorkerDescriptor(
            worker_id=config.worker_id, category=category,
            capabilities=capabilities)
        self._active: dict[str, RemoteEnvelope] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancellations: dict[str, RemoteCancellation] = {}
        self._lock = threading.RLock()

    @property
    def descriptor(self) -> WorkerDescriptor:
        return self._descriptor

    def accepts(self, request: WorkerRequest) -> tuple[bool, tuple[str, ...]]:
        accepted, codes = check_acceptance(self._descriptor, request)
        reasons = list(codes)
        required = self.config.required_remote_capability
        if required and required not in self.config.usable_capabilities:
            reasons.append(f"CAPABILITY_NOT_TRUSTED:{required}")
        return not reasons, tuple(reasons)

    def prepare(self, request: WorkerRequest) -> RemoteEnvelope:
        """Build the exact bound envelope without contacting the network."""
        accepted, codes = self.accepts(request)
        if not accepted:
            raise ValueError("remote request refused: " + ",".join(codes))
        interop = request_from_worker(
            request, self.config.identity,
            request.request_id + ":interop",
            requested_capability=self.config.required_remote_capability,
            timeout_s=self.config.timeout_s)
        digest = payload_digest(interop)
        interop = replace(interop, metadata={
            **interop.metadata,
            "worker_id": self.config.worker_id,
            "payload_digest": digest,
        })
        # The metadata addition is outside the projected payload, so the
        # digest remains the digest of the exact worker input.
        return RemoteEnvelope(
            worker_id=self.config.worker_id,
            interop_request=interop,
            payload_digest=digest,
            idempotency_key=interop.interop_request_id)

    def execute(self, request: WorkerRequest) -> WorkerResult:
        return self.execute_lifecycle(request).worker_result

    def execute_lifecycle(self, request: WorkerRequest) -> RemoteLifecycleResult:
        return self._execute_lifecycle(request, allow_in_flight=False)

    def _execute_lifecycle(self, request: WorkerRequest, *,
                           allow_in_flight: bool) -> RemoteLifecycleResult:
        started = time.perf_counter()
        preparation_started = time.perf_counter()
        try:
            envelope = self.prepare(request)
        except (TypeError, ValueError) as exc:
            return self._lifecycle_failure(
                request, "", FAILED, (REMOTE_MALFORMED,), str(exc), started)
        preparation_ms = round((time.perf_counter() - preparation_started) * 1000, 3)

        try:
            existing = self._begin(envelope)
        except RemoteLedgerFailure as exc:
            return self._lifecycle_failure(
                request, envelope, UNKNOWN, (exc.code,), exc.detail, started,
                record_terminal=False)
        if existing is not None:
            if not self._same_identity(existing, envelope):
                return self._lifecycle_failure(
                    request, envelope, FAILED, (IDENTITY_MISMATCH,),
                    "ledger identity does not match request", started)
            if existing.terminal:
                return self._lifecycle_failure(
                    request, envelope, FAILED, (DUPLICATE_RESULT, REQUEST_TERMINAL),
                    "one accepted result per request identity", started,
                    record_terminal=False)
            if not allow_in_flight:
                return self._lifecycle_failure(
                    request, envelope, FAILED, (DUPLICATE_RESULT,),
                    "request is already begun by another lifecycle", started,
                    record_terminal=False)

        cancel_event = threading.Event()
        with self._lock:
            if request.request_id in self._active:
                return self._lifecycle_failure(
                    request, envelope, FAILED, (DUPLICATE_RESULT,),
                    "request is already in flight", started)
            self._active[request.request_id] = envelope
            self._cancel_events[request.request_id] = cancel_event

        tracer = Tracer(self.collector, envelope.execution_id, envelope.task_id)
        tracer.mark(EV.REMOTE_WORKER_REQUESTED, component=EV.REMOTE_WORKER,
                    attributes=self._trace_attributes(envelope))
        raw: Any = None
        try:
            transport_started = time.perf_counter()
            raw = self.transport.send(envelope)
            transport_ms = round((time.perf_counter() - transport_started) * 1000, 3)
            parse_started = time.perf_counter()
            response = self._decode_response(envelope, raw)
            response_parse_ms = round((time.perf_counter() - parse_started) * 1000, 3)
            if cancel_event.is_set() or self._is_cancelled(envelope):
                return self._cancelled_result(
                    request, envelope, started, tracer, LATE_RESULT,
                    "remote response arrived after local cancellation")
            if response.status != "COMPLETED":
                code = REMOTE_FAILED if response.status == "FAILED" else REMOTE_MALFORMED
                return self._lifecycle_failure(
                    request, envelope, FAILED, (code,),
                    response.error.detail if response.error else response.status,
                    started, tracer=tracer, remote_status=response.status)

            mapping_started = time.perf_counter()
            worker_result = self._worker_result(request, envelope, response)
            result_mapping_ms = round((time.perf_counter() - mapping_started) * 1000, 3)
            accepted = self._record_terminal(envelope, COMPLETED, worker_result,
                                              self._fingerprint(response))
            if not accepted:
                return self._lifecycle_failure(
                    request, envelope, FAILED, (DUPLICATE_RESULT,),
                    "remote result was already accepted", started,
                    tracer=tracer, record_terminal=False)
            tracer.mark(EV.REMOTE_WORKER_COMPLETED, component=EV.REMOTE_WORKER,
                        duration_ms=_elapsed_ms(started),
                        attributes={**self._trace_attributes(envelope),
                                    "remote_status": response.status})
            return RemoteLifecycleResult(
                request_id=request.request_id,
                execution_id=envelope.execution_id, task_id=envelope.task_id,
                worker_id=envelope.worker_id,
                interop_request_id=envelope.interop_request_id,
                status=COMPLETED, worker_result=worker_result,
                remote_status=response.status,
                payload_digest=envelope.payload_digest,
                metrics=self._metrics(
                    envelope, started,
                    {"request_preparation_ms": preparation_ms,
                     "transport_ms": transport_ms,
                     "response_parse_ms": response_parse_ms,
                     "result_mapping_ms": result_mapping_ms}))
        except RemoteLedgerFailure as exc:
            return self._lifecycle_failure(
                request, envelope, UNKNOWN, (exc.code,), exc.detail, started,
                tracer=tracer, record_terminal=False)
        except RemoteTransportFailure as exc:
            status = TIMEOUT if exc.code in ("TIMEOUT", REMOTE_TIMEOUT) else (
                UNKNOWN if exc.code in ("UNAVAILABLE", REMOTE_UNAVAILABLE)
                else FAILED)
            code = REMOTE_TIMEOUT if status == TIMEOUT else (
                REMOTE_UNAVAILABLE if status == UNKNOWN else exc.code)
            return self._lifecycle_failure(
                request, envelope, status, (code,), exc.detail, started,
                tracer=tracer)
        except TimeoutError as exc:
            return self._lifecycle_failure(
                request, envelope, TIMEOUT, (REMOTE_TIMEOUT,),
                str(exc) or "remote timeout", started, tracer=tracer)
        except (TypeError, ValueError, KeyError, UnicodeError) as exc:
            code = str(exc) if str(exc) in (IDENTITY_MISMATCH, PAYLOAD_MISMATCH) else REMOTE_MALFORMED
            return self._lifecycle_failure(
                request, envelope, FAILED, (code,), str(exc),
                started, tracer=tracer)
        except (ConnectionError, OSError) as exc:
            return self._lifecycle_failure(
                request, envelope, UNKNOWN, (REMOTE_UNAVAILABLE,), str(exc),
                started, tracer=tracer)
        finally:
            with self._lock:
                self._active.pop(request.request_id, None)
                self._cancel_events.pop(request.request_id, None)

    def reconcile(self, request: WorkerRequest) -> RemoteLifecycleResult:
        """Explicitly retry reconciliation of a durable IN_FLIGHT identity."""
        if self.ledger is None:
            return self._lifecycle_failure(
                request, "", UNKNOWN, (REMOTE_UNAVAILABLE,),
                "no durable remote ledger is configured", time.perf_counter())
        envelope = self.prepare(request)
        try:
            item = self.ledger.get(envelope.idempotency_key)
        except RemoteLedgerFailure as exc:
            return self._lifecycle_failure(
                request, envelope, UNKNOWN, (exc.code,), exc.detail,
                time.perf_counter(), record_terminal=False)
        if item is None or item.status != IN_FLIGHT:
            return self._lifecycle_failure(
                request, envelope, UNKNOWN, (REQUEST_TERMINAL,),
                "no durable in-flight request requires reconciliation",
                time.perf_counter())
        return self._execute_lifecycle(request, allow_in_flight=True)

    def accept_late_response(self, envelope: RemoteEnvelope, raw: Any) -> RemoteLifecycleResult:
        """Reconcile a response after local terminal state without side effects."""
        try:
            item = self.ledger.get(envelope.idempotency_key) if self.ledger else None
        except RemoteLedgerFailure as exc:
            return RemoteLifecycleResult(
                request_id=envelope.interop_request_id.split(":", 1)[0],
                execution_id=envelope.execution_id, task_id=envelope.task_id,
                worker_id=envelope.worker_id,
                interop_request_id=envelope.interop_request_id,
                status=UNKNOWN, reason_codes=(exc.code,),
                diagnostics=({"code": exc.code, "detail": exc.detail},),
                payload_digest=envelope.payload_digest)
        if item is None:
            return RemoteLifecycleResult(
                request_id=envelope.interop_request_id.split(":", 1)[0],
                execution_id=envelope.execution_id, task_id=envelope.task_id,
                worker_id=envelope.worker_id,
                interop_request_id=envelope.interop_request_id,
                status=UNKNOWN, reason_codes=(LATE_RESULT,),
                payload_digest=envelope.payload_digest)
        code = LATE_RESULT if item.status == CANCELLED else DUPLICATE_RESULT
        return RemoteLifecycleResult(
            request_id=envelope.interop_request_id.split(":", 1)[0],
            execution_id=envelope.execution_id, task_id=envelope.task_id,
            worker_id=envelope.worker_id,
            interop_request_id=envelope.interop_request_id,
            status=item.status if item.status in (CANCELLED, COMPLETED) else UNKNOWN,
            reason_codes=(code,), payload_digest=envelope.payload_digest,
            diagnostics=({"code": code,
                          "detail": "late remote result ignored"},))

    def cancel_lifecycle(self, request_id: str) -> RemoteCancellation:
        with self._lock:
            envelope = self._active.get(request_id)
            event = self._cancel_events.get(request_id)
        if envelope is None or event is None:
            return RemoteCancellation(detail="request is not in flight")
        event.set()
        try:
            state = self.transport.cancel(envelope)
            if not isinstance(state, RemoteCancellation):
                state = RemoteCancellation(
                    local_requested=True, remote_request_sent=bool(state),
                    remote_confirmed=bool(state))
        except Exception as exc:  # noqa: BLE001 - best-effort cancellation
            state = RemoteCancellation(local_requested=True,
                                       remote_unknown=True, detail=type(exc).__name__)
        with self._lock:
            self._cancellations[request_id] = state
        reason = (REMOTE_CANCEL_CONFIRMED if state.remote_confirmed else
                  REMOTE_CANCEL_REQUESTED if state.remote_request_sent else
                  REMOTE_CANCEL_UNKNOWN)
        try:
            self._record_terminal(envelope, CANCELLED, None,
                                  reason_codes=(reason,), cancellation=state)
        except RemoteLedgerFailure as exc:
            state = replace(state, remote_unknown=True,
                            detail=f"{state.detail}; {exc.code}")
            with self._lock:
                self._cancellations[request_id] = state
        return state

    def cancel(self, request_id: str) -> bool:
        return self.cancel_lifecycle(request_id).local_requested

    def _begin(self, envelope: RemoteEnvelope) -> LedgerRecord | None:
        return self.ledger.begin(envelope) if self.ledger else None

    def _same_identity(self, item: LedgerRecord, envelope: RemoteEnvelope) -> bool:
        return (item.execution_id == envelope.execution_id
                and item.task_id == envelope.task_id
                and item.worker_id == envelope.worker_id
                and item.payload_digest == envelope.payload_digest)

    def _decode_response(self, envelope: RemoteEnvelope, raw: Any) -> InteropResult:
        if not isinstance(raw, dict):
            raise ValueError("remote response must be a mapping")
        if raw.get("worker_id") != envelope.worker_id:
            raise ValueError(IDENTITY_MISMATCH)
        if raw.get("payload_digest") != envelope.payload_digest:
            raise ValueError(PAYLOAD_MISMATCH)
        result_raw = raw.get("result")
        if not isinstance(result_raw, dict):
            raise ValueError("remote response missing result mapping")
        result = deserialize_result(result_raw)
        if result.interop_request_id != envelope.interop_request_id:
            raise ValueError(IDENTITY_MISMATCH)
        if result.execution_id != envelope.execution_id:
            raise ValueError(IDENTITY_MISMATCH)
        if result.task_id != envelope.task_id:
            raise ValueError(IDENTITY_MISMATCH)
        self._validate_untrusted_metadata(result)
        return result

    def _validate_untrusted_metadata(self, result: InteropResult) -> None:
        """Reject credential-shaped remote metadata before ledger/trace use."""
        values = [result.metadata]
        values.extend(artifact.metadata for artifact in result.artifacts)
        values.extend(artifact.parts for artifact in result.artifacts)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    lowered = str(key).lower()
                    if any(marker in lowered for marker in
                           ("credential", "api_key", "token", "password", "secret")):
                        raise ValueError("secret-shaped remote metadata")
                    walk(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)
            elif isinstance(value, str) and contains_secret(value):
                raise ValueError("secret-shaped remote metadata")

        for value in values:
            walk(value)

    def _worker_result(self, request: WorkerRequest,
                       envelope: RemoteEnvelope,
                       result: InteropResult) -> WorkerResult:
        return WorkerResult(
            request_id=request.request_id, worker_id=self.config.worker_id,
            status=WORKER_COMPLETED, output=result.output,
            artifacts=tuple(artifact.to_dict() for artifact in result.artifacts),
            usage={"remote_payload_chars": envelope.payload_chars,
                   "remote_payload_tokens": envelope.payload_tokens,
                   "payload_digest": envelope.payload_digest,
                   "remote_task_status": result.remote_task_status,
                   "remote_endpoint": self.config.identity.endpoint},
            limitations=("remote output is untrusted and is not authority, "
                         "project truth, a Decision, or Evidence",),
            diagnostics=({"code": "REMOTE_WORKER_NORMALIZED",
                          "detail": "remote result normalized by Tabularium"},))

    def _record_terminal(self, envelope: RemoteEnvelope, status: str,
                         worker_result: WorkerResult | None,
                         fingerprint: str = "", *,
                         reason_codes: tuple[str, ...] = (),
                         cancellation: RemoteCancellation | None = None) -> bool:
        if self.ledger is None:
            return True
        return self.ledger.record(LedgerRecord(
            key=envelope.idempotency_key,
            execution_id=envelope.execution_id, task_id=envelope.task_id,
            worker_id=envelope.worker_id, payload_digest=envelope.payload_digest,
            status=status, result_fingerprint=fingerprint,
            worker_result=asdict(worker_result) if worker_result else None,
            reason_codes=reason_codes,
            cancellation=cancellation.to_dict() if cancellation else None))

    def _is_cancelled(self, envelope: RemoteEnvelope) -> bool:
        try:
            item = self.ledger.get(envelope.idempotency_key) if self.ledger else None
        except RemoteLedgerFailure:
            return False
        return bool(item and item.status == CANCELLED)

    def _cancelled_result(self, request: WorkerRequest, envelope: RemoteEnvelope,
                          started: float, tracer: Tracer, code: str,
                          detail: str) -> RemoteLifecycleResult:
        state = self._cancellations.get(request.request_id)
        tracer.mark(EV.REMOTE_WORKER_CANCELLED, component=EV.REMOTE_WORKER,
                    status=EV.OK, duration_ms=_elapsed_ms(started),
                    reason_codes=(code,),
                    attributes=self._trace_attributes(envelope))
        worker = WorkerResult(
            request_id=request.request_id, worker_id=self.config.worker_id,
            status=WORKER_CANCELLED,
            limitations=("remote result was not accepted after cancellation",),
            diagnostics=({"code": code, "detail": detail[:300]},))
        return RemoteLifecycleResult(
            request_id=request.request_id, execution_id=envelope.execution_id,
            task_id=envelope.task_id, worker_id=envelope.worker_id,
            interop_request_id=envelope.interop_request_id, status=CANCELLED,
            worker_result=worker, reason_codes=(code,),
            payload_digest=envelope.payload_digest,
            cancellation=state, metrics=self._metrics(envelope, started))

    def _lifecycle_failure(self, request: WorkerRequest, envelope: Any,
                           status: str, codes: tuple[str, ...], detail: str,
                           started: float, *, tracer: Tracer | None = None,
                           remote_status: str = "",
                           record_terminal: bool = True) -> RemoteLifecycleResult:
        identity = envelope if isinstance(envelope, RemoteEnvelope) else None
        if identity is not None and status != CANCELLED and self._is_cancelled(identity):
            return self._cancelled_result(
                request, identity, started,
                tracer or Tracer(self.collector, identity.execution_id,
                                 identity.task_id),
                LATE_RESULT, "remote failure arrived after local cancellation")
        worker = WorkerResult(
            request_id=request.request_id, worker_id=self.config.worker_id,
            status=WORKER_FAILED if status != CANCELLED else WORKER_CANCELLED,
            limitations=("remote transport did not produce an accepted result",),
            diagnostics=tuple({"code": code, "detail": detail[:300]}
                              for code in codes))
        if identity is not None:
            terminal_status = status if status in (FAILED, TIMEOUT, CANCELLED) else status
            if record_terminal:
                try:
                    self._record_terminal(identity, terminal_status, worker,
                                          reason_codes=codes)
                except RemoteLedgerFailure as exc:
                    status = UNKNOWN
                    codes = (*codes, exc.code)
                    detail = f"{detail}; {exc.detail}"
            if tracer is not None:
                event_type = (EV.REMOTE_WORKER_TIMEOUT if status == TIMEOUT
                              else EV.REMOTE_WORKER_CANCELLED if status == CANCELLED
                              else EV.REMOTE_WORKER_FAILED)
                tracer.mark(event_type, component=EV.REMOTE_WORKER,
                            status=EV.ERROR, duration_ms=_elapsed_ms(started),
                            reason_codes=codes,
                            attributes={**self._trace_attributes(identity),
                                        "remote_status": remote_status})
        return RemoteLifecycleResult(
            request_id=request.request_id,
            execution_id=identity.execution_id if identity else "",
            task_id=identity.task_id if identity else request.task_id or "",
            worker_id=self.config.worker_id,
            interop_request_id=identity.interop_request_id if identity else "",
            status=status, worker_result=worker, reason_codes=codes,
            diagnostics=({"code": codes[0], "detail": detail[:300]},),
            payload_digest=identity.payload_digest if identity else "",
            remote_status=remote_status,
            metrics=self._metrics(identity, started) if identity else {})

    def _trace_attributes(self, envelope: RemoteEnvelope) -> dict[str, Any]:
        return {
            "remote_endpoint": self.config.identity.endpoint,
            "remote_agent_id": self.config.identity.agent_id,
            "remote_worker": self.config.identity.worker,
            "worker_id": envelope.worker_id,
            "payload_chars": envelope.payload_chars,
            "payload_tokens": envelope.payload_tokens,
            "payload_digest_prefix": envelope.payload_digest[:16],
        }

    def _metrics(self, envelope: RemoteEnvelope | None,
                 started: float, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        values = {"total_ms": _elapsed_ms(started)}
        if extra:
            values.update(extra)
        if envelope is not None and hasattr(self.transport, "transport_metrics"):
            values.update(self.transport.transport_metrics(envelope))
        return values

    def _fingerprint(self, result: InteropResult) -> str:
        body = json.dumps(result.to_dict(), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
