"""The controlled writer (M4.4). THE ONLY CODE THAT MUTATES PROJECT TRUTH.

There is deliberately no `write(path, bytes)` here. The single public entry
point demands all three of:

    a proposal          what exact change is proposed
    a passing gate      all nine frozen conditions satisfied
    an authorization    explicit human authority bound to that exact content

Missing or mismatched any one of them, nothing is written. The gate is
re-evaluated inside this call rather than trusted from the caller, so a
passing evaluation obtained earlier — or constructed by hand — cannot be
substituted for the real thing.

ATOMICITY: a record is written to a temporary file in the SAME directory,
flushed and fsynced, then moved into place with `os.replace`, which is atomic
on POSIX and on Windows for same-volume replacement. A reader therefore sees
either the old record or the new one, never a half-written one. On Windows the
replace can fail if another process holds the target open; that surfaces as a
controlled `WRITE_FAILED` rather than a partial record.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator, Tuple

from . import gate as G
from .foundation_validation import validate_foundation_document
from . import model as M


def apply_authorized_proposal(proposal: M.PersistenceProposal,
                              project_root: str,
                              authorization: Any,
                              used_bindings: tuple[str, ...] = ()
                              ) -> M.PersistenceResult:
    """Apply ONE authorized, gated proposal. The only durable write path.

    Returns a structured result for every outcome. A denial writes nothing and
    says which condition refused.
    """
    if getattr(proposal, "proposal_kind", "") == "FOUNDATION_BATCH":
        return _apply_authorized_foundation_batch(
            proposal, project_root, authorization, used_bindings
        )
    try:
        # Captured before any check: the write later must reach this inode.
        root_resolved = Path(project_root).resolve()
        root_identity = _identity(root_resolved)
    except (OSError, RuntimeError, ValueError):
        root_resolved, root_identity = None, None

    evaluation = G.evaluate(proposal, project_root, authorization,
                            used_bindings=used_bindings)
    if not evaluation.eligible:
        return M.PersistenceResult(
            status=M.DENIED, proposal_id=proposal.proposal_id,
            target_path=proposal.target_path,
            reason_codes=evaluation.reason_codes or (M.PERSISTENCE_GATE_FAILED,),
            gate=evaluation,
            diagnostics=tuple(
                {"code": c.reason_code or M.PERSISTENCE_GATE_FAILED,
                 "detail": f"condition {c.number} {c.identifier}: {c.detail}"}
                for c in evaluation.failed))

    target = G.resolved_target(proposal, project_root)
    if target is None:                      # already covered by conditions 8/9
        return _failed(proposal, M.TARGET_OUTSIDE_PROJECT,
                       "target does not resolve inside the project",
                       evaluation)

    parsed = (authorization
              if isinstance(authorization, M.PersistenceAuthorization)
              else M.PersistenceAuthorization.from_dict(authorization))

    try:
        if _supports_dir_fd():
            if root_identity is None:
                raise OSError(errno.ESTALE, "project root could not be identified before the checks")
            # The gate-validated, project-relative spelling, never the resolved
            # path: a symlink inside the project must be met by O_NOFOLLOW, not
            # silently followed by resolve().
            lexical = root_resolved / proposal.target_path
            if proposal.operation == M.CREATE:
                _exclusive_create(lexical, proposal.content, root_resolved, root_identity)
            else:
                _atomic_write(lexical, proposal.content, root_resolved, root_identity)
        else:
            _legacy_atomic_write(target, proposal.content)
    except OSError as exc:
        # No automatic retry, and never a false success.
        return _failed(proposal, M.WRITE_FAILED,
                       f"{type(exc).__name__}: {str(exc)[:120]}", evaluation)

    return M.PersistenceResult(
        status=M.WRITTEN,
        proposal_id=proposal.proposal_id,
        target_path=proposal.target_path,
        gate=evaluation,
        authorized_by=parsed.authorized_by if parsed else "",
        authorized_at=parsed.authorized_at if parsed else "",
        resulting_digest=proposal.content_digest)


def evaluate_only(proposal: M.PersistenceProposal, project_root: str,
                  authorization: Any = None,
                  used_bindings: tuple[str, ...] = ()) -> M.PersistenceResult:
    """Evaluate without writing. Strictly read-only.

    Useful for showing a human what would happen before they authorize it.
    """
    evaluation = G.evaluate(proposal, project_root, authorization,
                            used_bindings=used_bindings)
    if evaluation.eligible:
        return M.PersistenceResult(
            status=M.PROPOSAL_READY, proposal_id=proposal.proposal_id,
            target_path=proposal.target_path, gate=evaluation)
    return M.PersistenceResult(
        status=M.DENIED, proposal_id=proposal.proposal_id,
        target_path=proposal.target_path,
        reason_codes=evaluation.reason_codes or (M.PERSISTENCE_GATE_FAILED,),
        gate=evaluation,
        diagnostics=tuple(
            {"code": c.reason_code or M.PERSISTENCE_GATE_FAILED,
             "detail": f"condition {c.number} {c.identifier}: {c.detail}"}
            for c in evaluation.failed))


_CHANGE_CONFIG_MAX_BYTES = 64 * 1024


def _valid_change_config(content: str) -> bool:
    """Structural check of .ai/change.yaml at the writer's own boundary.

    The full rules live in tabularium.change_config (which tabularium/engine/ does not
    import); this keeps a second caller from writing a file --change would
    then refuse: size cap, a mapping of known keys, test_command a non-empty
    list of non-empty strings.
    """
    import yaml

    if len(content.encode("utf-8")) > _CHANGE_CONFIG_MAX_BYTES:
        return False
    try:
        data = yaml.safe_load(content)
    except (yaml.YAMLError, RecursionError):
        return False
    if not isinstance(data, dict) or not set(data) <= {"test_command", "timeout_seconds"}:
        return False
    command = data.get("test_command")
    return (isinstance(command, list) and bool(command)
            and all(isinstance(item, str) and item for item in command))


def _supports_dir_fd() -> bool:
    """Whether descriptor-relative writes are available (not on Windows)."""
    return {os.open, os.unlink}.issubset(os.supports_dir_fd)


def _identity(path: Path | str) -> tuple[int, int]:
    info = os.stat(path, follow_symlinks=False)
    return info.st_dev, info.st_ino


_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
              | getattr(os, "O_CLOEXEC", 0))
_FILE_FLAGS = (os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_CLOEXEC", 0))


def _check_identity(fd: int, expected: tuple[int, int] | None, what: str) -> None:
    if expected is not None:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != tuple(expected):
            raise OSError(errno.ESTALE, f"{what} changed after the preflight check")


@contextlib.contextmanager
def _open_dir(root: Path, parts: tuple[str, ...],
              root_identity: tuple[int, int] | None = None,
              dir_identity: tuple[int, int] | None = None) -> Iterator[int]:
    """Open root, then each component relative to the previous descriptor.

    Every open uses O_NOFOLLOW, and the root (and optionally the final
    directory) must still be the inode the preflight checked: a path swapped
    for a symlink or another directory in between fails instead of
    redirecting the write.
    """
    fds: list[int] = []
    try:
        fd = os.open(str(root), _DIR_FLAGS)
        fds.append(fd)
        _check_identity(fd, root_identity, "project root")
        for part in parts:
            if part in ("", ".", ".."):
                raise OSError(errno.EINVAL, f"invalid path component: {part!r}")
            fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            fds.append(fd)
        _check_identity(fd, dir_identity, "record directory")
        yield fd
    finally:
        for opened in reversed(fds):
            try:
                os.close(opened)
            except OSError:
                pass


def _relative_dir(root: Path | str, target: Path | str) -> tuple[Path, tuple[str, ...], str]:
    """(resolved root, directory components below it, file name) for target.

    The relative part is taken lexically, without resolving anything below
    the root: every component is then opened with O_NOFOLLOW, so a symlink
    anywhere on the way fails instead of redirecting the write.
    """
    root_res = Path(root).resolve()
    target_path = Path(os.path.normpath(str(target)))
    try:
        rel = target_path.parent.relative_to(root_res)
    except ValueError:
        raise OSError(errno.EPERM, f"{target} is outside the project") from None
    if any(part in ("", ".", "..") for part in rel.parts):
        raise OSError(errno.EPERM, f"{target} is outside the project")
    return root_res, rel.parts, target_path.name


def _quiet_unlink(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _write_temp(dir_fd: int, content: str) -> str:
    name = f".tabu-{secrets.token_hex(8)}.tmp"
    fd = os.open(name, _FILE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        # newline="" keeps the proposal's exact bytes (content digests).
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _quiet_unlink(dir_fd, name)
        raise
    return name


def _fd_replace(dir_fd: int, filename: str, content: str) -> None:
    try:
        info = os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise OSError(errno.ELOOP, f"{filename} is not a regular file")
    temporary = _write_temp(dir_fd, content)
    try:
        os.replace(temporary, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        _quiet_unlink(dir_fd, temporary)
        raise


def _fd_create(dir_fd: int, filename: str, content: str) -> None:
    """Create filename, failing if anything exists there (link() never replaces)."""
    temporary = _write_temp(dir_fd, content)
    try:
        try:
            os.link(temporary, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                raise
            # File systems without hard links (FAT/exFAT, some FUSE/SMB):
            # O_EXCL still refuses an existing entry; a partial file is removed.
            fd = os.open(filename, _FILE_FLAGS, 0o600, dir_fd=dir_fd)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                _quiet_unlink(dir_fd, filename)
                raise
    finally:
        _quiet_unlink(dir_fd, temporary)


def _legacy_atomic_write(target: Path | str, content: str) -> None:
    """Path-based write, for platforms without dir_fd and for callers without a root."""
    target = Path(target)
    directory = target.parent
    if not directory.is_dir():
        raise OSError(f"record directory does not exist: {directory.name}")
    handle, temporary = tempfile.mkstemp(dir=str(directory), prefix=".tabu-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(target))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _legacy_exclusive_create(target: Path | str, content: str) -> None:
    target = Path(target)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".tabu-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, str(target))
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                raise
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                os.unlink(str(target))
                raise
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _atomic_write(target, content: str, project_root: Path | str | None = None,
                  root_identity: tuple[int, int] | None = None) -> None:
    """Replace target with content via a same-directory temp file.

    With a project root and dir_fd support the directory is reached from the
    (identity-checked) root without following symlinks; otherwise the
    path-based behaviour is unchanged (code_gate/sandbox passes no root).
    """
    if project_root is None or not _supports_dir_fd():
        return _legacy_atomic_write(target, content)
    root, parts, name = _relative_dir(project_root, target)
    with _open_dir(root, parts, root_identity) as dir_fd:
        _fd_replace(dir_fd, name, content)


def _exclusive_create(target, content: str, project_root: Path | str | None = None,
                      root_identity: tuple[int, int] | None = None) -> None:
    if project_root is None or not _supports_dir_fd():
        return _legacy_exclusive_create(target, content)
    root, parts, name = _relative_dir(project_root, target)
    with _open_dir(root, parts, root_identity) as dir_fd:
        _fd_create(dir_fd, name, content)


def _failed(proposal, code: str, detail: str,
            evaluation) -> M.PersistenceResult:
    return M.PersistenceResult(
        status=M.FAILED, proposal_id=proposal.proposal_id,
        target_path=proposal.target_path, reason_codes=(code,),
        gate=evaluation,
        diagnostics=({"code": code, "detail": detail},))


def _apply_authorized_foundation_batch(batch: Any, project_root: str,
                                       authorizations: Any,
                                       used_bindings: tuple[str, ...]) -> M.PersistenceResult:
    """Apply M2 foundation proposals using the existing writer boundary.

    Foundation documents are explicitly bounded project-document owners, not
    M1 record families, so they cannot pass the frozen nine-condition record
    gate. This adapter does not broaden that gate or create a second writer: it
    reuses the existing PersistenceAuthorization shape, validates project-root,
    target, operation, provenance, digest, and binding for every proposal, then
    uses this module's existing atomic writer with rollback.
    """
    proposals = tuple(getattr(batch, "proposals", ()))
    if not proposals or not isinstance(authorizations, dict):
        return _foundation_denial(batch, M.PERSISTENCE_AUTHORIZATION_REQUIRED,
                                  "one authorization mapping is required per foundation proposal")

    try:
        root = Path(project_root).resolve(strict=True)
        if str(root) != str(getattr(batch, "project_root", "")):
            return _foundation_denial(batch, M.PERSISTENCE_AUTHORIZATION_MISMATCH,
                                      "proposal project root does not match the writer root")
        operation_id = str(getattr(batch, "onboarding_operation_id", ""))
        if not operation_id or operation_id != str(getattr(batch, "proposal_id", "")):
            return _foundation_denial(batch, M.PROPOSAL_INVALID,
                                      "onboarding operation identity is missing or inconsistent")
    except (OSError, RuntimeError, ValueError):
        return _foundation_denial(batch, M.TARGET_OUTSIDE_PROJECT,
                                  "project root cannot be resolved")

    allowed = {
        ".ai/project.md",
        ".ai/constraints.md",
        ".ai/current-state.md",
    }
    # Optional members are created alongside the foundations under the same
    # authorization rules, never updated: an existing file is the user's.
    creatable = {
        ".ai/change.yaml",
    }
    prepared: list[tuple[Any, M.PersistenceAuthorization, Path, bytes | None, str]] = []
    seen_bindings: set[str] = set(used_bindings)
    seen_ids: set[str] = set()
    ai_root = root / ".ai"
    try:
        if not ai_root.is_dir() or ai_root.is_symlink() or ai_root.resolve().parent != root:
            return _foundation_denial(batch, M.TARGET_OUTSIDE_PROJECT,
                                      "foundation root is not a direct project child")
        root_identity, ai_identity = _identity(root), _identity(ai_root)
        for proposal in proposals:
            proposal_id = str(getattr(proposal, "proposal_id", ""))
            target_path = str(getattr(proposal, "target_path", "")).replace("\\", "/")
            operation = str(getattr(proposal, "operation", ""))
            binding = str(proposal.binding())
            if proposal_id in seen_ids:
                return _foundation_denial(batch, M.PROPOSAL_INVALID,
                                          "duplicate foundation proposal id")
            seen_ids.add(proposal_id)
            creating = target_path in creatable
            if target_path not in allowed and not creating:
                return _foundation_denial(batch, "FOUNDATION_TARGET_UNSUPPORTED",
                                          "foundation target is not one of the supported documents")
            expected_operation = M.CREATE if creating else M.UPDATE
            if (operation != expected_operation or
                    str(getattr(proposal, "project_root", "")) != str(root) or
                    str(getattr(proposal, "onboarding_operation_id", "")) != operation_id or
                    binding in seen_bindings):
                return _foundation_denial(batch, M.PERSISTENCE_AUTHORIZATION_MISMATCH,
                                          "foundation proposal identity or target is invalid")
            if creating:
                target = ai_root.resolve() / Path(target_path).name
                if os.path.lexists(target):
                    return _foundation_denial(batch, M.STALE_TARGET,
                                              f"{target_path} appeared after proposal creation")
            else:
                if os.path.islink(root / target_path):
                    return _foundation_denial(batch, M.TARGET_OUTSIDE_PROJECT,
                                              "foundation target is a symlink")
                target = (root / target_path).resolve(strict=True)
                if (target.parent != ai_root.resolve() or target.is_symlink() or
                        not target.is_file() or target_path not in allowed):
                    return _foundation_denial(batch, M.TARGET_OUTSIDE_PROJECT,
                                              "foundation target is not a direct project document")
            content = str(getattr(proposal, "content", ""))
            reason = str(getattr(proposal, "reason", ""))
            if not content.strip() or not reason.strip():
                return _foundation_denial(batch, M.PROPOSAL_INVALID,
                                          "foundation proposal content and reason are required")
            if not creating and (semantic_code := validate_foundation_document(target_path, content)) is not None:
                return _foundation_denial(batch, semantic_code,
                                          "foundation content does not match its canonical structure")
            if creating and not _valid_change_config(content):
                return _foundation_denial(batch, M.PROPOSAL_INVALID,
                                          f"{target_path} is not a valid test configuration")
            if M.contains_secret(content) or M.contains_secret(reason):
                return _foundation_denial(batch, M.SECRET_MATERIAL_PRESENT,
                                          "secret-shaped material cannot become project truth")
            expected_current_digest = str(getattr(proposal, "expected_current_digest", ""))
            if creating:
                if expected_current_digest:
                    return _foundation_denial(batch, M.STALE_TARGET,
                                              f"{target_path} must not exist for CREATE")
                current_digest = ""
            else:
                if not expected_current_digest:
                    return _foundation_denial(batch, "FOUNDATION_CURRENT_DIGEST_REQUIRED",
                                              "foundation UPDATE requires an old-state digest")
                current_digest = M.digest(target.read_text(encoding="utf-8"))
                if current_digest != expected_current_digest:
                    return _foundation_denial(batch, M.STALE_TARGET,
                                              f"foundation target changed after proposal creation: {target_path}")
            authorization = authorizations.get(proposal_id)
            parsed = (authorization if isinstance(authorization, M.PersistenceAuthorization)
                      else M.PersistenceAuthorization.from_dict(authorization))
            if parsed is None:
                return _foundation_denial(batch, M.PERSISTENCE_AUTHORIZATION_REQUIRED,
                                          "missing foundation persistence authorization")
            if not parsed.authorized_by.strip():
                return _foundation_denial(batch, M.PERSISTENCE_ACTOR_REQUIRED,
                                          "authorized_by is required")
            if not M.valid_timestamp(parsed.authorized_at):
                return _foundation_denial(batch, M.PERSISTENCE_TIMESTAMP_INVALID,
                                          "authorized_at must include an explicit UTC offset")
            expected_fields = (
                ("proposal_id", proposal_id),
                ("execution_id", operation_id),
                ("target_path", target_path),
                ("operation", expected_operation),
                ("content_digest", proposal.content_digest),
            )
            if any(getattr(parsed, name) != expected for name, expected in expected_fields):
                return _foundation_denial(batch, M.PROPOSAL_CONTENT_CHANGED,
                                          "foundation authorization does not match exact content")
            if parsed.binding != binding:
                return _foundation_denial(batch, M.PERSISTENCE_AUTHORIZATION_MISMATCH,
                                          "foundation authorization binding does not match")
            seen_bindings.add(binding)
            prepared.append((proposal, parsed, target,
                             None if creating else target.read_bytes(), current_digest))
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return _foundation_denial(batch, M.WRITE_FAILED,
                                  "foundation target could not be safely inspected")

    pending = [item for item in prepared if item[0].content_digest != item[4]]
    if not pending:
        return M.PersistenceResult(
            status="NO_CHANGE",
            proposal_id=str(getattr(batch, "proposal_id", "")),
            target_path=".ai",
            authorized_by=prepared[0][1].authorized_by,
            authorized_at=prepared[0][1].authorized_at,
        )

    # Every member is a direct child of .ai. With dir_fd support one .ai
    # descriptor, opened from the identity-checked root and checked against
    # the preflight inode, serves every write and the rollback: a swap in
    # the middle of the batch cannot split it across two directories.
    if _supports_dir_fd():
        try:
            with _open_dir(root, (".ai",), root_identity, ai_identity) as ai_fd:
                committed_fd: list[tuple[str, bytes | None]] = []
                try:
                    for proposal, _authorization, target, original, _current_digest in pending:
                        if original is None:
                            _fd_create(ai_fd, target.name, proposal.content)
                        else:
                            _fd_replace(ai_fd, target.name, proposal.content)
                        committed_fd.append((target.name, original))
                    # A .ai moved away and replaced during the batch would leave
                    # the writes in the moved directory while the project shows
                    # another one: roll back and refuse instead of reporting WRITTEN.
                    current = _identity(root / ".ai")
                    held = os.fstat(ai_fd)
                    if current != (held.st_dev, held.st_ino):
                        raise OSError(errno.ESTALE, ".ai was replaced during the write")
                except OSError as exc:
                    rollback_ok = True
                    for name, original in reversed(committed_fd):
                        try:
                            if original is None:
                                os.unlink(name, dir_fd=ai_fd)
                            else:
                                _fd_replace(ai_fd, name, original.decode("utf-8"))
                        except (OSError, UnicodeDecodeError):
                            rollback_ok = False
                    detail = ("foundation write failed and was rolled back" if rollback_ok
                              else "foundation write failed with rollback failure")
                    return _foundation_denial(batch, M.WRITE_FAILED, f"{detail}: {str(exc)[:120]}")
        except OSError as exc:
            return _foundation_denial(batch, M.WRITE_FAILED,
                                      f"foundation directory could not be opened safely: {str(exc)[:120]}")
    else:
        committed: list[tuple[Path, bytes | None]] = []
        try:
            for proposal, _authorization, target, original, _current_digest in pending:
                if original is None:
                    _legacy_exclusive_create(target, proposal.content)
                else:
                    _legacy_atomic_write(target, proposal.content)
                committed.append((target, original))
        except OSError as exc:
            rollback_ok = True
            for target, original in reversed(committed):
                try:
                    if original is None:
                        target.unlink()
                    else:
                        _legacy_atomic_write(target, original.decode("utf-8"))
                except (OSError, UnicodeDecodeError):
                    rollback_ok = False
            detail = ("foundation write failed and was rolled back" if rollback_ok
                      else "foundation write failed with rollback failure")
            return _foundation_denial(batch, M.WRITE_FAILED, f"{detail}: {str(exc)[:120]}")

    return M.PersistenceResult(
        status=M.WRITTEN,
        proposal_id=str(getattr(batch, "proposal_id", "")),
        target_path=".ai",
        authorized_by=pending[0][1].authorized_by,
        authorized_at=pending[0][1].authorized_at,
        resulting_digest=M.digest("".join(p.content for p, _, _, _, _ in pending)),
    )


def _foundation_denial(batch: Any, code: str, detail: str) -> M.PersistenceResult:
    return M.PersistenceResult(
        status=M.DENIED,
        proposal_id=str(getattr(batch, "proposal_id", "")),
        target_path=".ai",
        reason_codes=(code,),
        diagnostics=({"code": code, "detail": detail},),
    )
