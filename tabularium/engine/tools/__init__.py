"""Governed tool boundary (M4.6).

    WORKER MAY REQUEST A TOOL ACTION.
    WORKER MAY NOT GRANT ITSELF AUTHORITY TO PERFORM THAT ACTION.

    Worker / Runtime -> ToolRequest -> Tabularium tool gate
                     -> human authorization if the effect requires it
                     -> ToolAdapter -> ToolResult

Three distinct objects — `ToolRequest` (what a worker wants),
`ToolAuthorization` (authority for ONE exact action), `ToolResult` (what
happened) — with no conversion between them. Tool classes are reused from the
M3.4 sidecar vocabulary, and a skill can only ever NARROW them.

Separation held in all three directions, each asserted by test:

    an M4.3 execution approval      does NOT authorize a tool
    an M4.4 persistence authorization does NOT authorize a tool
    a ToolAuthorization             does NOT authorize project persistence

MCP: TRANSPORT DEFERRED — TABULARIUM BOUNDARY READY. The mapping helpers in
`adapter.py` speak the MCP wire shapes with no SDK dependency.
"""

from .adapter import (ToolAdapter, from_mcp_result, run_tool_cycle,
                      runtime_state, to_mcp_call, to_mcp_tool)
from .governance import evaluate
from .local_fixture import FixtureFileTool, FixtureWriteTool
from .store import JsonToolReplayStore, ToolReplayStore, ToolReplayStoreFailure
from .model import (AUTHORIZATION_REQUIRED, DENIED, EFFECTS,
                    EFFECTS_REQUIRING_AUTHORIZATION, EXECUTED,
                    EXTERNAL_EFFECT, FAILED, NETWORK, NONE, PROCESS, READ,
                    READ_ONLY, REASON_CODES, REQUESTABLE_CLASSES,
                    RESULT_STATUSES, STATE_CHANGING, TOOL_APPROVAL_REQUIRED,
                    TOOL_CLASS_VALUES, WRITE, CheckResult, ToolAuthorization,
                    ToolDescriptor, ToolGateEvaluation, ToolRequest,
                    ToolResult, canonical, contains_secret, digest,
                    valid_timestamp)

__all__ = [
    "ToolRequest", "ToolAuthorization", "ToolResult", "ToolDescriptor",
    "ToolGateEvaluation", "CheckResult", "ToolAdapter",
    "ToolReplayStore", "JsonToolReplayStore", "ToolReplayStoreFailure",
    "evaluate", "run_tool_cycle", "runtime_state",
    "FixtureFileTool", "FixtureWriteTool",
    "to_mcp_tool", "to_mcp_call", "from_mcp_result",
    "digest", "canonical", "valid_timestamp", "contains_secret",
    "TOOL_CLASS_VALUES", "REQUESTABLE_CLASSES", "NONE", "READ", "WRITE",
    "PROCESS", "NETWORK", "EFFECTS", "READ_ONLY", "STATE_CHANGING",
    "EXTERNAL_EFFECT", "EFFECTS_REQUIRING_AUTHORIZATION",
    "RESULT_STATUSES", "EXECUTED", "DENIED", "FAILED",
    "AUTHORIZATION_REQUIRED", "TOOL_APPROVAL_REQUIRED", "REASON_CODES",
]
