"""A deterministic local fixture tool (M4.6).

Exists to exercise the governed boundary end to end with no network, no
credentials, and no shell. Two operations:

    read_text    READ_ONLY        may proceed without human authorization
    write_text   STATE_CHANGING   ALWAYS requires an exact ToolAuthorization

There is deliberately NO `run_command` and no arbitrary shell execution: the
milestone needs a read path and a write path, and an arbitrary-command tool
would be a far larger authority surface than the boundary being demonstrated.

The adapter is confined to a root supplied at construction. It re-checks
containment itself rather than trusting the gate — defence in depth, since an
adapter is the last thing standing between a request and the filesystem.
"""

from __future__ import annotations

from pathlib import Path

from . import model as M

TOOL_NAME = "fixture_file"

READ_TEXT = "read_text"
WRITE_TEXT = "write_text"


class FixtureFileTool:
    """A file tool confined to one throwaway root. Implements ToolAdapter."""

    def __init__(self, root: str | Path, max_read_chars: int = 20_000) -> None:
        self.root = Path(root).resolve()
        self.max_read_chars = max_read_chars

    def descriptor(self) -> M.ToolDescriptor:
        return M.ToolDescriptor(
            name=TOOL_NAME,
            tool_class=M.READ,
            operations={READ_TEXT: M.READ_ONLY},
            description="Read a UTF-8 text file inside a fixture root.")

    def execute(self, request: M.ToolRequest) -> M.ToolResult:
        target = self._contained(request.arguments.get("path"))
        if target is None:
            return self._failed(request, M.TOOL_SCOPE_VIOLATION,
                                "path escapes the adapter root")
        if request.operation == READ_TEXT:
            return self._read(request, target)
        return self._failed(request, M.UNKNOWN_OPERATION,
                            f"{request.operation!r} is not supported")

    # -- operations ---------------------------------------------------------

    def _read(self, request, target: Path) -> M.ToolResult:
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return self._failed(request, M.TOOL_ADAPTER_FAILED,
                                f"{type(exc).__name__}: {str(exc)[:120]}")
        truncated = text[:self.max_read_chars]
        return M.ToolResult(
            tool_request_id=request.tool_request_id, status=M.EXECUTED,
            output=truncated, executed_by=TOOL_NAME,
            artifacts=({"path": str(target), "bytes": str(len(text))},))

    # -- helpers ------------------------------------------------------------

    def _contained(self, name) -> Path | None:
        """Resolve under the root, or None if it escapes.

        Canonical containment: both sides fully resolved, so `..`, absolute
        paths, and symlink/junction targets are judged by where they land.
        """
        if not isinstance(name, str) or not name.strip():
            return None
        try:
            candidate = (self.root / name).resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        if candidate == self.root or self.root in candidate.parents:
            return candidate
        return None

    @staticmethod
    def _failed(request, code: str, detail: str) -> M.ToolResult:
        return M.ToolResult(
            tool_request_id=request.tool_request_id, status=M.FAILED,
            reason_codes=(code,), executed_by=TOOL_NAME,
            diagnostics=({"code": code, "detail": detail},))


class FixtureWriteTool(FixtureFileTool):
    """The STATE_CHANGING counterpart, kept as a separate registered tool.

    Splitting read from write means a skill granted only READ cannot reach a
    write operation by renaming it — the tool CLASS differs, and the gate
    checks the class against the descriptor rather than against the request's
    own claim.
    """

    def descriptor(self) -> M.ToolDescriptor:
        return M.ToolDescriptor(
            name="fixture_file_write",
            tool_class=M.WRITE,
            operations={WRITE_TEXT: M.STATE_CHANGING},
            description="Write a UTF-8 text file inside a fixture root.")

    def execute(self, request: M.ToolRequest) -> M.ToolResult:
        target = self._contained(request.arguments.get("path"))
        if target is None:
            return self._failed(request, M.TOOL_SCOPE_VIOLATION,
                                "path escapes the adapter root")
        if request.operation != WRITE_TEXT:
            return self._failed(request, M.UNKNOWN_OPERATION,
                                f"{request.operation!r} is not supported")
        content = request.arguments.get("content")
        if not isinstance(content, str):
            return self._failed(request, M.TOOL_REQUEST_INVALID,
                                "arguments.content must be a string")
        if not target.parent.is_dir():
            return self._failed(request, M.TOOL_ADAPTER_FAILED,
                                "target directory does not exist")
        try:
            target.write_text(content, encoding="utf-8", newline="\n")
        except OSError as exc:
            return self._failed(request, M.TOOL_ADAPTER_FAILED,
                                f"{type(exc).__name__}: {str(exc)[:120]}")
        return M.ToolResult(
            tool_request_id=request.tool_request_id, status=M.EXECUTED,
            output=f"wrote {len(content)} chars", executed_by="fixture_file_write",
            artifacts=({"path": str(target),
                        "digest": M.digest(content)},))
