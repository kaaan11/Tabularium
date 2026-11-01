"""Framework-neutral interoperability boundary (M6.1).

STATUS: UNWIRED / OPT-IN EXTENSION.
This module is an opt-in extension and is not wired to default runtime paths.
Interop is a transport seam, not an authority seam.  The package deliberately
owns plain Tabularium envelopes and a deterministic local fixture; it does not
import an A2A SDK, a network client, or any persistence/tool/approval module.
"""

PACKAGE_STATUS: str = "UNWIRED / OPT-IN EXTENSION"

from .a2a_protocol import (
    A2A_PROTOCOL_VERSION,
    deserialize_a2a_task_response,
    serialize_a2a_task_request,
)
from .adapter import InteropAdapter, InteropTransport, TransportFailure
from .fixture import FixtureMode, LocalRemoteTransport
from .mapping import deserialize_request, deserialize_result, serialize_request
from .model import (
    INTEROP_RESULT_STATUSES,
    CAPABILITY_NOT_TRUSTED,
    DUPLICATE_RESULT,
    MALFORMED_RESPONSE,
    PROTOCOL_ERROR,
    REMOTE_STATUS_NOT_COMPLETED,
    REMOTE_BLOCKED,
    REMOTE_COMPLETED,
    REMOTE_FAILED,
    REMOTE_FAILED_STATUS,
    REMOTE_INPUT_REQUIRED,
    REMOTE_WAITING,
    TIMEOUT,
    UNAVAILABLE,
    WRONG_EXECUTION_ID,
    WRONG_TASK_ID,
    InteropArtifact,
    InteropError,
    InteropRequest,
    InteropResult,
    RemoteCapabilityView,
    RemoteIdentity,
)

__all__ = [
    "InteropAdapter", "InteropTransport", "TransportFailure",
    "LocalRemoteTransport", "FixtureMode", "serialize_request",
    "deserialize_request", "deserialize_result", "InteropRequest", "InteropResult",
    "InteropArtifact", "InteropError", "RemoteIdentity",
    "RemoteCapabilityView", "INTEROP_RESULT_STATUSES", "UNAVAILABLE",
    "TIMEOUT", "MALFORMED_RESPONSE", "PROTOCOL_ERROR", "REMOTE_COMPLETED", "REMOTE_FAILED",
    "REMOTE_FAILED_STATUS",
    "CAPABILITY_NOT_TRUSTED", "DUPLICATE_RESULT", "REMOTE_STATUS_NOT_COMPLETED",
    "WRONG_EXECUTION_ID", "WRONG_TASK_ID",
    "REMOTE_WAITING", "REMOTE_INPUT_REQUIRED", "REMOTE_BLOCKED",
    "A2A_PROTOCOL_VERSION", "serialize_a2a_task_request", "deserialize_a2a_task_response",
]
