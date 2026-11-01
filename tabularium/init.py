"""Bounded, safe project scaffolding for ``tabu init``.

The M2 templates remain the source of truth. M7.1 only substitutes explicit
neutral scaffold language for the M2 human-confirmation markers; it does not
create a charter, Task, approval, Decision, Evidence, or other project truth.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from tabularium.version import get_version

TEMPLATE_PACKAGE = "tabularium.templates"
RULESET_VERSION = get_version()

REQUIRED_FILES = (
    ".ai/manifest.yaml",
    ".ai/project.md",
    ".ai/constraints.md",
    ".ai/current-state.md",
)
REQUIRED_DIRECTORIES = (
    ".ai/tasks",
    ".ai/evidence",
    ".ai/investigations",
    ".ai/decisions",
    ".ai/questions",
    ".ai/handoffs",
    ".ai/reports",
)
README_SOURCES = {
    "tasks/README.md": "task/README.md",
    "evidence/README.md": "evidence/README.md",
    "investigations/README.md": "investigation/README.md",
    "decisions/README.md": "decision/README.md",
    "questions/README.md": "question/README.md",
    "handoffs/README.md": "handoff/README.md",
    "reports/README.md": "report/README.md",
}

_PROJECT_TITLE = (
    "No human-confirmed project title has been recorded yet. "
    "Project onboarding will record it in a later step."
)
_PROJECT_PURPOSE = (
    "No human-confirmed project purpose has been recorded yet. "
    "This M7.1 scaffold creates structure only."
)
_PROJECT_SCOPE = (
    "No human-confirmed broad project scope has been recorded yet. "
    "This M7.1 scaffold creates structure only."
)
_ZERO_STATE = (
    "M7.1 initialization created this project scaffold only. No bootstrap "
    "Task, approval, Decision, Evidence, Handoff, or Report was created. "
    "Human-confirmed project intent remains to be recorded by onboarding."
)
_UNSUBSTITUTED_MARKERS = (
    "{{ HUMAN-CONFIRMED PROJECT TITLE }}",
    "{{ HUMAN-CONFIRMED PROJECT PURPOSE }}",
    "{{ HUMAN-CONFIRMED BROAD PROJECT SCOPE }}",
    "{{ BOOTSTRAP_TASK_ID }}",
    "{{ SOURCE-LINKED ZERO-STATE SUMMARY }}",
)


class InitError(Exception):
    """A product-level expected initialization error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InitResult:
    status: str
    target: str
    created: tuple[str, ...] = field(default_factory=tuple)
    code: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "target": self.target,
            "created": list(self.created),
        }
        if self.code is not None:
            result["code"] = self.code
        if self.message is not None:
            result["message"] = self.message
        return result


def _resource_text(relative: str) -> str:
    try:
        return files(TEMPLATE_PACKAGE).joinpath(relative).read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise InitError(
            "TEMPLATE_UNAVAILABLE",
            f"required project template is unavailable: {relative}",
        ) from exc


TEMPLATE_BANNER = "**TEMPLATE ONLY — DO NOT PERSIST WITH UNSUBSTITUTED MARKERS**"


def _strip_template_banner(text: str, relative: str) -> str:
    """Drop the template-only banner from a rendered foundation document.

    The banner addresses whoever fills the template by hand; a rendered file
    has its markers substituted and must not carry the banner into a project.
    """
    stripped, removed = re.subn(
        rf"(?m)^{re.escape(TEMPLATE_BANNER)}[ \t]*\r?\n(?:[ \t]*\r?\n)?", "", text)
    if removed != 1:
        raise InitError("TEMPLATE_INVALID",
                        f"the {relative} template must contain exactly one template-only banner")
    return stripped


def _render_templates() -> dict[str, str]:
    """Render only the neutral M7.1 scaffold from canonical M2 templates."""
    project = _resource_text("project/project.md")
    project = project.replace("{{ HUMAN-CONFIRMED PROJECT TITLE }}", _PROJECT_TITLE)
    project = project.replace("{{ HUMAN-CONFIRMED PROJECT PURPOSE }}", _PROJECT_PURPOSE)
    project = project.replace("{{ HUMAN-CONFIRMED BROAD PROJECT SCOPE }}", _PROJECT_SCOPE)

    current_state = _resource_text("project/current-state.md")
    current_state = current_state.replace(
        ".ai/tasks/{{ BOOTSTRAP_TASK_ID }}.md",
        "No bootstrap Task created by M7.1 initialization",
    )
    current_state = current_state.replace("{{ SOURCE-LINKED ZERO-STATE SUMMARY }}", _ZERO_STATE)

    manifest_template = _resource_text("project/manifest.yaml")
    manifest_rendered, replaced = re.subn(
        r'(?m)^(tabularium_ruleset:[ \t]*)"[^"\n]*"[ \t]*$',
        rf'\1"{get_version()}"',
        manifest_template,
    )
    if replaced != 1:
        # A packaged template the substitution cannot find would silently
        # stamp new projects with a stale ruleset.
        raise InitError("TEMPLATE_INVALID",
                        "the manifest template must contain exactly one quoted tabularium_ruleset line")

    rendered = {
        "manifest.yaml": manifest_rendered,
        "project.md": _strip_template_banner(project, "project.md"),
        "constraints.md": _resource_text("project/constraints.md"),
        "current-state.md": _strip_template_banner(current_state, "current-state.md"),
    }
    rendered.update({relative: _resource_text(source) for relative, source in README_SOURCES.items()})
    return rendered


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _direct_child(path: Path, parent: Path) -> bool:
    """Require a path to resolve to a real direct child of its parent."""
    if _is_link(path):
        return False
    try:
        return path.resolve(strict=True).parent == parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False


def _manifest_verdict(text: str) -> str:
    """Classify one manifest without creating project state.

    "VALID" satisfies Manifest v1.0 for this installation's ruleset,
    "RULESET_MISMATCH" is a structurally valid manifest governed by a
    different Tabularium version, and "INVALID" fails the frozen validator.
    """
    try:
        from tabularium.engine.context_loader.manifest import ruleset_compatible, validate
    except (ImportError, ModuleNotFoundError):
        return "INVALID"
    manifest, _ = validate(text)
    if not manifest:
        return "INVALID"
    ruleset = manifest.get("tabularium_ruleset")
    if not ruleset or not ruleset_compatible(ruleset, RULESET_VERSION):
        return "RULESET_MISMATCH"
    return "VALID"


def _classify_ai(ai_dir: Path, target: Path) -> str:
    """Classify an existing .ai tree without modifying or repairing it."""
    if not ai_dir.exists() and not _is_link(ai_dir):
        return "ABSENT"
    if not ai_dir.is_dir() or not _direct_child(ai_dir, target):
        return "CONFLICT"

    missing = False
    for relative in REQUIRED_FILES:
        path = target / relative
        if not path.exists():
            missing = True
            continue
        if not path.is_file() or not _direct_child(path, ai_dir):
            return "CONFLICT"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return "CONFLICT"
        if relative != ".ai/manifest.yaml" and any(
            marker in text for marker in _UNSUBSTITUTED_MARKERS
        ):
            return "CONFLICT"
        if relative == ".ai/manifest.yaml":
            verdict = _manifest_verdict(text)
            if verdict == "INVALID":
                return "CONFLICT"
            if verdict == "RULESET_MISMATCH":
                # A structurally valid manifest for another Tabularium version is
                # not malformed: classify it truthfully so init/onboard
                # refusals agree with what status/doctor report.
                return "VERSION_MISMATCH"
        if not text.strip():
            return "CONFLICT"

    for relative in REQUIRED_DIRECTORIES:
        path = target / relative
        if not path.exists():
            missing = True
            continue
        if not path.is_dir() or not _direct_child(path, ai_dir):
            return "CONFLICT"

    return "PARTIAL" if missing else "VALID"


def _canonical_target(raw_target: str | Path | None) -> Path:
    candidate = Path(raw_target) if raw_target is not None else Path.cwd()
    try:
        if not candidate.exists():
            raise InitError("TARGET_NOT_FOUND", f"target does not exist: {candidate}")
        target = candidate.resolve(strict=True)
    except InitError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise InitError("TARGET_NOT_FOUND", f"target cannot be resolved: {candidate}") from exc
    if not target.is_dir():
        raise InitError("TARGET_NOT_DIRECTORY", f"target is not a directory: {candidate}")
    return target


def _missing_scaffold_items(target: Path) -> tuple[str, ...]:
    """Absent required scaffold relatives, sorted — display evidence only.

    Read-only recheck over the same static constants `_classify_ai` uses;
    it never repairs, creates, or writes anything.
    """
    return tuple(sorted(
        relative for relative in REQUIRED_FILES + REQUIRED_DIRECTORIES
        if not (target / relative).exists()
    ))


def initialize(raw_target: str | Path | None = None) -> InitResult:
    """Initialize one target, refusing existing partial or conflicting state."""
    target = _canonical_target(raw_target)
    target_label = str(target)
    ai_dir = target / ".ai"
    classification = _classify_ai(ai_dir, target)

    if classification == "VALID":
        return InitResult("ALREADY_INITIALIZED", target_label)
    if classification == "PARTIAL":
        missing = _missing_scaffold_items(target)
        message = ".ai exists but required scaffold content is missing"
        if missing:
            message = f"{message}: {', '.join(missing)}"
        return InitResult(
            "PARTIAL_INITIALIZATION",
            target_label,
            code="PARTIAL_INITIALIZATION",
            message=message,
        )
    if classification == "VERSION_MISMATCH":
        return InitResult(
            "VERSION_MISMATCH",
            target_label,
            code="VERSION_MISMATCH",
            message=".ai is governed by a different Tabularium ruleset version "
                    "than this installation provides; no files were changed",
        )
    if classification == "CONFLICT":
        return InitResult(
            "EXISTING_AI_DIRECTORY_CONFLICT",
            target_label,
            code="EXISTING_AI_DIRECTORY_CONFLICT",
            message=".ai exists but is malformed or redirected; no files were changed",
        )

    rendered = _render_templates()
    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=".ai-init-", dir=str(target)))
        for relative, text in rendered.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        os.replace(str(stage), str(ai_dir))
        stage = None
    except PermissionError as exc:
        raise InitError("PERMISSION_DENIED", f"permission denied while initializing {target}") from exc
    except OSError as exc:
        raise InitError("WRITE_FAILED", f"could not initialize {target}: {exc.strerror or exc}") from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)

    if _classify_ai(ai_dir, target) != "VALID":
        return InitResult(
            "PARTIAL_INITIALIZATION",
            target_label,
            code="PARTIAL_INITIALIZATION",
            message="initialization did not produce a complete scaffold",
        )
    created = tuple(sorted(REQUIRED_FILES + tuple(f".ai/{p}" for p in README_SOURCES)))
    return InitResult("INITIALIZED", target_label, created=created)


def render_human(result: InitResult) -> str:
    """Render the canonical result for a human terminal."""
    if result.status == "INITIALIZED":
        lines = [f"Initialized Tabularium in:\n{result.target}", "", "Created:"]
        lines.extend(result.created)
        lines.extend(["", "Initialization complete."])
        return "\n".join(lines)
    if result.status == "ALREADY_INITIALIZED":
        return f"Tabularium is already initialized in:\n{result.target}\n\nNo files changed."
    return f"{result.status}: {result.message}\nTarget: {result.target}"


def render_json(result: InitResult) -> str:
    return json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True)
