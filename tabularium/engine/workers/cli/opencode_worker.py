"""Headless OpenCode CLI worker adapter (DEC-2026-0007).

Executes governed requests through the local OpenCode CLI (`opencode run`)
using structured JSON stream output (`--format json --pure`).
Utilizes existing user-configured subscriptions (e.g. OpenAI, OpenRouter, Claude)
without requiring raw per-token billed API keys.
"""

from __future__ import annotations

import json
import os
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from tabularium.engine.workers import (CLI_CODING, COMPLETED, FAILED, REJECTED,
                             WorkerDescriptor, WorkerRequest, WorkerResult,
                             check_acceptance)
from tabularium.engine.workers.model import CANCELLABLE, STREAMS_OUTPUT, STRUCTURED_OUTPUT

from .prompt import render_prompt
from .bounded_process import BoundedProcess
from .transport import _environment
from .worker import _scrub

OPENCODE_WORKER_ID = "opencode-cli"
_DEFAULT_PATHS = (
    Path.home() / ".opencode" / "bin" / "opencode",
)


def find_opencode_executable() -> str | None:
    """Locate the opencode binary on the system."""
    configured = os.environ.get("TABU_OPENCODE_EXECUTABLE", "").strip()
    if configured:
        p = Path(configured).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None

    # Check PATH
    found = shutil.which("opencode")
    if found:
        return found

    # Check standard local install paths
    for candidate in _DEFAULT_PATHS:
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


class OpenCodeCLIWorker:
    """A governed OpenCode CLI worker adapter implementing the WorkerAdapter protocol."""

    def __init__(self,
                 executable: str | None = None,
                 model: str | None = None,
                 cwd: str | Path | None = None,
                 timeout_s: float = 180.0,
                 worker_id: str = OPENCODE_WORKER_ID,
                 max_output_bytes: int = 1024 * 1024,
                 change_mode: bool = False,
                 permissions: dict[str, Any] | str | None = None) -> None:
        self.executable = executable or find_opencode_executable()
        self.model = model
        self.change_mode = change_mode
        self.permissions = permissions
        if self.change_mode and self.permissions is None:
            self.permissions = {
                # Allowlist: anything not named here (task subagents, web
                # fetch/search, questions, new tools) is denied and hidden
                # from the model instead of failing the run with an "ask".
                "*": "deny",
                "read": "allow",
                "list": "allow",
                "glob": "allow",
                "grep": "allow",
                "edit": "allow",
                "bash": "deny",
                # Named as well: a project agent config can re-enable tools
                # that only the "*" rule covers (reproduced for task/webfetch).
                "task": "deny",
                "webfetch": "deny",
                "websearch": "deny",
                "skill": "deny",
                "question": "deny",
                "todowrite": "deny",
                # Without this a project opencode.json can turn the "ask"
                # default into "allow" and let edits land outside --dir.
                "external_directory": "deny",
            }
        self._scratch = None
        if cwd is None:
            self._scratch = tempfile.TemporaryDirectory(prefix="tabu-opencode-")
            cwd = self._scratch.name
        self.cwd = str(cwd)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        if not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self._current_process: BoundedProcess | None = None
        self._descriptor = WorkerDescriptor(
            worker_id=worker_id,
            category=CLI_CODING,
            capabilities=(STREAMS_OUTPUT, STRUCTURED_OUTPUT, CANCELLABLE),
        )

    @property
    def descriptor(self) -> WorkerDescriptor:
        return self._descriptor

    def accepts(self, request: WorkerRequest) -> tuple[bool, tuple[str, ...]]:
        return check_acceptance(self._descriptor, request)

    def build_argv(self, prompt: str) -> list[str]:
        cmd = [self.executable or "opencode", "run", prompt, "--format", "json", "--pure", "--dir", self.cwd]
        if self.model:
            cmd.extend(["-m", self.model])
        return cmd

    def execute(self, request: WorkerRequest) -> WorkerResult:
        """Run the governed request through opencode run."""
        accepted, reasons = self.accepts(request)
        if not accepted:
            return WorkerResult(
                request_id=request.request_id,
                status=REJECTED,
                output="",
                worker_id=self.descriptor.worker_id,
                diagnostics=tuple({"code": r, "detail": "acceptance failed"} for r in reasons),
            )

        if not self.executable or not Path(self.executable).is_file():
            return WorkerResult(
                request_id=request.request_id,
                status=FAILED,
                output="",
                worker_id=self.descriptor.worker_id,
                diagnostics=({"code": "EXECUTABLE_NOT_FOUND", "detail": "opencode executable unavailable"},),
            )

        prompt = render_prompt(request)
        argv = self.build_argv(prompt)

        out_lines: list[str] = []
        usage: dict[str, int] = {}
        diagnostics: list[dict[str, Any]] = []

        # Scrub recognizable credentials and exact secret values from the
        # parent environment before anything enters WorkerResult/checkpoints.
        secrets = tuple(value for key, value in os.environ.items()
                        if len(value) >= 8 and re.search(
                            r"(?i)(key|token|secret|password|credential)", key))

        def scrub(text):
            for value in sorted(secrets, key=len, reverse=True):
                text = text.replace(value, "[REDACTED]")
            return _scrub(text)

        env = _environment()
        # The project's opencode.json is repository content, not operator
        # configuration: it can start local MCP servers (commands run as the
        # user when the worker starts) and widen permissions. Never load it.
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        if self.permissions:
            env["OPENCODE_PERMISSION"] = (
                self.permissions if isinstance(self.permissions, str)
                else json.dumps(self.permissions)
            )

        try:
            process = BoundedProcess(argv, self.cwd, self.timeout_s, self.max_output_bytes, env=env)
            self._current_process = process
            captured = process.run()
            if captured.outcome != "EXITED":
                detail = "OpenCode process did not complete within its bounds"
                if not captured.capture_complete:
                    detail += "; output capture incomplete and discarded"
                return WorkerResult(
                    request_id=request.request_id, status=FAILED, output="",
                    worker_id=self.descriptor.worker_id,
                    diagnostics=({"code": captured.outcome,
                                  "detail": detail},),
                )
            if captured.stderr_present:
                stderr_text = getattr(captured, "stderr", "")
                perm_line = ""
                if stderr_text:
                    for line in stderr_text.splitlines():
                        if "permission requested:" in line and "auto-rejecting" in line:
                            perm_line = line.strip()
                            break
                if perm_line:
                    diagnostics.append({
                        "code": "WORKER_PERMISSION_DENIED",
                        "detail": scrub(perm_line)[:300],
                    })
                else:
                    diagnostics.append({"code": "STDERR_PRESENT", "detail": "OpenCode stderr withheld"})
            if captured.returncode != 0:
                diagnostics.append({"code": "OPENCODE_PROCESS_ERROR",
                                    "detail": f"Exit code {captured.returncode}"})
            stdout = captured.stdout

            # Parse JSON stream lines
            for line in stdout.splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    evt_type = event.get("type")
                    part = event.get("part")
                    part = part if isinstance(part, dict) else {}

                    if evt_type == "text":
                        text_val = part.get("text")
                        if isinstance(text_val, str) and text_val:
                            out_lines.append(text_val)

                    elif evt_type == "step_finish":
                        toks = part.get("tokens") or {}
                        if isinstance(toks, dict):
                            usage = {
                                f"{name}_tokens": value if type(value) is int and value >= 0 else 0
                                for name, value in (("input", toks.get("input", 0)),
                                                    ("output", toks.get("output", 0)),
                                                    ("total", toks.get("total", 0)))
                            }

                    elif evt_type == "error":
                        diagnostics.append({
                            "code": "OPENCODE_STREAM_ERROR",
                            "detail": scrub(str(event.get("message") or part.get("message") or "stream error"))[:300],
                        })

                except json.JSONDecodeError:
                    continue

            output_text = scrub("".join(out_lines).strip())
            stream_error = any(d["code"] == "OPENCODE_STREAM_ERROR" for d in diagnostics)
            perm_denied = any(d["code"] == "WORKER_PERMISSION_DENIED" for d in diagnostics)
            status = COMPLETED if (output_text and captured.returncode == 0 and not stream_error and not perm_denied) else FAILED

            return WorkerResult(
                request_id=request.request_id,
                status=status,
                output=output_text,
                worker_id=self.descriptor.worker_id,
                usage=usage,
                diagnostics=tuple(diagnostics),
            )

        except Exception as exc:
            return WorkerResult(
                request_id=request.request_id,
                status=FAILED,
                output="",
                worker_id=self.descriptor.worker_id,
                diagnostics=({"code": "SPAWN_FAILED", "detail": type(exc).__name__},),
            )
        finally:
            self._current_process = None

    def cancel(self, request_id: str) -> bool:
        return self._current_process.cancel() if self._current_process else False
