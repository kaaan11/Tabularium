"""Build the approved test overlay and run configured checks in isolation."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

def run_configured_tests(
    target: Path,
    change_config: Any,
    file_patches: tuple[Any, ...] | list[Any],
    *,
    cancel_candidate: Callable[[str], None],
    public_api: Any,
) -> tuple[dict[str, Any] | None, float, tuple[str, str] | None]:
    """Run tests against the exact patch overlay and return test data or refusal."""
    from tabularium.engine.code_gate.parser import apply_patch_to_text

    overlay: dict[str, str] = {}
    for file_patch in file_patches:
        rel = file_patch.normalized_path
        dest = Path(target) / rel
        try:
            original = dest.read_text(encoding="utf-8") if dest.exists() else ""
            overlay[rel] = apply_patch_to_text(original, file_patch.hunks)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            cancel_candidate("DIFF_UNSUPPORTED")
            return None, 0.0, (
                "DIFF_UNSUPPORTED",
                f"{rel}: the approved diff could not be staged for the isolated "
                f"test copy ({type(exc).__name__}); nothing was applied.",
            )

    cmd_display = " ".join(change_config.test_command)
    print(public_api.sanitize_terminal(f"Running tests in an isolated copy: {cmd_display}"))
    t0 = time.monotonic()
    try:
        run_res = public_api.run_tests_on_copy(
            target,
            overlay,
            public_api.resolve_test_command(target, change_config.test_command),
            timeout=float(change_config.timeout_seconds),
        )
        test_duration = time.monotonic() - t0
    except (public_api.SandboxError, OSError) as exc:
        test_duration = time.monotonic() - t0
        code = (exc.code if isinstance(exc, public_api.SandboxError)
                else "SANDBOX_UNAVAILABLE")
        detail = (exc.detail if isinstance(exc, public_api.SandboxError)
                  else f"{type(exc).__name__}: {exc}")
        cancel_candidate(code)
        return {
            "status": "FAILED",
            "command": list(change_config.test_command),
            "duration_s": test_duration,
            "timed_out": False,
            "truncated": False,
            "untested_override": False,
        }, test_duration, (code, f"Sandbox error ({code}): {detail}")

    if not run_res.passed:
        code = "ISOLATED_TESTS_FAILED"
        cancel_candidate(code)
        print("--- Test output ---")
        print(public_api.sanitize_terminal(run_res.output()))
        print("-------------------")
        if run_res.truncated:
            print("Warning: test output was truncated.")

        if run_res.timed_out:
            message = (f"Isolated tests timed out after {change_config.timeout_seconds}s; "
                       "change was not applied.")
        else:
            message = (f"Isolated tests failed (returncode {run_res.returncode}); "
                       "change was not applied.")

        return {
            "status": "FAILED",
            "command": list(change_config.test_command),
            "duration_s": test_duration,
            "timed_out": run_res.timed_out,
            "truncated": run_res.truncated,
            "untested_override": False,
        }, test_duration, (code, message)

    return {
        "status": "PASSED",
        "command": list(change_config.test_command),
        "duration_s": test_duration,
        "timed_out": False,
        "truncated": run_res.truncated,
        "untested_override": False,
    }, test_duration, None
