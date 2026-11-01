"""Tabularium-owned remote worker lifecycle (M6.3).

STATUS: UNWIRED / OPT-IN EXTENSION.
This module is an opt-in extension and is not wired to default runtime paths.
This package adapts the existing WorkerAdapter and M6.1 interop contracts to a
bounded request/response lifecycle. It performs no network activity at import
time. The standard-library HTTP transport is explicit and optional.
"""

PACKAGE_STATUS: str = "UNWIRED / OPT-IN EXTENSION"

from .adapter import RemoteWorkerAdapter
from .ledger import InMemoryRemoteLedger, JsonRemoteLedger, RemoteLedgerFailure
from .model import (
    CANCELLED,
    COMPLETED,
    FAILED,
    IN_FLIGHT,
    REQUESTED,
    TIMEOUT,
    UNKNOWN,
    RemoteCancellation,
    RemoteEnvelope,
    RemoteLifecycleResult,
    RemoteWorkerConfig,
)
from .tls import TLSConfigurationError, build_tls_context, validate_tls_context
from .transport import RemoteTransportFailure, UrllibJsonTransport

__all__ = [
    "RemoteWorkerAdapter", "InMemoryRemoteLedger", "JsonRemoteLedger",
    "RemoteLedgerFailure",
    "RemoteWorkerConfig", "RemoteEnvelope", "RemoteLifecycleResult",
    "RemoteCancellation", "UrllibJsonTransport", "RemoteTransportFailure",
    "build_tls_context", "validate_tls_context", "TLSConfigurationError",
    "REQUESTED", "IN_FLIGHT", "COMPLETED", "FAILED", "TIMEOUT",
    "CANCELLED", "UNKNOWN",
]
