"""Native/session-authenticated CLI worker, using Codex exec when installed."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from tabularium.engine.workers import (CLI_CODING, COMPLETED, FAILED, REJECTED,
                             WorkerDescriptor, WorkerRequest, WorkerResult,
                             check_acceptance)
from tabularium.engine.workers.model import CANCELLABLE, STREAMS_OUTPUT, STRUCTURED_OUTPUT

from .prompt import render_prompt
from .transport import CANCELLED, EXITED, SPAWN_FAILED, TIMED_OUT, NativeProcess

NATIVE_WORKER_ID = "codex-cli"
_CANDIDATES = ("codex", "codex.exe")
_SECRET = re.compile(r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._~-]+|(?:api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;]+)")

# Stored-diagnostic bound for one structured failure message. Mirrors the
# established 300-character diagnostic-detail bound already used by the API
# worker (tabularium/engine/workers/api/transport.py::sanitize_detail); that helper is
# not imported because it belongs to a sibling worker package and applies a
# different, line-dropping secret policy than this worker's _scrub.
_ERROR_DETAIL_LIMIT = 300
# A failure normally arrives twice (error + turn.failed). Keep the evidence
# small and bounded rather than building an event history.
_MAX_ERROR_EVENTS = 2
WORKER_ERROR_EVENT = "WORKER_ERROR_EVENT"


def _scrub(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text)


def _error_detail(text: str) -> str:
    """Secret-scrubbed, whitespace-normalized, bounded stored detail.

    Scrubbing happens here, before the diagnostic can reach a WorkerResult and
    from there the runtime checkpoint. Terminal escaping is deliberately NOT
    done here: rendering is the product layer's boundary, not the worker's.
    """
    return " ".join(_scrub(text).split())[:_ERROR_DETAIL_LIMIT]


def find_native_executable() -> str | None:
    configured = os.environ.get("TABU_NATIVE_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() else None
    for name in _CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


class NativeCLIWorker:
    """A read-only Codex CLI adapter behind the frozen WorkerAdapter seam."""

    def __init__(self, executable: str | None = None, *, cwd: str | Path | None = None,
                 timeout_s: float = 120.0, worker_id: str = NATIVE_WORKER_ID,
                 sandbox: str = "read-only"):
        self.executable = executable or find_native_executable()
        self.sandbox = sandbox
        self._scratch = None
        if cwd is None:
            self._scratch = tempfile.TemporaryDirectory(prefix="tabu-native-")
            cwd = self._scratch.name
        self.cwd = str(cwd)
        self.timeout_s = timeout_s
        self._process: NativeProcess | None = None
        self._descriptor = WorkerDescriptor(
            worker_id=worker_id, category=CLI_CODING,
            capabilities=(STREAMS_OUTPUT, STRUCTURED_OUTPUT, CANCELLABLE))

    @property
    def descriptor(self) -> WorkerDescriptor:
        return self._descriptor

    def accepts(self, request: WorkerRequest):
        return check_acceptance(self._descriptor, request)

    def build_argv(self, request: WorkerRequest) -> list[str]:
        # The prompt is stdin; '-' is the documented exec stdin sentinel.
        return [self.executable or "", "exec", "--json", "--sandbox",
                self.sandbox, "--skip-git-repo-check", "-"]

    @staticmethod
    def _failure_text_from_event(data: dict) -> str:
        """Text of the two known Codex structured failure shapes, or "".

        Recognized, and nothing else:

            {"type": "error",       "message": "..."}
            {"type": "turn.failed", "error": {"message": "..."}}

        This is not a generic event parser: no recursive search, no nested
        lookup beyond the one documented level, and no interpretation of what
        the failure MEANS. Quota, auth, and rate-limit semantics are the
        provider's; the worker only reports that Codex emitted a failure.
        """
        kind = data.get("type")
        if kind == "error":
            message = data.get("message")
        elif kind == "turn.failed":
            error = data.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        else:
            return ""
        return message if isinstance(message, str) else ""

    @staticmethod
    def _text_from_event(data: dict) -> str:
        item = data.get("item") if isinstance(data.get("item"), dict) else data
        if item.get("type") in {"agent_message", "message", "assistant"}:
            return str(item.get("text") or item.get("content") or "")
        return str(data.get("text") or "")

    def execute(self, request: WorkerRequest) -> WorkerResult:
        accepted, codes = self.accepts(request)
        if not accepted:
            return WorkerResult(request.request_id, self.descriptor.worker_id,
                                REJECTED, limitations=("request refused before execution",),
                                diagnostics=tuple({"code": c} for c in codes))
        # Availability is a property of the command that will actually run,
        # not of PATH discovery alone. The base implementation refuses exactly
        # as before, because it puts the discovered executable - or "" - in
        # argv[0]; a subclass that supplies its own argv (the documented
        # fixture seam) is no longer refused for lacking an unrelated binary.
        argv = self.build_argv(request)
        if not argv or not argv[0]:
            return WorkerResult(request.request_id, self.descriptor.worker_id,
                                FAILED, limitations=("the native worker is unavailable",),
                                diagnostics=({"code": "NATIVE_WORKER_UNAVAILABLE"},))
        prompt = render_prompt(request)
        events: list[str] = []
        diagnostics: list[dict] = []
        # Distinct structured failure details, in first-seen order. Appended
        # (never inserted) so TRANSPORT_OUTCOME and EXIT_CODE keep the leading
        # diagnostic positions the runtime derives reason_codes[0] from.
        failures: list[str] = []

        def on_line(stream: str, line: str):
            if stream == "stderr":
                # Stderr is intentionally not retained: launchers sometimes
                # include authentication or profile details in diagnostics.
                diagnostics.append({"code": "STDERR_PRESENT",
                                    "detail": "native stderr withheld"})
                return
            try:
                data = json.loads(line)
            except (TypeError, ValueError):
                if line:
                    events.append(line)
                return
            if isinstance(data, dict):
                text = self._text_from_event(data)
                if text:
                    events.append(text)
                failure = _error_detail(self._failure_text_from_event(data))
                if (failure and failure not in failures
                        and len(failures) < _MAX_ERROR_EVENTS):
                    # A structured failure is EVIDENCE, never worker output:
                    # it must not be able to become an answer.
                    failures.append(failure)
                    diagnostics.append({"code": WORKER_ERROR_EVENT,
                                        "detail": failure})
                usage = data.get("usage")
                if isinstance(usage, dict):
                    diagnostics.append({"code": "USAGE", "detail": json.dumps(usage, sort_keys=True)})

        self._process = NativeProcess(argv, self.cwd, prompt,
                                      self.timeout_s)
        outcome = self._process.run(on_line=on_line)
        diagnostics.insert(0, {"code": "TRANSPORT_OUTCOME", "detail": outcome.outcome})
        diagnostics.insert(1, {"code": "EXIT_CODE", "detail": str(outcome.exit_code)})
        limitations: list[str] = []
        if outcome.outcome == SPAWN_FAILED:
            status = FAILED
            limitations.append("the native worker could not be started")
            diagnostics.insert(0, {"code": "NATIVE_WORKER_UNAVAILABLE",
                                   "detail": outcome.error or "spawn failed"})
        elif outcome.outcome == TIMED_OUT:
            status = FAILED
            limitations.append("execution exceeded its timeout and was terminated")
        elif outcome.outcome == CANCELLED:
            status = FAILED
            limitations.append("execution was cancelled")
        elif outcome.exit_code == 0:
            status = COMPLETED
        else:
            status = FAILED
        return WorkerResult(
            request_id=request.request_id, worker_id=self.descriptor.worker_id,
            status=status, output=_scrub("\n".join(events)),
            usage={"prompt_chars": len(prompt),
                   "stdout_lines": len(outcome.stdout_lines),
                   "stderr_lines": len(outcome.stderr_lines),
                   "duration_ms": outcome.duration_ms},
            limitations=tuple(limitations),
            diagnostics=tuple(diagnostics))

    def cancel(self, request_id: str) -> bool:
        return self._process.cancel() if self._process else False
