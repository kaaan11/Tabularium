"""Read-only product health surfaces for M7.3.

The diagnostic layer is descriptive only. It reuses the production context
loader and the trusted M7.2 foundation validator, but has no persistence,
authorization, execution, model, remote, or memory authority.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tabularium.engine.code_gate.isolated_run import find_bwrap
from tabularium.engine.context_loader import load_context
from tabularium.engine.context_loader import manifest as manifest_rules
from tabularium.engine.context_loader.loader import _looks_like_source_checkout, default_ai_os_root
from tabularium.engine.context_loader.model import GOVERNED
from tabularium.engine.persistence.foundation_validation import validate_foundation_document
from tabularium.engine.workers.cli import find_native_executable

from .init import (
    InitError,
    REQUIRED_DIRECTORIES,
    REQUIRED_FILES,
    TEMPLATE_BANNER,
    _canonical_target,
    _classify_ai,
    _direct_child,
    _missing_scaffold_items,
)
from .onboarding import _needs_onboarding, discover_project
from .version import DIST_NAME, get_version

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

READY = "READY"
NOT_INITIALIZED = "NOT_INITIALIZED"
PARTIAL_INITIALIZATION = "PARTIAL_INITIALIZATION"
NOT_ONBOARDED = "NOT_ONBOARDED"
INVALID_PROJECT = "INVALID_PROJECT"
VERSION_MISMATCH = "VERSION_MISMATCH"
DEGRADED = "DEGRADED"
ERROR = "ERROR"

_FOUNDATION_PATHS = (".ai/project.md", ".ai/constraints.md", ".ai/current-state.md")
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9_.-]+")
_OPTIONAL_FALLBACKS = {
    "api": ("litellm",),
    "runtime": ("langgraph", "langgraph-checkpoint-sqlite"),
    "observability": ("opentelemetry-api", "opentelemetry-sdk"),
    "dev": ("pydantic", "pytest", "pytest-subtests"),
}


@dataclass(frozen=True)
class DiagnosticCheck:
    check_id: str
    component: str
    status: str
    severity: str
    reason_code: str
    message: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "component": self.component,
            "status": self.status,
            "severity": self.severity,
            "reason_code": self.reason_code,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class CapabilityStatus:
    name: str
    required: bool
    available: bool
    reason_code: str
    packages: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "available": self.available,
            "reason_code": self.reason_code,
            "packages": list(self.packages),
            "message": self.message,
        }


@dataclass(frozen=True)
class HealthResult:
    command: str
    tabularium_version: str | None
    target: str
    overall_state: str
    initialization_state: str
    onboarding_state: str
    loader_state: str
    foundation_state: str
    project_classification: tuple[str, ...] = ()
    compatibility: dict[str, object] = field(default_factory=dict)
    capabilities: tuple[CapabilityStatus, ...] = ()
    checks: tuple[DiagnosticCheck, ...] = ()
    next_steps: tuple[str, ...] = ()
    error_code: str | None = None
    # Presentation-only Task-selection evidence for the human renderer.
    # Deliberately excluded from to_dict(): the machine-readable surface is
    # unchanged; this line teaches READY == project/context health without
    # claiming anything about actionability or worker/account readiness.
    task_selection: str | None = None

    @property
    def exit_code(self) -> int:
        if self.overall_state == READY:
            return 0
        if self.overall_state == ERROR:
            return 2
        return 1

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "tabularium_version": self.tabularium_version,
            "target": self.target,
            "overall_state": self.overall_state,
            "initialization": {"state": self.initialization_state},
            "onboarding": {"state": self.onboarding_state},
            "loader": {"state": self.loader_state},
            "foundations": {"state": self.foundation_state},
            "project": {"classification": list(self.project_classification)},
            "compatibility": dict(self.compatibility),
            "capabilities": [cap.to_dict() for cap in self.capabilities],
            "checks": [check.to_dict() for check in self.checks],
            "next_steps": list(self.next_steps),
            "exit_code": self.exit_code,
            "error_code": self.error_code,
        }


def _check(check_id: str, component: str, status: str, reason_code: str,
           message: str, remediation: str | None = None) -> DiagnosticCheck:
    severity = {PASS: "INFO", WARN: "WARN", FAIL: "ERROR", SKIP: "INFO"}[status]
    return DiagnosticCheck(check_id, component, status, severity, reason_code,
                           message, remediation)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _display_text(value: object, limit: int = 64) -> str:
    """Terminal-safe bounded rendering of one trusted diagnostic value.

    Non-printable characters become visible ``ascii()`` escapes, whitespace
    collapses deterministically, and the result is hard-bounded. Used only
    for HUMAN output; machine-readable surfaces are never rewritten here.
    """
    if not isinstance(value, str):
        return ""
    escaped = "".join(ch if ch.isprintable() else ascii(ch)[1:-1]
                      for ch in value)
    collapsed = " ".join(escaped.split())
    if len(collapsed) > limit:
        return collapsed[:limit - 3].rstrip() + "..."
    return collapsed


def _package_versions() -> tuple[str | None, str | None, str | None, str | None]:
    source_version_file = Path(__file__).resolve().parents[1] / "VERSION"
    source_version = _read_text(source_version_file)
    source_version = source_version.strip() if source_version is not None else None
    try:
        installed = importlib.metadata.version(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        installed = None
    try:
        active = get_version()
    except (RuntimeError, OSError, UnicodeDecodeError):
        active = None
    origin = "repository" if source_version else ("package_metadata" if installed else None)
    return source_version, installed, active, origin


def _package_checks() -> tuple[str | None, list[DiagnosticCheck], dict[str, object]]:
    source_version, installed_version, active_version, source_origin = _package_versions()
    checks: list[DiagnosticCheck] = []
    if active_version is None:
        checks.append(_check("package-version", "Tabularium package", FAIL,
                             "PACKAGE_VERSION_UNAVAILABLE",
                             "Tabularium version metadata could not be read.",
                             "Reinstall Tabularium from the project package."))
    else:
        checks.append(_check("package-version", "Tabularium package", PASS,
                             "PACKAGE_VERSION_AVAILABLE",
                             f"Tabularium {active_version} is available."))
    if source_version and installed_version and source_version != installed_version:
        checks.append(_check(
            "package-coherence", "Package metadata", WARN,
            "VERSION_SOURCE_INSTALLED_MISMATCH",
            "Source VERSION and installed package metadata differ.",
            "Use the intended checkout or reinstall the package.",
        ))
    elif source_version or installed_version:
        checks.append(_check(
            "package-coherence", "Package metadata", PASS,
            "VERSION_METADATA_COHERENT",
            "Source and installed package version information is coherent for this environment.",
        ))
    else:
        checks.append(_check(
            "package-coherence", "Package metadata", FAIL,
            "PACKAGE_METADATA_UNAVAILABLE",
            "Neither source nor installed package metadata is available.",
            "Install Tabularium from a wheel or source checkout.",
        ))
    return active_version, checks, {
        "source_version": source_version,
        "installed_version": installed_version,
        "active_version": active_version,
        "source_version_origin": source_origin,
    }


def _target_check(target: Path | None, target_error: InitError | None) -> DiagnosticCheck:
    if target is not None:
        return _check("target", "Target directory", PASS, "TARGET_VALID",
                       "Target exists and is a directory.")
    code = target_error.code if target_error else "TARGET_INVALID"
    remediation = None
    if code == "TARGET_NOT_FOUND":
        remediation = "Provide an existing project directory."
    elif code == "TARGET_NOT_DIRECTORY":
        remediation = "Provide a directory path."
    return _check("target", "Target directory", FAIL, code,
                  "The target project directory could not be inspected.", remediation)


def _python_check() -> DiagnosticCheck:
    if sys.version_info >= (3, 12):
        return _check("python", "Python", PASS, "PYTHON_VERSION_SUPPORTED",
                      f"Python {platform.python_version()} satisfies the package requirement.")
    return _check("python", "Python", FAIL, "PYTHON_VERSION_UNSUPPORTED",
                  f"Python {platform.python_version()} is below the required 3.12 baseline.",
                  "Install Python 3.12 or newer.")


def _manifest_check(target: Path) -> tuple[DiagnosticCheck, dict[str, str] | None]:
    path = target / ".ai" / "manifest.yaml"
    if not _direct_child(path, target / ".ai"):
        return _check("manifest", "Project manifest", FAIL, "PATH_CONFINEMENT_FAILED",
                      "The manifest is not confined to the project .ai directory.",
                      "Inspect the .ai directory and remove redirected entries."), None
    text = _read_text(path)
    if text is None:
        return _check("manifest", "Project manifest", FAIL, "INVALID_MANIFEST",
                      "The project manifest is missing, unreadable, or invalid UTF-8.",
                      f"Run `tabu init {target}` only for an uninitialized project."), None
    manifest, _reason = manifest_rules.validate(text)
    if manifest is None:
        return _check("manifest", "Project manifest", FAIL, "INVALID_MANIFEST",
                      "The project manifest does not satisfy Manifest v1.0.",
                      "Correct the manifest version and required quoted fields."), None
    return _check("manifest", "Project manifest", PASS, "MANIFEST_VALID",
                  "The project manifest is valid."), manifest


def _foundation_checks(target: Path) -> tuple[list[DiagnosticCheck], str]:
    ai_root = target / ".ai"
    checks: list[DiagnosticCheck] = []
    if not ai_root.is_dir() or ai_root.is_symlink() or not _direct_child(ai_root, target):
        return [_check("foundations", "Foundation documents", FAIL,
                        "PATH_CONFINEMENT_FAILED",
                        "The .ai directory is absent, redirected, or outside the target.",
                        "Inspect the project path and .ai directory.")], "INVALID"
    valid = True
    for relative in _FOUNDATION_PATHS:
        path = target / relative
        if not _direct_child(path, ai_root) or not path.is_file():
            checks.append(_check(f"foundation-{path.name}", "Foundation documents", FAIL,
                                 "FOUNDATION_INVALID_STRUCTURE",
                                 f"{relative} is missing or not a confined file.",
                                 f"Run `tabu onboard {target}` after confirming the project scaffold."))
            valid = False
            continue
        content = _read_text(path)
        code = validate_foundation_document(relative, content or "")
        if code is not None:
            checks.append(_check(f"foundation-{path.name}", "Foundation documents", FAIL,
                                 code, f"{relative} does not satisfy its trusted foundation structure.",
                                 f"Review or rerun `tabu onboard {target}` after human confirmation."))
            valid = False
        else:
            checks.append(_check(f"foundation-{path.name}", "Foundation documents", PASS,
                                 "FOUNDATION_VALID", f"{relative} has valid structure."))
        if TEMPLATE_BANNER in (content or ""):
            checks.append(_check(f"template-banner-{path.name}", "Foundation documents", WARN,
                                 "TEMPLATE_BANNER_PERSISTED",
                                 f"{relative} still carries the template-only banner.",
                                 f"Delete the banner line from {relative}."))
    return checks, "VALID" if valid else "INVALID"


def _path_check(target: Path) -> DiagnosticCheck:
    ai_root = target / ".ai"
    if not ai_root.exists():
        return _check("path-confinement", "Project paths", SKIP, "NOT_INITIALIZED",
                      "Project path confinement will be checked after initialization.")
    entries = [ai_root / rel.removeprefix(".ai/") for rel in REQUIRED_FILES]
    entries.extend(ai_root / rel.removeprefix(".ai/") for rel in REQUIRED_DIRECTORIES)
    if ai_root.is_symlink() or not _direct_child(ai_root, target):
        return _check("path-confinement", "Project paths", FAIL,
                      "PATH_CONFINEMENT_FAILED", "The .ai directory is redirected or outside the target.",
                      "Remove the redirected .ai directory and inspect the target path.")
    for path in entries:
        if path.exists() and not _direct_child(path, ai_root):
            return _check("path-confinement", "Project paths", FAIL,
                          "PATH_CONFINEMENT_FAILED", "A project entry resolves outside the .ai directory.",
                          "Remove the redirected project entry.")
    return _check("path-confinement", "Project paths", PASS, "PATHS_CONFINED",
                  "The inspected project entries remain confined to the target.")


def _base_dependency_checks() -> tuple[DiagnosticCheck, ...]:
    try:
        found = importlib.util.find_spec("yaml") is not None
    except (ImportError, AttributeError, ValueError):
        found = False
    if found:
        return (_check("base-dependency-pyyaml", "Base dependencies", PASS,
                       "BASE_DEPENDENCY_AVAILABLE", "Required YAML support is available."),)
    return (_check("base-dependency-pyyaml", "Base dependencies", FAIL,
                   "BASE_DEPENDENCY_MISSING", "Required PyYAML support is unavailable.",
                   "Install the base Tabularium package dependencies."),)


def _requirement_name(value: str) -> str | None:
    match = _REQUIREMENT_NAME.match(value.strip())
    return match.group(0).lower().replace("_", "-") if match else None


def _optional_requirements() -> dict[str, tuple[str, ...]]:
    requirements: dict[str, set[str]] = {}
    try:
        metadata = importlib.metadata.metadata(DIST_NAME)
        extras = set(metadata.get_all("Provides-Extra") or ())
        for requirement in importlib.metadata.requires(DIST_NAME) or ():
            extra_match = re.search(r'extra\s*==\s*["\']([^"\']+)["\']', requirement)
            if not extra_match or extra_match.group(1) not in extras:
                continue
            name = _requirement_name(requirement)
            if name:
                requirements.setdefault(extra_match.group(1), set()).add(name)
    except (importlib.metadata.PackageNotFoundError, ValueError, OSError):
        pass
    if not requirements:
        return {name: tuple(values) for name, values in _OPTIONAL_FALLBACKS.items()}
    return {name: tuple(sorted(values)) for name, values in sorted(requirements.items())}


def _module_for_distribution(name: str) -> str:
    return {
        "langgraph-checkpoint-sqlite": "langgraph.checkpoint.sqlite",
        "opentelemetry-api": "opentelemetry",
        "opentelemetry-sdk": "opentelemetry.sdk",
    }.get(name, name.replace("-", "_"))


# The default worker's readiness uses the same capability vocabulary as the
# optional extras: availability is a FACT about this environment, never a
# claim that a worker is authorized, configured for use, or able to run.
_NATIVE_WORKER_CAPABILITY_NAME = "native-worker"
NATIVE_WORKER_READY = "NATIVE_WORKER_READY"
NATIVE_WORKER_UNAVAILABLE = "NATIVE_WORKER_UNAVAILABLE"


def _native_worker_capability() -> CapabilityStatus:
    """Readiness of the DEFAULT native worker, on the same model as extras.

    Mirrors find_native_executable's selection order exactly (a configured
    TABU_NATIVE_EXECUTABLE wins and, when unusable, disables the PATH
    candidates rather than falling back). Readiness is NOT invocation
    success: an available executable says nothing about authentication or
    whether a run will complete.
    """
    configured = os.environ.get("TABU_NATIVE_EXECUTABLE", "").strip()
    if configured:
        try:
            usable = Path(configured).expanduser().is_file()
        except (OSError, RuntimeError, ValueError):
            # Never leak a raw path or OS error through diagnostics.
            usable = False
        if usable:
            return CapabilityStatus(
                name=_NATIVE_WORKER_CAPABILITY_NAME, required=False,
                available=True, reason_code=NATIVE_WORKER_READY, packages=(),
                message="The native worker executable is ready via "
                        "TABU_NATIVE_EXECUTABLE; authorization, "
                        "configuration, and actual invocation remain "
                        "separate.")
        return CapabilityStatus(
            name=_NATIVE_WORKER_CAPABILITY_NAME, required=False,
            available=False, reason_code=NATIVE_WORKER_UNAVAILABLE,
            packages=(),
            message="TABU_NATIVE_EXECUTABLE is set but does not resolve to "
                    "an executable file; the native worker is unavailable.")
    found_on_path = False
    try:
        found_on_path = find_native_executable() is not None
    except (OSError, RuntimeError, ValueError):
        found_on_path = False
    if found_on_path:
        return CapabilityStatus(
            name=_NATIVE_WORKER_CAPABILITY_NAME, required=False,
            available=True, reason_code=NATIVE_WORKER_READY, packages=(),
            message="A native worker executable was found on PATH; "
                    "authorization, configuration, and actual invocation "
                    "remain separate.")
    return CapabilityStatus(
        name=_NATIVE_WORKER_CAPABILITY_NAME, required=False,
        available=False, reason_code=NATIVE_WORKER_UNAVAILABLE, packages=(),
        message="No native worker executable was found on PATH; install it "
                "or set TABU_NATIVE_EXECUTABLE.")


_SANDBOX_CAPABILITY_NAME = "sandbox-isolation"
SANDBOX_ISOLATION_PRESENT = "SANDBOX_ISOLATION_PRESENT"
SANDBOX_ISOLATION_UNAVAILABLE = "SANDBOX_ISOLATION_UNAVAILABLE"


def _sandbox_capability() -> CapabilityStatus:
    """Whether bwrap, which isolated test runs require, is on PATH.

    Presence only: doctor executes nothing. Whether the kernel lets bwrap
    create its namespaces is probed when tests actually run, and a failed
    probe refuses the run instead of falling back to an unconfined one.
    """
    try:
        present = find_bwrap() is not None
    except (OSError, ValueError):
        present = False
    if present:
        return CapabilityStatus(
            name=_SANDBOX_CAPABILITY_NAME, required=False, available=True,
            reason_code=SANDBOX_ISOLATION_PRESENT, packages=(),
            message="bwrap was found on PATH; authorization is separate, "
                    "and isolated test runs probe namespace support when "
                    "they start.")
    return CapabilityStatus(
        name=_SANDBOX_CAPABILITY_NAME, required=False, available=False,
        reason_code=SANDBOX_ISOLATION_UNAVAILABLE, packages=(),
        message="bwrap was not found on PATH; change runs with a test "
                "command are refused until bubblewrap is installed.")


def _capabilities() -> tuple[CapabilityStatus, ...]:
    result: list[CapabilityStatus] = []
    for name, packages in sorted(_optional_requirements().items()):
        missing: list[str] = []
        for package in packages:
            try:
                available = importlib.util.find_spec(_module_for_distribution(package)) is not None
            except (ImportError, AttributeError, ValueError):
                available = False
            if not available:
                missing.append(package)
        if missing:
            result.append(CapabilityStatus(
                name=name, required=False, available=False,
                reason_code="OPTIONAL_DEPENDENCY_MISSING", packages=packages,
                message=f"Optional capability is unavailable: {', '.join(missing)}.",
            ))
        else:
            result.append(CapabilityStatus(
                name=name, required=False, available=True,
                reason_code="OPTIONAL_DEPENDENCY_AVAILABLE", packages=packages,
                message="Optional dependencies are installed; authorization, configuration, and activation are separate.",
            ))
    result.append(_native_worker_capability())
    result.append(_sandbox_capability())
    result.sort(key=lambda cap: cap.name)
    return tuple(result)


# Loader diagnostic codes that mean Task-family records exist but are not
# usable candidates. Used ONLY to point at diagnostics; never to restate
# defect details (the loader owns that evidence).
_TASK_QUARANTINE_CODES = frozenset({
    "INVALID_RECORD_QUARANTINED", "MALFORMED_RECORD_QUARANTINED",
    "FILENAME_ID_MISMATCH", "RESOLUTION_AMBIGUOUS",
})

_TASK_REMEDIATION = "Create or select a usable Task before running work."
_TASK_STORAGE_REMEDIATION = ("Restore read access to .ai/tasks/ so Task "
                             "records can be inspected.")


def _task_selection_hint(package) -> str | None:
    """Task-selection evidence derived from the ALREADY-LOADED context package.

    No second discovery pass: governed project entry selects exactly the
    repository-level usable, non-terminal Tasks (reason
    PROJECT_NONTERMINAL_TASK) — the same candidate set `tabu run` accepts
    before task-specific actionability checks. The hint is presentation only:
    it never claims actionability, execution readiness, or worker/account
    availability, and it omits itself rather than fabricate certainty when
    evidence is unavailable.
    """
    if package.entry_mode != GOVERNED or \
            package.package_status != READY or \
            not package.compatibility.get("compatible", False):
        return None
    # Defensive invariant: project-entry candidates carry exactly this reason.
    # Requiring it keeps future loader-selection expansion from silently
    # changing what this line claims about run-gate candidate availability.
    ids = sorted({item.record_id for item in package.selected
                  if item.family == "Task"
                  and "PROJECT_NONTERMINAL_TASK" in item.selection_reasons})
    if len(ids) == 1:
        return f"1 candidate ({ids[0]})"
    if len(ids) > 1:
        return f"{len(ids)} candidates — choose one with --task"
    if any(diagnostic.semantic_owner == "Task"
           and diagnostic.code == "RECORD_DIRECTORY_UNREADABLE"
           for diagnostic in package.diagnostics):
        return "task storage unreadable — see diagnostics"
    task_defects = any(
        diagnostic.semantic_owner == "Task" and
        diagnostic.code in _TASK_QUARANTINE_CODES
        for diagnostic in package.diagnostics)
    return ("no usable Task candidate — see diagnostics" if task_defects
            else "no usable Task candidate")


def _loader_check(target: Path) -> tuple[DiagnosticCheck, str, dict[str, object], str | None]:
    try:
        package = load_context(target)
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return (_check("loader", "Context loader", FAIL, "CONTEXT_LOAD_FAILED",
                        "The production context loader could not inspect the project.",
                        "Correct the project structure and rerun `tabu doctor`."),
                "ERROR", {}, None)
    compatible = bool(package.compatibility.get("compatible", False))
    ready = package.entry_mode == "GOVERNED" and package.package_status == "READY" and compatible
    details = {
        "entry_mode": package.entry_mode,
        "package_status": package.package_status,
        "task_actionability": package.task_actionability,
        "compatible": compatible,
        "diagnostic_codes": sorted({diag.code for diag in package.diagnostics}),
    }
    if ready:
        return (_check("loader", "Context loader", PASS, "CONTEXT_READY",
                       "Production context loaded successfully."), "READY",
                details, _task_selection_hint(package))
    limit_codes = {"RECORD_TOO_LARGE", "RECORD_LIMIT_EXCEEDED", "TOTAL_BYTES_EXCEEDED"}
    limit_diags = [d for d in package.diagnostics if d.code in limit_codes]
    if limit_diags:
        diag = limit_diags[0]
        src = ", ".join(diag.sources) if diag.sources else str(target)
        msg = f"Production context is not ready: {diag.code} — {diag.reason} ({src})."
        rem = f"Discovery limit exceeded ({diag.code}) in {src}: {diag.reason}."
        return (_check("loader", "Context loader", FAIL, "CONTEXT_NOT_READY", msg, rem),
                "BLOCKED", details, None)
    return (_check("loader", "Context loader", FAIL, "CONTEXT_NOT_READY",
                    "Production context is not ready.",
                    "Correct the manifest, ruleset, or required foundations."),
            "BLOCKED", details, None)


def _next_steps(state: str, target: str, checks: tuple[DiagnosticCheck, ...]) -> tuple[str, ...]:
    if state == NOT_INITIALIZED:
        return (f"Run `tabu init {target}`.",)
    if state == PARTIAL_INITIALIZATION:
        return ("Inspect the partial .ai scaffold; initialization does not repair it.",)
    if state == NOT_ONBOARDED:
        return (f"Run `tabu onboard {target}`.",)
    if state == VERSION_MISMATCH:
        # Checked before the first FAIL check: a version-mismatch project
        # always also has a failing loader check, whose generic remediation
        # would otherwise shadow this specific guidance.
        return (f"Set `tabularium_ruleset` in {target}/.ai/manifest.yaml to the "
                "available Tabularium ruleset reported above.",)
    for check in checks:
        if check.remediation and check.status == FAIL:
            return (check.remediation,)
    return ()


def diagnose(raw_target: str | Path | None = None, command: str = "status") -> HealthResult:
    """Return a bounded, deterministic, read-only health result."""
    target_label = str(raw_target if raw_target is not None else Path.cwd())
    active_version, package_checks, package_meta = _package_checks()
    checks = [_python_check(), *package_checks]
    target: Path | None = None
    target_error: InitError | None = None
    try:
        target = _canonical_target(raw_target)
    except InitError as exc:
        target_error = exc
    checks.append(_target_check(target, target_error))
    if target is None:
        result = HealthResult(command, active_version, target_label, ERROR, ERROR,
                              ERROR, ERROR, ERROR, checks=tuple(checks),
                              next_steps=("Provide a valid project directory.",),
                              error_code=target_error.code if target_error else "TARGET_INVALID")
        return result

    pre_manifest: dict[str, str] | None = None
    pre_manifest_text = _read_text(target / ".ai" / "manifest.yaml")
    if pre_manifest_text is not None:
        pre_manifest, _ = manifest_rules.validate(pre_manifest_text)
    available_for_mismatch = package_meta.get("source_version") or package_meta.get("installed_version")
    if not (default_ai_os_root() / "VERSION").is_file() and _looks_like_source_checkout(default_ai_os_root()):
        available_for_mismatch = None
    manifest_version_mismatch = bool(
        pre_manifest and available_for_mismatch and
        not manifest_rules.ruleset_compatible(pre_manifest.get("tabularium_ruleset", ""), available_for_mismatch)
    )
    try:
        classification = "VERSION_MISMATCH" if manifest_version_mismatch else _classify_ai(target / ".ai", target)
    except (OSError, RuntimeError, ValueError):
        classification = "CONFLICT"
    initialization = {
        "ABSENT": NOT_INITIALIZED,
        "PARTIAL": PARTIAL_INITIALIZATION,
        "VALID": "INITIALIZED",
        "CONFLICT": INVALID_PROJECT,
        "VERSION_MISMATCH": "INITIALIZED",
    }.get(classification, INVALID_PROJECT)
    if classification == "VERSION_MISMATCH":
        checks.append(_check("initialization", "Project initialization", PASS,
                             "INITIALIZED", "The project scaffold is present."))
    elif classification == "ABSENT":
        checks.append(_check("initialization", "Project initialization", FAIL,
                             "NOT_INITIALIZED", "The project has no .ai directory.",
                             f"Run `tabu init {target}`."))
    elif classification == "PARTIAL":
        missing = _missing_scaffold_items(target)
        message = "The .ai project scaffold is incomplete."
        if missing:
            message = f"{message} Missing: {', '.join(sorted(missing))}."
        checks.append(_check("initialization", "Project initialization", FAIL,
                             "PARTIAL_INITIALIZATION", message,
                             "Inspect the partial scaffold; `tabu init` does not repair it."))
    elif classification == "CONFLICT":
        checks.append(_check("initialization", "Project initialization", FAIL,
                             "INVALID_PROJECT", "The .ai project structure is malformed or redirected.",
                             "Inspect the .ai directory and project manifest."))
    else:
        checks.append(_check("initialization", "Project initialization", PASS,
                             "INITIALIZED", "The project is initialized."))

    project_types: tuple[str, ...] = ()
    try:
        project_types = discover_project(target).project_types
    except (OSError, RuntimeError, ValueError):
        checks.append(_check("classification", "Project classification", SKIP,
                             "CLASSIFICATION_UNAVAILABLE", "Project classification was unavailable."))

    manifest: dict[str, str] | None = None
    foundation_state = "UNKNOWN"
    loader_state = "UNKNOWN"
    onboarding_state = "UNKNOWN"
    task_selection: str | None = None
    compatibility: dict[str, object] = dict(package_meta)
    if classification in {"VALID", "VERSION_MISMATCH"}:
        manifest_check, manifest = _manifest_check(target)
        checks.append(manifest_check)
        checks.append(_path_check(target))
        foundation_checks, foundation_state = _foundation_checks(target)
        checks.extend(foundation_checks)
        project_text = _read_text(target / ".ai" / "project.md") or ""
        current_text = _read_text(target / ".ai" / "current-state.md") or ""
        if manifest is not None:
            try:
                onboarding_complete = not _needs_onboarding(project_text, current_text)
            except (AttributeError, TypeError):
                onboarding_complete = False
            if onboarding_complete:
                onboarding_state = "COMPLETE"
                checks.append(_check("onboarding", "Project onboarding", PASS,
                                     "ONBOARDED", "Project identity has been recorded."))
            else:
                onboarding_state = NOT_ONBOARDED
                checks.append(_check("onboarding", "Project onboarding", WARN,
                                     "NOT_ONBOARDED", "Project identity has not been recorded.",
                                     f"Run `tabu onboard {target}`."))
        loader_check, loader_state, loader_details, task_selection = _loader_check(target)
        checks.append(loader_check)
        compatibility.update(loader_details)
        required_ruleset = manifest.get("tabularium_ruleset") if manifest else None
        source_root = default_ai_os_root()
        source_version_file = source_root / "VERSION"
        source_ruleset = _read_text(source_version_file)
        source_ruleset = source_ruleset.strip() if source_ruleset is not None else None
        source_checkout = _looks_like_source_checkout(source_root)
        available_ruleset = source_ruleset if source_checkout else (source_ruleset or package_meta.get("installed_version"))
        compatibility.update({
            "required_ruleset": required_ruleset,
            "available_ruleset": available_ruleset,
            "comparison": "MAJOR_MINOR",
        })
        if (
            required_ruleset
            and available_ruleset
            and manifest_rules.ruleset_compatible(required_ruleset, available_ruleset)
        ):
            checks.append(_check("version-compatibility", "Project compatibility", PASS,
                                 "VERSION_COMPATIBLE", "Project ruleset matches the available Tabularium ruleset."))
        else:
            checks.append(_check("version-compatibility", "Project compatibility", FAIL,
                                 "VERSION_MISMATCH", "Project ruleset does not match the available Tabularium ruleset.",
                                 "Use a compatible Tabularium installation or correct the manifest."))
        if source_checkout and source_ruleset is None:
            checks.append(_check("source-version", "Source version", FAIL,
                                 "VERSION_SOURCE_MISSING",
                                 "The source checkout is missing its VERSION file.",
                                 "Restore VERSION or run from an installed wheel."))
    else:
        checks.extend([
            _check("manifest", "Project manifest", SKIP, "PROJECT_NOT_INITIALIZED",
                   "Manifest validation requires a complete .ai scaffold."),
            _check("foundations", "Foundation documents", SKIP, "PROJECT_NOT_INITIALIZED",
                   "Foundation validation requires a complete .ai scaffold."),
            _check("loader", "Context loader", SKIP, "PROJECT_NOT_INITIALIZED",
                   "Production context loading requires a complete .ai scaffold."),
            _check("onboarding", "Project onboarding", SKIP, "PROJECT_NOT_INITIALIZED",
                   "Onboarding state requires a complete .ai scaffold."),
            _check("path-confinement", "Project paths", SKIP, "PROJECT_NOT_INITIALIZED",
                   "Project path confinement requires a complete .ai scaffold."),
        ])

    checks.extend(_base_dependency_checks())
    capabilities = _capabilities()
    checks.append(_check("network", "Network defaults", PASS, "NETWORK_DEFAULT_OFF",
                         "Status and doctor perform no network health probes."))
    checks.append(_check("authority", "Diagnostic authority", PASS, "DIAGNOSTIC_ONLY",
                         "Status and doctor have no execution, persistence, or repair authority."))

    required_failures = [check for check in checks if check.status == FAIL and check.check_id not in {"package-coherence"}]
    if classification == "ABSENT":
        overall = NOT_INITIALIZED
    elif classification == "PARTIAL":
        overall = PARTIAL_INITIALIZATION
    elif classification == "CONFLICT":
        overall = INVALID_PROJECT
    elif any(check.reason_code == "VERSION_MISMATCH" for check in checks):
        overall = VERSION_MISMATCH
    elif required_failures:
        overall = INVALID_PROJECT
    elif onboarding_state == NOT_ONBOARDED:
        overall = NOT_ONBOARDED
    elif any(check.reason_code == "VERSION_SOURCE_INSTALLED_MISMATCH" for check in checks):
        overall = DEGRADED
    else:
        overall = READY
    if any(check.reason_code == "PACKAGE_VERSION_UNAVAILABLE" for check in checks):
        overall = ERROR
    next_steps = _next_steps(overall, str(target), tuple(checks))
    if task_selection and overall in {READY, DEGRADED} and \
            task_selection.startswith("no usable Task candidate"):
        next_steps = (*next_steps, _TASK_REMEDIATION)
    if task_selection and overall in {READY, DEGRADED} and \
            task_selection.startswith("task storage unreadable"):
        next_steps = (*next_steps, _TASK_STORAGE_REMEDIATION)
    return HealthResult(command, active_version, str(target), overall,
                        initialization, onboarding_state, loader_state,
                        foundation_state, project_types, compatibility,
                        capabilities, tuple(checks), next_steps,
                        task_selection=task_selection)


def status_project(raw_target: str | Path | None = None) -> HealthResult:
    return diagnose(raw_target, "status")


def doctor_project(raw_target: str | Path | None = None) -> HealthResult:
    return diagnose(raw_target, "doctor")


def _capability_label(cap: CapabilityStatus) -> str:
    """One bounded human label for a capability, naming its distributions.

    Presentation only: the label mirrors the machine-readable `name` and
    `packages` fields exactly and adds no readiness claim beyond the
    available/unavailable grouping it appears under.
    """
    name = _display_text(cap.name)
    if not cap.packages:
        return name
    packages = ", ".join(_display_text(package) for package in cap.packages)
    return f"{name} [{packages}]"


def _capability_name_lines(capabilities: tuple[CapabilityStatus, ...]) -> list[str]:
    """Grouped human-readable capability names; empty groups are omitted."""
    available = [_capability_label(cap) for cap in capabilities if cap.available]
    unavailable = [_capability_label(cap)
                   for cap in capabilities if not cap.available]
    lines: list[str] = []
    if available:
        lines.append(f"  Available: {', '.join(available)}")
    if unavailable:
        lines.append(f"  Unavailable: {', '.join(unavailable)}")
    return lines


def render_json(result: HealthResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


def render_human(result: HealthResult) -> str:
    caps_available = sum(1 for cap in result.capabilities if cap.available)
    caps_missing = len(result.capabilities) - caps_available
    lines = [
        f"Tabularium {result.tabularium_version or 'unknown'}",
        f"Project: {result.target}",
        f"State: {result.overall_state}",
        f"Initialization: {result.initialization_state}",
        f"Onboarding: {result.onboarding_state}",
        f"Context: {result.loader_state}",
        f"Foundations: {result.foundation_state}",
        f"Optional capabilities: {caps_available} available, {caps_missing} unavailable",
        *(_capability_name_lines(result.capabilities)
          if result.capabilities else []),
    ]
    if any(cap.name == "runtime" and not cap.available
           for cap in result.capabilities):
        # READY reports project/context health, never execution readiness:
        # without the runtime extra, run/continue have no engine to use.
        lines.append("Execution (run/continue) is disabled: "
                     "the `runtime` extra is not installed.")
    if result.task_selection and result.overall_state in {READY, DEGRADED}:
        # Same high-level gate as the Task remediation in next_steps: the
        # line is presentation for healthy/inspectable projects, not a
        # diagnostic for broken ones.
        lines.append(f"Task selection: {result.task_selection}")
    if result.overall_state == VERSION_MISMATCH:
        # B6-B: the expected/current ruleset values are already trusted,
        # already published in the JSON surface; show them to humans too.
        # Display-only bounding keeps a hostile manifest value inert.
        required = _display_text(result.compatibility.get("required_ruleset"))
        available = _display_text(result.compatibility.get("available_ruleset"))
        lines.append(
            f"Ruleset required by project: {required or 'unknown'}; "
            f"available in this Tabularium: {available or 'unknown'}")
    if result.command == "doctor":
        lines.extend(["", "Checks:"])
        for check in result.checks:
            lines.append(f"[{check.status}] {check.component} — {check.message}")
    if result.next_steps:
        lines.extend(["", "Next:"])
        lines.extend(result.next_steps)
    lines.extend(["", f"Result: {result.overall_state}"])
    return "\n".join(lines)
