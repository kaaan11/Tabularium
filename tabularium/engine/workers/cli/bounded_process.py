"""Bounded POSIX subprocess capture for the opt-in OpenCode adapter.

Process groups provide best-effort cleanup, not process/security containment.
Pipe readiness avoids background readers surviving an escaped descendant.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from .transport import _environment


@dataclass(frozen=True)
class Capture:
    outcome: str
    returncode: int | None
    stdout: str = ""
    stderr_present: bool = False
    capture_complete: bool = True
    stderr: str = ""


class BoundedProcess:
    def __init__(self, argv, cwd, timeout_s, max_output_bytes, env: dict[str, str] | None = None):
        self.argv, self.cwd = argv, cwd
        self.timeout_s, self.max_output_bytes = timeout_s, max_output_bytes
        self.env = _environment() if env is None else env
        self._cancelled = threading.Event()
        self._process = None

    def _stop(self):
        process = self._process
        if process is None:
            return True
        try:
            # Kill the group even if the leader exited with inherited pipes
            # held by a descendant. Escaped sessions are outside this boundary.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    def cancel(self):
        self._cancelled.set()
        return True

    def run(self):
        started = time.monotonic()
        if sys.platform == "win32":
            return Capture("UNSUPPORTED_PLATFORM", None)
        if self._cancelled.is_set():
            return Capture("CANCELLED", None)
        try:
            process = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
        except (OSError, ValueError):
            return Capture("SPAWN_FAILED", None)
        self._process = process
        selector = selectors.DefaultSelector()
        total = 0
        stdout = bytearray()
        stderr_bytes = bytearray()
        stderr_present = False
        outcome = "EXITED"
        capture_complete = False
        try:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while process.poll() is None or selector.get_map():
                if self._cancelled.is_set():
                    outcome = "CANCELLED"
                    break
                if time.monotonic() - started >= self.timeout_s:
                    outcome = "TIMEOUT"
                    break
                for key, _ in selector.select(timeout=0.01):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = max(0, self.max_output_bytes - total)
                    if key.fileobj is process.stdout:
                        stdout.extend(chunk[:remaining])
                    else:
                        stderr_present = True
                        stderr_bytes.extend(chunk[:remaining])
                    total += len(chunk)
                    if total > self.max_output_bytes:
                        outcome = "OUTPUT_LIMIT"
                        break
                if outcome != "EXITED":
                    break
            capture_complete = not selector.get_map()
        except (OSError, ValueError):
            outcome = "CAPTURE_FAILED"
        finally:
            cleaned = self._stop()
            selector.close()
            process.stdout.close()
            process.stderr.close()
            self._process = None
            if not cleaned:
                outcome = "CLEANUP_FAILED"
        return Capture(outcome, process.returncode,
                       bytes(stdout).decode("utf-8", errors="replace"),
                       stderr_present, capture_complete,
                       bytes(stderr_bytes).decode("utf-8", errors="replace"))
