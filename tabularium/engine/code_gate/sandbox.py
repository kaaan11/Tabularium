"""Experimental single-file patch staging and atomic replacement.

Verification runs on a full copy of the project with the patched file in
place. A ``test_command`` runs under bubblewrap (see ``isolated_run``) and is
refused when isolation is unavailable; a ``custom_verifier`` is trusted
operator code and runs with normal privileges. Multi-file patches and
deletions are unsupported and fail closed. The caller must ensure exclusive
project access during verification/commit.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from .gate import evaluate_patch
from .isolated_run import SandboxError, build_tree, copy_holds, run_isolated
from .model import (
    APPLIED,
    DENIED,
    ISOLATED_TESTS_FAILED,
    PATCH_APPLY_FAILED,
    PATCH_AUTHORIZATION_REQUIRED,
    PATCH_SCOPE_UNSUPPORTED,
    SANDBOX_UNAVAILABLE,
    VERIFICATION_REQUIRED,
    TARGET_CHANGED,
    TESTS_FAILED,
    PatchAuthorization,
    PatchProposal,
    PatchResult,
    digest,
)
from .parser import _normalize_patch_path, apply_patch_to_text, parse_unified_diff


def _atomic_write(target_path: Path, content: str) -> None:
    """Atomic same-directory replacement."""
    directory = target_path.parent
    handle, temp_file = tempfile.mkstemp(dir=str(directory), prefix=".tabu-patch-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            if target_path.exists() and hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), stat.S_IMODE(target_path.stat().st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_file, str(target_path))
    except BaseException:
        try:
            os.unlink(temp_file)
        except OSError:
            pass
        raise


def apply_patch_transactional(
    project_root: str | Path,
    proposal: PatchProposal,
    authorization: PatchAuthorization | dict[str, Any] | None = None,
    *,
    test_command: list[str] | None = None,
    custom_verifier: Callable[[Path], tuple[bool, str]] | None = None,
) -> PatchResult:
    """Verify and atomically replace one authorized text file; see module limits."""
    root = Path(project_root).resolve()

    # Step 1: Human Authorization and Security Gate Evaluation
    if authorization is None:
        return PatchResult(
            status=DENIED,
            proposal_id=proposal.proposal_id,
            reason_codes=(PATCH_AUTHORIZATION_REQUIRED,),
            diagnostics=({"code": PATCH_AUTHORIZATION_REQUIRED, "detail": "human authorization required to apply patch"},),
        )

    evaluation = evaluate_patch(proposal, root, authorization=authorization)
    if not evaluation.allowed:
        return PatchResult(
            status=DENIED,
            proposal_id=proposal.proposal_id,
            reason_codes=evaluation.reason_codes,
            diagnostics=tuple({"code": c.reason_code, "detail": c.detail} for c in evaluation.failed),
        )

    parsed_auth = (
        authorization
        if isinstance(authorization, PatchAuthorization)
        else PatchAuthorization.from_dict(authorization)
    )

    file_patches = parse_unified_diff(proposal.raw_diff)

    if len(file_patches) != 1 or file_patches[0].is_deletion:
        return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                           reason_codes=(PATCH_SCOPE_UNSUPPORTED,),
                           diagnostics=({"code": PATCH_SCOPE_UNSUPPORTED,
                                         "detail": "only one text update/creation is supported; no deletions or multi-file transaction"},))
    if (custom_verifier is None and not test_command) or (custom_verifier is not None and test_command is not None):
        return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                           reason_codes=(VERIFICATION_REQUIRED,),
                           diagnostics=({"code": VERIFICATION_REQUIRED,
                                         "detail": "provide exactly one trusted verifier or nonempty test command"},))
    target = root / file_patches[0].normalized_path
    fp = file_patches[0]
    if (fp.is_creation == target.exists()
            or (not fp.is_creation and _normalize_patch_path(fp.source_path) != fp.normalized_path)):
        return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                           reason_codes=(PATCH_SCOPE_UNSUPPORTED,))
    if not target.parent.is_dir() or (target.exists() and not target.is_file()):
        return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                           reason_codes=(PATCH_SCOPE_UNSUPPORTED,))
    try:
        original_bytes = target.read_bytes() if target.exists() else None
    except OSError:
        return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                           reason_codes=(PATCH_APPLY_FAILED,))

    # Step 2 & 3: scratch setup and patch application
    with tempfile.TemporaryDirectory(prefix="tabu-sandbox-") as sandbox_dir:
        sandbox_path = Path(sandbox_dir)

        # Prepare staged contents for each target file
        staged_contents: dict[str, str] = {}
        for fp in file_patches:
            try:
                orig_text = original_bytes.decode("utf-8") if original_bytes is not None else ""
                new_text = apply_patch_to_text(orig_text, fp.hunks)
            except Exception as exc:
                return PatchResult(
                    status=DENIED,
                    proposal_id=proposal.proposal_id,
                    reason_codes=(PATCH_APPLY_FAILED,),
                    diagnostics=({"code": PATCH_APPLY_FAILED, "detail": f"failed to apply diff to {fp.normalized_path}: {exc}"},),
                )
            staged_contents[fp.normalized_path] = new_text

        # Full project copy with the patched file in place.
        try:
            build_tree(root, sandbox_path, staged_contents)
        except (SandboxError, OSError) as exc:
            code = exc.code if isinstance(exc, SandboxError) else PATCH_APPLY_FAILED
            return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                               reason_codes=(code,),
                               diagnostics=({"code": code, "detail": f"project copy failed: {exc}"},))

        # Step 4: verification on the copy.
        test_output = ""
        if custom_verifier is not None:
            try:
                passed, test_output = custom_verifier(sandbox_path)
            except Exception as exc:
                return PatchResult(status=TESTS_FAILED, proposal_id=proposal.proposal_id,
                                   reason_codes=(ISOLATED_TESTS_FAILED,),
                                   diagnostics=({"code": ISOLATED_TESTS_FAILED,
                                                 "detail": f"verifier raised {type(exc).__name__}"},))
            if passed is not True:
                return PatchResult(
                    status=TESTS_FAILED,
                    proposal_id=proposal.proposal_id,
                    reason_codes=(ISOLATED_TESTS_FAILED,),
                    test_output=test_output,
                    diagnostics=({"code": ISOLATED_TESTS_FAILED, "detail": "custom verifier reported failure"},),
                )
        elif test_command is not None:
            try:
                run = run_isolated(sandbox_path, list(test_command), hidden=(root,))
                test_output = run.output()
                if not run.passed:
                    detail = ("test command timed out" if run.timed_out
                              else f"test command failed with exit code {run.returncode}")
                    return PatchResult(
                        status=TESTS_FAILED,
                        proposal_id=proposal.proposal_id,
                        reason_codes=(ISOLATED_TESTS_FAILED,),
                        test_output=test_output,
                        diagnostics=({"code": ISOLATED_TESTS_FAILED, "detail": detail},),
                    )
            except SandboxError as exc:
                return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                                   reason_codes=(exc.code,),
                                   diagnostics=({"code": exc.code, "detail": exc.detail},))
            except Exception as exc:
                # A failing test is a return code; an exception here is the
                # runner itself (disk full, fd limit), not the patch.
                return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                                   reason_codes=(SANDBOX_UNAVAILABLE,),
                                   diagnostics=({"code": SANDBOX_UNAVAILABLE,
                                                 "detail": f"test runner error: {type(exc).__name__}: {exc}"},))

        # A verifier must test the proposed bytes, not replace them after the
        # content-bound authorization. Reject stale targets before commit.
        try:
            current_bytes = target.read_bytes() if target.exists() else None
            rel = file_patches[0].normalized_path
            if (current_bytes != original_bytes
                    or not copy_holds(sandbox_path, rel, staged_contents[rel].encode("utf-8"))):
                return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                                   reason_codes=(TARGET_CHANGED,))
            final_gate = evaluate_patch(proposal, root, authorization=authorization)
            if not final_gate.allowed:
                return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                                   reason_codes=final_gate.reason_codes)
            _atomic_write(target, staged_contents[file_patches[0].normalized_path])
        except (OSError, ValueError) as exc:
            return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                               reason_codes=(PATCH_APPLY_FAILED,),
                               diagnostics=({"code": PATCH_APPLY_FAILED,
                                             "detail": f"commit failed: {type(exc).__name__}"},))
        modified = [file_patches[0].normalized_path]

        return PatchResult(
            status=APPLIED,
            proposal_id=proposal.proposal_id,
            modified_files=tuple(modified),
            authorized_by=parsed_auth.authorized_by if parsed_auth else "",
            authorized_at=parsed_auth.authorized_at if parsed_auth else "",
            resulting_digest=digest("".join(staged_contents.values())),
            test_output=test_output,
        )
