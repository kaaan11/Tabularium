"""The Tabularium tool gate (M4.6). READ-ONLY — it decides, it never executes.

Every check is explicit and separately reported. There is no "mostly allowed"
path and no silent fallback to a different tool: any failed check denies.

Authority direction, unchanged from M3.4:

    effective tool classes = control-plane grant INTERSECT skill sidecar

A skill can only ever NARROW. Widening is structurally inexpressible because
the intersection is computed from the grant, and the grant is Tabularium's.

The gate FAILS CLOSED: an unexpected exception inside a check becomes a failed
check rather than propagating, because an evaluator that raises leaves the
caller with no verdict at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..skills.governance import check_tools
from ..skills.model import EffectiveConstraints
from . import model as M


def evaluate(request: M.ToolRequest,
             descriptor: M.ToolDescriptor | None,
             constraints: EffectiveConstraints | None = None,
             authorization: Any = None,
             used_bindings: tuple[str, ...] = (),
             replay_store: Any = None) -> M.ToolGateEvaluation:
    """Decide whether ONE tool request may proceed. Executes nothing."""
    checks: list[M.CheckResult] = []
    effect = ""
    needs_auth = False

    steps = (
        ("request_well_formed", lambda: _well_formed(request)),
        ("no_secret_material", lambda: _no_secrets(request)),
        ("tool_known", lambda: _tool_known(request, descriptor)),
        ("class_matches_tool", lambda: _class_matches(request, descriptor)),
        ("operation_known", lambda: _operation_known(request, descriptor)),
        ("skill_permits_class", lambda: _skill_permits(request, constraints)),
        ("scope_declared", lambda: _scope_declared(request, descriptor)),
        ("scope_confined", lambda: _scope_confined(request, descriptor)),
    )
    for name, check in steps:
        checks.append(_guard(name, check))

    if all(c.passed for c in checks) and descriptor is not None:
        effect = descriptor.effect(request.operation) or ""
        needs_auth = effect in M.EFFECTS_REQUIRING_AUTHORIZATION
        if needs_auth:
            checks.append(_guard(
                "authorized",
                lambda: _authorized(request, authorization, used_bindings, replay_store)))

    permitted = all(c.passed for c in checks)
    codes: list[str] = []
    for check in checks:
        if not check.passed and check.reason_code not in codes:
            codes.append(check.reason_code)

    return M.ToolGateEvaluation(
        permitted=permitted, effect=effect,
        authorization_required=needs_auth, checks=tuple(checks),
        reason_codes=tuple(codes))


# --- individual checks ------------------------------------------------------

def _well_formed(request: M.ToolRequest) -> M.CheckResult:
    if not isinstance(request, M.ToolRequest):
        return _fail("request_well_formed", M.TOOL_REQUEST_INVALID,
                     "not a ToolRequest")
    for field in ("tool_request_id", "execution_id", "tool_name", "operation",
                  "tool_class"):
        if not _nonempty(getattr(request, field, "")):
            return _fail("request_well_formed", M.TOOL_REQUEST_INVALID,
                         f"{field} is required")
    if not isinstance(request.arguments, dict):
        return _fail("request_well_formed", M.TOOL_REQUEST_INVALID,
                     "arguments must be a mapping")
    if not isinstance(request.requested_scope, dict):
        return _fail("request_well_formed", M.TOOL_REQUEST_INVALID,
                     "requested_scope must be a mapping")
    if request.tool_class not in M.REQUESTABLE_CLASSES:
        return _fail("request_well_formed", M.UNKNOWN_TOOL_CLASS,
                     f"{request.tool_class!r} is not a requestable class")
    return _ok("request_well_formed")


def _no_secrets(request: M.ToolRequest) -> M.CheckResult:
    """Credentials belong to an external operator source, never a request."""
    blob = M.canonical({"arguments": request.arguments,
                        "scope": request.requested_scope,
                        "reason": request.reason})
    if M.contains_secret(blob):
        return _fail("no_secret_material", M.SECRET_MATERIAL_PRESENT,
                     "credential-shaped material in the request")
    return _ok("no_secret_material")


def _tool_known(request, descriptor) -> M.CheckResult:
    if descriptor is None:
        return _fail("tool_known", M.UNKNOWN_TOOL,
                     f"no registered tool named {request.tool_name!r}")
    if descriptor.name != request.tool_name:
        return _fail("tool_known", M.UNKNOWN_TOOL,
                     "descriptor does not match the requested tool")
    return _ok("tool_known")


def _class_matches(request, descriptor) -> M.CheckResult:
    """A request may not relabel a tool into a class the skill permits."""
    if descriptor is None:
        return _fail("class_matches_tool", M.UNKNOWN_TOOL, "no descriptor")
    if descriptor.tool_class != request.tool_class:
        return _fail("class_matches_tool", M.TOOL_CLASS_MISMATCH,
                     f"tool is {descriptor.tool_class}, request claims "
                     f"{request.tool_class}")
    return _ok("class_matches_tool")


def _operation_known(request, descriptor) -> M.CheckResult:
    if descriptor is None:
        return _fail("operation_known", M.UNKNOWN_TOOL, "no descriptor")
    effect = descriptor.effect(request.operation)
    if effect is None:
        return _fail("operation_known", M.UNKNOWN_OPERATION,
                     f"{request.operation!r} is not supported by "
                     f"{descriptor.name}")
    if effect not in M.EFFECTS:
        return _fail("operation_known", M.UNKNOWN_OPERATION,
                     f"operation declares unknown effect {effect!r}")
    return _ok("operation_known")


def _skill_permits(request, constraints) -> M.CheckResult:
    """Reuses the M3.4 check. Absent constraints means NOTHING is permitted."""
    if constraints is None:
        return _fail("skill_permits_class", M.TOOL_CLASS_DENIED_BY_SKILL,
                     "no effective constraints supplied; tools denied")
    compat = check_tools(constraints, (request.tool_class,))
    if not compat.compatible:
        return _fail("skill_permits_class", M.TOOL_CLASS_DENIED_BY_SKILL,
                     f"{request.tool_class} not in effective tool classes")
    return _ok("skill_permits_class")


def _scope_declared(request, descriptor) -> M.CheckResult:
    """Scope must be explicit. No wildcard, no unbounded traversal."""
    if descriptor is None:
        return _fail("scope_declared", M.UNKNOWN_TOOL, "no descriptor")
    root = request.requested_scope.get("root")
    if not _nonempty(root):
        return _fail("scope_declared", M.TOOL_SCOPE_REQUIRED,
                     "requested_scope.root is required")
    if any(token in str(root) for token in ("*", "?", "[")):
        return _fail("scope_declared", M.TOOL_SCOPE_VIOLATION,
                     "wildcard scope is not permitted")
    return _ok("scope_declared")


def _scope_confined(request, descriptor) -> M.CheckResult:
    """The target path must canonically resolve INSIDE the declared root.

    Both sides are fully resolved first, so `..`, absolute paths, and symlink
    or junction targets are judged by where they actually land rather than by
    how they are spelled — the same containment rule M3.4's loader uses.
    """
    root = request.requested_scope.get("root")
    target = request.arguments.get("path")
    if not _nonempty(target):
        return _fail("scope_confined", M.TOOL_SCOPE_REQUIRED,
                     "arguments.path is required for a file-like operation")
    try:
        root_path = Path(str(root)).resolve()
        candidate = (root_path / str(target)).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        return _fail("scope_confined", M.TOOL_SCOPE_VIOLATION,
                     f"unresolvable path ({type(exc).__name__})")
    if candidate != root_path and root_path not in candidate.parents:
        return _fail("scope_confined", M.TOOL_SCOPE_VIOLATION,
                     "target escapes the declared scope root")
    return _ok("scope_confined")


def _authorized(request, authorization, used_bindings, replay_store=None) -> M.CheckResult:
    """A STATE_CHANGING / EXTERNAL_EFFECT action needs exact authority.

    Nothing that is not a full ToolAuthorization parses as one, so a bare
    `True`, the string "APPROVE", worker prose, an M4.3 execution approval, and
    an M4.4 persistence authorization all land here as "no authorization".
    """
    parsed = (authorization if isinstance(authorization, M.ToolAuthorization)
              else M.ToolAuthorization.from_dict(authorization))
    if parsed is None:
        return _fail("authorized", M.TOOL_AUTHORIZATION_REQUIRED,
                     "no tool authorization supplied")
    if not _nonempty(parsed.authorized_by):
        return _fail("authorized", M.TOOL_ACTOR_REQUIRED,
                     "authorized_by is required")
    if not M.valid_timestamp(parsed.authorized_at):
        return _fail("authorized", M.TOOL_TIMESTAMP_INVALID,
                     "authorized_at must be ISO 8601 with explicit offset")
    if parsed.tool_request_id != request.tool_request_id:
        return _fail("authorized", M.TOOL_AUTHORIZATION_MISMATCH,
                     "authorization is for a different request")
    if parsed.execution_id != request.execution_id:
        return _fail("authorized", M.TOOL_EXECUTION_MISMATCH,
                     "authorization is for a different execution")
    if (parsed.tool_name != request.tool_name
            or parsed.operation != request.operation):
        return _fail("authorized", M.TOOL_AUTHORIZATION_MISMATCH,
                     "authorization is for a different tool or operation")
    if parsed.arguments_digest != request.arguments_digest:
        return _fail("authorized", M.TOOL_ARGUMENTS_CHANGED,
                     "arguments changed after authorization")
    expected = request.binding()
    if parsed.binding and parsed.binding != expected:
        return _fail("authorized", M.TOOL_AUTHORIZATION_MISMATCH,
                     "binding does not match this exact request")
    if expected in tuple(used_bindings):
        return _fail("authorized", M.TOOL_AUTHORIZATION_ALREADY_USED,
                     "this authorization was already used")
    if replay_store is not None:
        # Atomically reserve BEFORE the tool runs. A store that cannot claim the
        # binding, or that offers no way to claim it, refuses the action: a
        # replay defense that cannot be exercised is not a defense.
        reserve_fn = getattr(replay_store, "reserve", None) \
            or getattr(replay_store, "consume", None)
        if reserve_fn is None:
            return _fail("authorized", M.TOOL_GATE_FAILED,
                         "replay store exposes neither reserve nor consume")
        if not reserve_fn(expected,
                          execution_id=request.execution_id,
                          tool_request_id=request.tool_request_id):
            return _fail("authorized", M.TOOL_AUTHORIZATION_ALREADY_USED,
                         "this authorization was already used (durable store)")
    return _ok("authorized")


# --- helpers ----------------------------------------------------------------

def _guard(name: str, check) -> M.CheckResult:
    """Run one check; an unexpected error becomes a FAILED check."""
    try:
        return check()
    except Exception as exc:  # noqa: BLE001 - the gate FAILS CLOSED
        return _fail(name, M.TOOL_GATE_FAILED,
                     f"check raised {type(exc).__name__}")


def _ok(name: str) -> M.CheckResult:
    return M.CheckResult(name=name, passed=True)


def _fail(name: str, code: str, detail: str) -> M.CheckResult:
    return M.CheckResult(name=name, passed=False, reason_code=code,
                         detail=detail)


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())
