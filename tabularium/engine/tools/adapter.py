"""The tool adapter boundary and the one-step governed tool cycle (M4.6).

    worker requests a tool
      -> Tabularium tool gate
      -> human authorization if the effect requires it
      -> adapter executes
      -> ToolResult returned to the execution flow

Deliberately NOT a nested-agent loop and NOT a plugin framework: one request,
one decision, one execution. There is no automatic tool selection and no
fallback to a different tool when one is denied.

MCP MAPPING (transport only). The mapping helpers below translate Tabularium objects
to and from the MCP wire shapes WITHOUT importing the SDK. MCP carries the
CALL; it does not carry AUTHORITY — `CallToolRequestParams` has fields for a
name and arguments and no field in which permission could travel. The gate
therefore sits above any transport, and swapping a local adapter for an MCP
client changes nothing about who decides.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..skills.model import EffectiveConstraints
from . import governance as G
from . import model as M


@runtime_checkable
class ToolAdapter(Protocol):
    """What Tabularium needs from any tool, local or remote.

    An adapter EXECUTES an already-permitted request. It performs no
    permission logic of its own: by the time `execute` is called the gate has
    already decided. An adapter that tried to authorize itself would be
    reimplementing the boundary it exists to sit behind.
    """

    def descriptor(self) -> M.ToolDescriptor:
        """The tool's registered identity, class, and operation effects."""
        ...

    def execute(self, request: M.ToolRequest) -> M.ToolResult:
        """Perform the action. Called ONLY after the gate permitted it."""
        ...


def run_tool_cycle(request: M.ToolRequest,
                   adapter: ToolAdapter | None,
                   constraints: EffectiveConstraints | None = None,
                   authorization: Any = None,
                   used_bindings: tuple[str, ...] = (),
                   collector: Any = None,
                   replay_store: Any = None) -> M.ToolResult:
    """One governed tool step. The ONLY path from a request to an action.

    Returns a structured result for every outcome:

        AUTHORIZATION_REQUIRED  the gate would permit it, but a human must
                                authorize this exact action first
        DENIED                  a check refused; nothing was executed
        EXECUTED / FAILED       the adapter ran and reported
    """
    from tabularium.engine.observability import Tracer
    from tabularium.engine.observability import events as EV

    tracer = Tracer(collector, request.execution_id,
                    getattr(request, "task_id", ""))
    tracer.mark(EV.TOOL_REQUESTED, component=EV.TOOLS,
                tool_request_id=request.tool_request_id,
                attributes={"tool_name": request.tool_name,
                            "tool_class": request.tool_class,
                            "operation": request.operation})

    descriptor = None
    if adapter is not None:
        try:
            descriptor = adapter.descriptor()
        except Exception as exc:  # noqa: BLE001 - an unusable adapter is unknown
            return M.ToolResult(
                tool_request_id=getattr(request, "tool_request_id", ""),
                status=M.FAILED, reason_codes=(M.TOOL_ADAPTER_FAILED,),
                diagnostics=({"code": M.TOOL_ADAPTER_FAILED,
                              "detail": f"descriptor raised "
                                        f"{type(exc).__name__}"},))

    evaluation = G.evaluate(request, descriptor, constraints, authorization,
                            used_bindings=used_bindings,
                            replay_store=replay_store)

    if not evaluation.permitted:
        # An action needing authorization that has none is reported distinctly
        # from a refusal, so a caller can tell "ask a human" from "no".
        only_missing_auth = (
            evaluation.authorization_required
            and evaluation.reason_codes == (M.TOOL_AUTHORIZATION_REQUIRED,))
        refusal = M.ToolResult(
            tool_request_id=request.tool_request_id,
            status=(M.AUTHORIZATION_REQUIRED if only_missing_auth
                    else M.DENIED),
            reason_codes=evaluation.reason_codes or (M.TOOL_GATE_FAILED,),
            gate=evaluation,
            diagnostics=tuple({"code": c.reason_code or M.TOOL_GATE_FAILED,
                               "detail": f"{c.name}: {c.detail}"}
                              for c in evaluation.failed))
        tracer.mark(EV.TOOL_AUTHORIZATION_REQUIRED if only_missing_auth
                    else EV.TOOL_DENIED, component=EV.TOOLS,
                    status=EV.PENDING if only_missing_auth else EV.ERROR,
                    tool_request_id=request.tool_request_id,
                    reason_codes=tuple(refusal.reason_codes),
                    attributes={"effect": evaluation.effect})
        return refusal

    try:
        result = adapter.execute(request)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return M.ToolResult(
            tool_request_id=request.tool_request_id, status=M.FAILED,
            reason_codes=(M.TOOL_ADAPTER_FAILED,), gate=evaluation,
            diagnostics=({"code": M.TOOL_ADAPTER_FAILED,
                          "detail": f"{type(exc).__name__}: "
                                    f"{str(exc)[:120]}"},))

    if not isinstance(result, M.ToolResult):
        return M.ToolResult(
            tool_request_id=request.tool_request_id, status=M.FAILED,
            reason_codes=(M.TOOL_ADAPTER_FAILED,), gate=evaluation,
            diagnostics=({"code": M.TOOL_ADAPTER_FAILED,
                          "detail": "adapter returned a non-ToolResult"},))

    # Carry the gate verdict onto the result so an audit shows WHY it ran.
    tracer.mark(EV.TOOL_EXECUTED if result.status == M.EXECUTED
                else EV.TOOL_FAILED, component=EV.TOOLS,
                status=EV.OK if result.status == M.EXECUTED else EV.ERROR,
                tool_request_id=request.tool_request_id,
                reason_codes=tuple(result.reason_codes),
                attributes={"effect": evaluation.effect,
                            "tool_name": request.tool_name,
                            "output_chars": len(result.output or "")})
    return M.ToolResult(
        tool_request_id=result.tool_request_id or request.tool_request_id,
        status=result.status, output=result.output,
        artifacts=result.artifacts, reason_codes=result.reason_codes,
        gate=evaluation, executed_by=result.executed_by or descriptor.name,
        diagnostics=result.diagnostics)


def runtime_state(result: M.ToolResult) -> str:
    """The control state a runtime surfaces for this result.

    TOOL_APPROVAL_REQUIRED is deliberately distinct from the M4.1
    AWAITING_APPROVAL: one is about touching the outside world, the other
    about an execution continuing. Collapsing them would let an execution
    approval read as tool permission.
    """
    return (M.TOOL_APPROVAL_REQUIRED
            if result.status == M.AUTHORIZATION_REQUIRED else result.status)


# --- MCP mapping (no SDK dependency) ----------------------------------------
#
# Shapes verified against the official `mcp` package, protocol revision
# 2026-07-28: Tool(name, description, input_schema, output_schema, …),
# CallToolRequestParams(name, arguments, …), CallToolResult(content, is_error,
# structured_content, …).

def to_mcp_tool(descriptor: M.ToolDescriptor) -> dict[str, Any]:
    """Tabularium descriptor -> an MCP `Tool` advertisement dict.

    Operation and effect travel as annotations; MCP has no notion of Tabularium tool
    classes, which is precisely why the class check stays on this side.
    """
    return {
        "name": descriptor.name,
        "description": descriptor.description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string",
                              "enum": sorted(descriptor.operations)},
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["operation", "path"],
        },
        "annotations": {"tabuToolClass": descriptor.tool_class,
                        "tabuOperationEffects": dict(descriptor.operations)},
    }


def to_mcp_call(request: M.ToolRequest) -> dict[str, Any]:
    """Tabularium request -> MCP `CallToolRequestParams` dict.

    NOTE the omission: no authorization field exists in the MCP call shape and
    none is invented here. Authority does not travel over the wire; it is
    settled by the gate before this mapping is ever reached.
    """
    return {"name": request.tool_name,
            "arguments": {"operation": request.operation,
                          **dict(request.arguments)}}


def from_mcp_result(request: M.ToolRequest,
                    payload: dict[str, Any]) -> M.ToolResult:
    """MCP `CallToolResult` dict -> Tabularium ToolResult.

    `isError` maps to FAILED rather than raising: a remote tool reporting
    failure is data, not an exception, and never a partial success.
    """
    blocks = payload.get("content") or []
    text = "\n".join(str(b.get("text", "")) for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text")
    failed = bool(payload.get("isError") or payload.get("is_error"))
    return M.ToolResult(
        tool_request_id=request.tool_request_id,
        status=(M.FAILED if failed else M.EXECUTED),
        output=text,
        reason_codes=((M.TOOL_ADAPTER_FAILED,) if failed else ()),
        executed_by=request.tool_name)
