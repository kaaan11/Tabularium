"""All-or-nothing application of a multi-file patch (V2-04, first part).

`apply_patch_transactional` (sandbox.py) applies exactly one file. This module
applies a change set of up to MAX_FILES updates and creations:

1. Every file is planned before anything is written: compare-and-swap base
   (current bytes), staged text, and the caller's verifier over all staged
   files. Any refusal here leaves the project untouched.
2. A journal listing every path and its original digest is written and
   fsynced before the first replacement, so an interrupted run is detectable.
3. Files are replaced one by one; each is re-checked against its base right
   before its replacement. On any failure every replaced file is restored,
   created files and directories are removed, and the journal is deleted.

Deletions and renames are unsupported and fail closed. Verification here is
the caller's verifier over the staged bytes; running project tests in an
isolated full tree is the second part of V2-04 and not done here.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .gate import evaluate_patch
from .model import (
    APPLIED,
    DENIED,
    ISOLATED_TESTS_FAILED,
    PATCH_APPLY_FAILED,
    PATCH_AUTHORIZATION_REQUIRED,
    PATCH_SCOPE_UNSUPPORTED,
    TARGET_CHANGED,
    TESTS_FAILED,
    PatchAuthorization,
    PatchProposal,
    PatchResult,
    digest,
)
from .parser import _normalize_patch_path, apply_patch_to_text, parse_unified_diff

MAX_FILES = 20
CHANGESET_TOO_LARGE = "CHANGESET_TOO_LARGE"
INTERRUPTED_APPLY = "INTERRUPTED_APPLY"
ROLLBACK_FAILED = "ROLLBACK_FAILED"


@dataclass
class _Planned:
    rel: str
    dest: Path
    original: bytes | None          # None: the file is created
    staged: str
    mode: int | None = None         # permission bits of the original file
    temp: Path | None = None
    replaced: bool = False


@dataclass
class _Undo:
    created_dirs: list[Path] = field(default_factory=list)


def _default_mode() -> int:
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _sha256(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _denied(proposal: PatchProposal, code: str, detail: str = "") -> PatchResult:
    diagnostics = ({"code": code, "detail": detail},) if detail else ()
    return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                       reason_codes=(code,), diagnostics=diagnostics)


def _current(dest: Path) -> bytes | None:
    if not os.path.lexists(dest):
        return None
    if dest.is_symlink() or not dest.is_file():
        raise ValueError("not a regular file")
    return dest.read_bytes()


def _plan(root: Path, proposal: PatchProposal) -> tuple[list[_Planned], PatchResult | None]:
    file_patches = parse_unified_diff(proposal.raw_diff)
    if not file_patches:
        return [], _denied(proposal, PATCH_SCOPE_UNSUPPORTED, "empty change set")
    if len(file_patches) > MAX_FILES:
        return [], _denied(proposal, CHANGESET_TOO_LARGE,
                           f"{len(file_patches)} files; at most {MAX_FILES}")
    seen: set[str] = set()
    planned: list[_Planned] = []
    for fp in file_patches:
        rel = fp.normalized_path
        key = unicodedata.normalize("NFC", rel).casefold()
        if key in seen:
            return [], _denied(proposal, PATCH_SCOPE_UNSUPPORTED, f"{rel} appears twice")
        seen.add(key)
        if fp.is_deletion:
            return [], _denied(proposal, PATCH_SCOPE_UNSUPPORTED, f"{rel}: deletion")
        if not fp.is_creation and _normalize_patch_path(fp.source_path) != rel:
            return [], _denied(proposal, PATCH_SCOPE_UNSUPPORTED, f"{rel}: rename")
        dest = root / rel
        # Every existing ancestor inside the project must be a real directory.
        for parent in dest.parents:
            if parent == root or not parent.is_relative_to(root):
                break
            if os.path.lexists(parent) and (parent.is_symlink() or not parent.is_dir()):
                return [], _denied(proposal, PATCH_SCOPE_UNSUPPORTED,
                                   f"{rel}: {parent.relative_to(root)} is not a directory")
        try:
            original = _current(dest)
        except (OSError, ValueError) as exc:
            return [], _denied(proposal, PATCH_SCOPE_UNSUPPORTED, f"{rel}: {exc}")
        if fp.is_creation != (original is None):
            return [], _denied(proposal, PATCH_SCOPE_UNSUPPORTED,
                               f"{rel}: {'already exists' if fp.is_creation else 'does not exist'}")
        try:
            text = original.decode("utf-8") if original is not None else ""
            staged = apply_patch_to_text(text, fp.hunks)
        except (UnicodeDecodeError, ValueError) as exc:
            return [], _denied(proposal, PATCH_APPLY_FAILED, f"{rel}: {type(exc).__name__}")
        mode = stat.S_IMODE(dest.stat().st_mode) if original is not None else None
        planned.append(_Planned(rel, dest, original, staged, mode))
    return planned, None


def _confined(root: Path, dest: Path) -> bool:
    """No existing ancestor inside root is a symlink or non-directory."""
    for parent in dest.parents:
        if parent == root or not parent.is_relative_to(root):
            return True
        if os.path.lexists(parent) and (parent.is_symlink() or not parent.is_dir()):
            return False
    return True


def _write_temp(root: Path, item: _Planned, undo: _Undo) -> None:
    # Re-checked here, not only at planning: a directory swapped for a
    # symlink in between would otherwise receive the temp file.
    if not _confined(root, item.dest):
        raise _Changed(item.rel)
    missing = [p for p in reversed(item.dest.parents) if not os.path.lexists(p)]
    for directory in missing:
        directory.mkdir()
        undo.created_dirs.append(directory)
    handle, name = tempfile.mkstemp(dir=str(item.dest.parent), prefix=".tabu-patch-",
                                    suffix=".tmp")
    item.temp = Path(name)
    with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
        if hasattr(os, "fchmod"):
            # Updates keep their mode; creations get the umask default, not
            # mkstemp's 0o600.
            os.fchmod(stream.fileno(), item.mode if item.mode is not None else _default_mode())
        stream.write(item.staged)
        stream.flush()
        os.fsync(stream.fileno())


def _restore(item: _Planned) -> None:
    if item.original is None:
        try:
            os.unlink(item.dest)
        except FileNotFoundError:
            pass  # the replace never happened
        return
    handle, name = tempfile.mkstemp(dir=str(item.dest.parent), prefix=".tabu-restore-",
                                    suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            if item.mode is not None and hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), item.mode)
            stream.write(item.original)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, item.dest)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _rollback(planned: list[_Planned], undo: _Undo) -> list[str]:
    """Undo everything; returns the paths that could not be restored."""
    failed: list[str] = []
    for item in reversed(planned):
        if item.replaced:
            try:
                _restore(item)
            except OSError:
                failed.append(item.rel)
        if item.temp is not None and os.path.lexists(item.temp):
            try:
                os.unlink(item.temp)
            except OSError:
                failed.append(str(item.temp))
    for directory in reversed(undo.created_dirs):
        try:
            directory.rmdir()
        except OSError:
            failed.append(str(directory))
    return failed


def apply_changeset_transactional(
    project_root: str | Path,
    proposal: PatchProposal,
    authorization: PatchAuthorization | dict[str, Any] | None,
    *,
    verifier: Callable[[dict[str, str]], tuple[bool, str]],
    journal_path: str | Path | None = None,
) -> PatchResult:
    """Apply every file of an authorized change set, or none of them."""
    root = Path(project_root).resolve()
    if authorization is None:
        return _denied(proposal, PATCH_AUTHORIZATION_REQUIRED,
                       "human authorization required to apply patch")
    evaluation = evaluate_patch(proposal, root, authorization=authorization)
    if not evaluation.allowed:
        return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                           reason_codes=evaluation.reason_codes)
    parsed_auth = (authorization if isinstance(authorization, PatchAuthorization)
                   else PatchAuthorization.from_dict(authorization))

    planned, refusal = _plan(root, proposal)
    if refusal is not None:
        return refusal

    try:
        passed, detail = verifier({item.rel: item.staged for item in planned})
    except Exception as exc:  # noqa: BLE001 - a verifier crash is a failed verification
        passed, detail = False, f"verifier raised {type(exc).__name__}"
    if passed is not True:
        return PatchResult(status=TESTS_FAILED, proposal_id=proposal.proposal_id,
                           reason_codes=(ISOLATED_TESTS_FAILED,), test_output=str(detail))

    final_gate = evaluate_patch(proposal, root, authorization=authorization)
    if not final_gate.allowed:
        return PatchResult(status=DENIED, proposal_id=proposal.proposal_id,
                           reason_codes=final_gate.reason_codes)

    journal = Path(journal_path) if journal_path is not None else None
    if journal is not None:
        payload = {"proposal_id": proposal.proposal_id, "root": str(root),
                   "files": [{"path": item.rel, "created": item.original is None,
                              "original_sha256": _sha256(item.original)}
                             for item in planned]}
        try:
            # "x": an existing journal is an interrupted earlier apply; it is
            # evidence and must never be overwritten or removed here.
            stream = open(journal, "x", encoding="utf-8")
        except FileExistsError:
            return _denied(proposal, INTERRUPTED_APPLY, f"apply journal exists: {journal}")
        except OSError as exc:
            return _denied(proposal, PATCH_APPLY_FAILED, f"journal: {type(exc).__name__}")
        try:
            with stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            # This run created the file and nothing was applied yet: a partial
            # journal must not lock later runs.
            _unlink_quietly(journal)
            return _denied(proposal, PATCH_APPLY_FAILED, f"journal: {type(exc).__name__}")
    undo = _Undo()
    try:
        for item in planned:
            _write_temp(root, item, undo)
        for item in planned:
            # Compare-and-swap: the file must still be what was planned.
            if _current(item.dest) != item.original or not _confined(root, item.dest):
                raise _Changed(item.rel)
            assert item.temp is not None
            # Marked before the replace: an interrupt between os.replace and
            # this line would otherwise leave a replaced file un-restored.
            item.replaced = True
            os.replace(item.temp, item.dest)
            item.temp = None
    except (OSError, ValueError, _Changed) as exc:
        failed = _rollback(planned, undo)
        if failed:
            # The journal stays so the interrupted state is visible.
            return _denied(proposal, ROLLBACK_FAILED, "could not restore: " + ", ".join(failed))
        if journal is not None:
            _unlink_quietly(journal)
        if isinstance(exc, _Changed):
            return _denied(proposal, TARGET_CHANGED, f"{exc} changed before it was replaced")
        return _denied(proposal, PATCH_APPLY_FAILED, f"commit failed: {type(exc).__name__}")
    except BaseException:
        # Ctrl-C or similar mid-commit: restore, then re-raise.
        failed = _rollback(planned, undo)
        if journal is not None and not failed:
            _unlink_quietly(journal)
        raise

    if journal is not None:
        _unlink_quietly(journal)
    return PatchResult(
        status=APPLIED,
        proposal_id=proposal.proposal_id,
        modified_files=tuple(item.rel for item in planned),
        authorized_by=parsed_auth.authorized_by if parsed_auth else "",
        authorized_at=parsed_auth.authorized_at if parsed_auth else "",
        resulting_digest=digest("".join(item.staged for item in planned)),
    )


class _Changed(Exception):
    """A planned file changed between planning and its replacement."""


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
