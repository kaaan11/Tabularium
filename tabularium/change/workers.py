"""Worker policy and selection for file-editing change runs."""

from __future__ import annotations

import os
from typing import Any

_API_WORKER_NAMES = frozenset({"api", "api-model", "litellm"})


def select_change_worker(
    worker_name: str | None,
    choice: str,
    model: str | None,
    adapter: Any,
    worktree_dir,
    *,
    public_api: Any,
) -> tuple[Any | None, tuple[str, str] | None]:
    """Select and configure the worker that edits the detached worktree."""
    if adapter is not None:
        selected_worker = adapter
        if hasattr(selected_worker, "cwd"):
            selected_worker.cwd = str(worktree_dir)
        return selected_worker, None

    selected_model = (model or os.environ.get("TABU_MODEL") or "").strip()
    if choice in {"native", "codex", "codex-cli"}:
        exe = public_api.find_native_executable()
        if not exe:
            return None, (
                "WORKER_UNAVAILABLE",
                "The selected native worker is unavailable.",
            )
        return public_api.NativeCLIWorker(
            executable=exe, cwd=worktree_dir, sandbox="workspace-write"
        ), None
    if choice in {"opencode", "opencode-cli"}:
        exe = public_api.find_opencode_executable()
        if not exe:
            return None, (
                "WORKER_UNAVAILABLE",
                "The selected OpenCode worker is unavailable.",
            )
        return public_api.OpenCodeCLIWorker(
            executable=exe,
            model=selected_model or None,
            cwd=worktree_dir,
            change_mode=True,
        ), None
    if choice:
        return None, (
            "WORKER_SELECTION_INVALID",
            f"Unknown worker '{worker_name}'; choose native or opencode.",
        )

    exe = public_api.find_native_executable()
    if exe:
        return public_api.NativeCLIWorker(
            executable=exe, cwd=worktree_dir, sandbox="workspace-write"
        ), None

    opencode_bin = public_api.find_opencode_executable()
    if opencode_bin:
        return public_api.OpenCodeCLIWorker(
            executable=opencode_bin,
            model=selected_model or None,
            cwd=worktree_dir,
            change_mode=True,
        ), None
    return None, (
        "WORKER_SELECTION_REQUIRED",
        "No suitable worker available for --change: install the "
        "Codex CLI or OpenCode (--worker api cannot edit files).",
    )
