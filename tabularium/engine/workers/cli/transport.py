"""Bounded, argv-only subprocess transport for the native worker."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

EXITED = "EXITED"
TIMED_OUT = "TIMED_OUT"
SPAWN_FAILED = "SPAWN_FAILED"
CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ProcessResult:
    outcome: str
    exit_code: int | None
    stdout_lines: tuple[str, ...]
    stderr_lines: tuple[str, ...]
    duration_ms: int
    error: str | None = None


# Operator opt-in: names listed in TABU_WORKER_ENV (comma separated) also
# cross the boundary, e.g. a provider key the worker's config reads with
# {env:NAME}. Names only, no wildcards; variables that change how a process
# loads code or how a worker is permitted are never passed.
WORKER_ENV_OPT_IN = "TABU_WORKER_ENV"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NEVER_PASSED_PREFIXES = ("LD_", "DYLD_", "PYTHON", "GIT_", "OPENCODE_", "NODE_",
                          "BASH_", "TABU_")
_NEVER_PASSED = frozenset({"ENV", "SHELLOPTS", "PS4", "PROMPT_COMMAND", "IFS",
                           "PERL5LIB", "PERL5OPT", "RUBYOPT", "RUBYLIB"})


def worker_env_passthrough(parent: dict[str, str] | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(names passed to workers, "NAME: reason" entries refused) from TABU_WORKER_ENV."""
    source = os.environ if parent is None else parent
    passed: list[str] = []
    refused: list[str] = []
    for raw in source.get(WORKER_ENV_OPT_IN, "").split(","):
        name = raw.strip()
        if not name:
            continue
        if not _ENV_NAME.fullmatch(name):
            refused.append(f"{name}: not a plain variable name")
        elif name.upper() in _NEVER_PASSED or name.upper().startswith(_NEVER_PASSED_PREFIXES):
            refused.append(f"{name}: changes how processes load code or are permitted")
        elif name not in source:
            refused.append(f"{name}: not set")
        elif name not in passed:
            passed.append(name)
    return tuple(passed), tuple(refused)


def _environment(parent: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if parent is None else parent
    # Only process-launch and local session-location variables cross the
    # boundary. Provider/API credential variables are deliberately absent
    # unless the operator names them in TABU_WORKER_ENV.
    names = (
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP",
        "TMP", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
        "PROGRAMDATA", "CODEX_HOME", "LANG", "LC_ALL",
    )
    env = {name: source[name] for name in names if name in source}
    passed, _refused = worker_env_passthrough(source)
    env.update({name: source[name] for name in passed})
    return env


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, shell=False, timeout=10)
        else:
            os.killpg(os.getpgid(process.pid), 9)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


class NativeProcess:
    def __init__(self, argv: Sequence[str], cwd: str, stdin_text: str,
                 timeout_s: float = 120.0) -> None:
        self.argv = list(argv)
        self.cwd = str(cwd)
        self.stdin_text = stdin_text
        self.timeout_s = timeout_s
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def cancel(self) -> bool:
        self._cancel.set()
        with self._lock:
            process = self._process
        if process is not None:
            _kill(process)
        return True

    def run(self, on_line: Callable[[str, str], None] | None = None
            ) -> ProcessResult:
        started = time.monotonic()
        if self._cancel.is_set():
            return ProcessResult(CANCELLED, None, (), (), 0)
        try:
            process = subprocess.Popen(
                self.argv, cwd=self.cwd, env=_environment(), shell=False,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               if sys.platform == "win32" else 0),
                preexec_fn=(None if sys.platform == "win32" else os.setsid))
        except (OSError, ValueError) as exc:
            return ProcessResult(SPAWN_FAILED, None, (), (), 0,
                                 type(exc).__name__)
        with self._lock:
            self._process = process
        out: list[str] = []
        err: list[str] = []

        def drain(stream, sink, name):
            try:
                for line in stream:
                    line = line.rstrip("\r\n")
                    sink.append(line)
                    if on_line:
                        on_line(name, line)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        readers = [threading.Thread(target=drain, args=(process.stdout, out,
                                                         "stdout"), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, err,
                                                         "stderr"), daemon=True)]
        for reader in readers:
            reader.start()
        try:
            process.stdin.write(self.stdin_text)
        except (OSError, ValueError):
            # The child exited without draining stdin. Not an error: the
            # outcome is decided by its exit code and output, not by whether
            # it read the prompt.
            pass
        finally:
            # Close even when the write failed. Leaving the stream open lets
            # the interpreter retry the same doomed flush at finalization,
            # which reaches the operator as an "Exception ignored while
            # finalizing file" traceback on an otherwise successful run.
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

        outcome = EXITED
        deadline = started + self.timeout_s if self.timeout_s else None
        while process.poll() is None:
            if self._cancel.is_set():
                outcome = CANCELLED
                _kill(process)
                break
            if deadline is not None and time.monotonic() > deadline:
                outcome = TIMED_OUT
                _kill(process)
                break
            time.sleep(0.01)
        for reader in readers:
            reader.join(timeout=5)
        return ProcessResult(outcome, process.poll(), tuple(out), tuple(err),
                             int((time.monotonic() - started) * 1000))
