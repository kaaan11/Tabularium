"""Single-decision approval, target recheck, authorization, and apply."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def request_change_approval(
    confirm: Callable[[str], bool] | None,
    target_files: tuple[str, ...],
    proposal: Any,
    *,
    public_api: Any,
) -> bool:
    """Show the reviewed proposal and collect its one human decision."""
    if confirm is not None:
        return confirm("Uygula? [y/N]")

    print(public_api.sanitize_terminal(
        f"Target files ({len(target_files)}): {', '.join(target_files)}"
    ))
    print(f"Gate evaluation: PASSED (diff_digest={proposal.diff_digest})")
    print("--- Diff preview ---")
    print(public_api.sanitize_terminal(proposal.raw_diff))
    print("--------------------")
    try:
        response = input("Uygula? [y/N]: ").strip().casefold()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def authorize_and_apply(
    target: Path,
    proposal: Any,
    base_commit: str,
    config_digest: str,
    journal: Path,
    target_files: tuple[str, ...],
    worktree_dir: Path,
    *,
    pre_apply_hook: Callable[[], None] | None,
    custom_verifier: Callable[[dict[str, str]], tuple[bool, str]] | None,
    public_api: Any,
) -> tuple[str, str, bool, Any]:
    """Recheck target/config, bind authorization to the approved proposal, then apply it."""
    actor = public_api.get_git_user_name(target) or os.environ.get("USER", "unknown")
    now_iso = datetime.now(timezone.utc).isoformat()

    # Keep the compare-and-swap check after tests and approval, immediately before authorization.
    clean_now, _clean_code, current_head = public_api.check_git_clean(target)
    if (not clean_now or current_head != base_commit
            or public_api.config_digest(target) != config_digest):
        return actor, now_iso, True, None

    authorization = public_api.PatchAuthorization(
        proposal_id=proposal.proposal_id,
        execution_id=proposal.execution_id,
        diff_digest=proposal.diff_digest,
        authorized_by=actor,
        authorized_at=now_iso,
        binding=proposal.binding(),
        note="Single confirmation change authorization",
    )

    def verifier_wrapper(staged: dict[str, str]) -> tuple[bool, str]:
        if pre_apply_hook is not None:
            pre_apply_hook()
        if custom_verifier is not None:
            return custom_verifier(staged)
        # Every staged file must reproduce exactly what the worker wrote in
        # the worktree; anything else means the diff did not round-trip.
        for rel in target_files:
            produced = worktree_dir / rel
            if rel not in staged or not produced.is_file():
                return False, f"{rel}: staged or worker file missing"
            if staged[rel].encode("utf-8") != produced.read_bytes():
                return False, f"{rel}: staged content differs from worker output"
        return True, "staged content matches worker output"

    apply_result = public_api.apply_changeset_transactional(
        target,
        proposal,
        authorization,
        verifier=verifier_wrapper,
        journal_path=journal,
    )
    return actor, now_iso, False, apply_result
