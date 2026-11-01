"""Deterministic, human-reviewed project onboarding for M7.2.

Discovery is deliberately an ephemeral observation surface. It reports bounded
signals from a project without copying source bodies, secrets, or repository
text into the profile. Only explicit human declarations are rendered into
project foundation-document proposals. M7.2 creates no Task automatically.
"""

from __future__ import annotations

import json
import math
import os
import shlex
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .init import (
    InitError,
    _canonical_target,
    _classify_ai,
    _direct_child,
    _resource_text,
    _strip_template_banner,
)
from tabularium.engine.persistence.foundation_validation import (
    FOUNDATION_INVALID_STRUCTURE,
    FOUNDATION_TARGET_UNSUPPORTED,
)

MAX_FILES = 200
MAX_DEPTH = 4
MAX_FILE_SIZE = 32 * 1024
MAX_BYTES_READ = 256 * 1024
MAX_PROFILE_BYTES = 16 * 1024

IGNORED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "dist", "build", "target", "out", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", "__pycache__", "vendor",
    "third_party", "site-packages",
})
SOURCE_DIRECTORIES = frozenset({"src", "app", "lib", "libs", "cmd", "internal", "packages"})
TEST_DIRECTORIES = frozenset({"test", "tests", "spec", "specs"})
DOC_DIRECTORIES = frozenset({"doc", "docs", "documentation"})

PROJECT_TYPE_BY_FILE = {
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "setup.cfg": "Python",
    "pipfile": "Python",
    "package.json": "JavaScript/Node.js",
    "cargo.toml": "Rust",
    "go.mod": "Go",
    "pom.xml": "JVM",
    "build.gradle": "JVM",
    "build.gradle.kts": "JVM",
}


CHANGE_CONFIG_PATH = ".ai/change.yaml"


@dataclass(frozen=True)
class DiscoveryFact:
    kind: str
    value: str
    source: str
    classification: str = "DIRECT_OBSERVATION"

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "value": self.value,
            "source": self.source,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class ProjectDiscovery:
    project_root: str
    project_types: tuple[str, ...]
    facts: tuple[DiscoveryFact, ...]
    safe_candidate_files: int
    files_considered: int
    files_inspected: int
    files_excluded: int
    safe_candidate_bytes: int
    bytes_read: int
    excluded_by_reason: tuple[tuple[str, int], ...]
    limits_reached: tuple[str, ...]

    @property
    def complete_within_bounds(self) -> bool:
        return not self.limits_reached

    def to_dict(self) -> dict[str, object]:
        return {
            "project_root": self.project_root,
            "project_types": list(self.project_types),
            "facts": [fact.to_dict() for fact in self.facts],
            "metrics": {
                "safe_candidate_files": self.safe_candidate_files,
                "files_considered": self.files_considered,
                "files_inspected": self.files_inspected,
                "files_excluded": self.files_excluded,
                "safe_candidate_bytes": self.safe_candidate_bytes,
                "bytes_read": self.bytes_read,
            },
            "limits": {
                "max_files": MAX_FILES,
                "max_depth": MAX_DEPTH,
                "max_file_size": MAX_FILE_SIZE,
                "max_bytes_read": MAX_BYTES_READ,
                "reached": list(self.limits_reached),
                "complete_within_bounds": self.complete_within_bounds,
            },
            "excluded_by_reason": dict(self.excluded_by_reason),
        }


@dataclass(frozen=True)
class UserDeclarations:
    title: str
    purpose: str
    scope: str
    constraints: str = ""
    test_command: tuple[str, ...] = ()

    def fields_received(self) -> tuple[str, ...]:
        fields = ["title", "purpose", "scope"]
        if self.constraints:
            fields.append("constraints")
        if self.test_command:
            fields.append("test_command")
        return tuple(fields)


@dataclass(frozen=True)
class ProjectProfile:
    discovery: ProjectDiscovery
    declaration_fields: tuple[str, ...] = ()
    profile_bytes: int = 0
    profile_tokens: int = 0
    profile_vs_safe_candidate_reduction_pct: float | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "discovered": self.discovery.to_dict(),
            "user_declaration_fields": list(self.declaration_fields),
            "authority_boundary": {
                "discovered_facts_are_project_truth": False,
                "user_declarations_are_project_truth": False,
                "persistence_requires_explicit_confirmation": True,
            },
        }
        payload["token_metrics"] = {
            "profile_bytes": self.profile_bytes,
            "profile_tokens": self.profile_tokens,
            "safe_candidate_bytes": self.discovery.safe_candidate_bytes,
            "profile_vs_safe_candidate_reduction_pct": self.profile_vs_safe_candidate_reduction_pct,
        }
        return payload


@dataclass(frozen=True)
class OnboardingProposal:
    operation_id: str
    durable_proposals: tuple["FoundationProposal", ...]
    requires_authorization: bool = True

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(proposal.target_path for proposal in self.durable_proposals)

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "proposals": [proposal.to_dict() for proposal in self.durable_proposals],
            "files": list(self.files),
            "requires_authorization": self.requires_authorization,
        }


@dataclass(frozen=True)
class FoundationProposal:
    """Exact content proposal for one M2 bounded project document.

    This is a proposal, not an authority. The existing M4.4 writer accepts the
    batch adapter only after it validates the same content-bound authorization
    fields used by ``PersistenceAuthorization``.
    """

    proposal_id: str
    onboarding_operation_id: str
    project_root: str
    target_path: str
    operation: str
    content: str = field(repr=False, compare=False)
    reason: str
    expected_current_digest: str = ""

    @property
    def proposal_kind(self) -> str:
        return "FOUNDATION_DOCUMENT"

    @property
    def execution_id(self) -> str:
        return self.onboarding_operation_id

    @property
    def content_digest(self) -> str:
        from tabularium.engine.persistence.model import digest
        return digest(self.content)

    def binding(self) -> str:
        from tabularium.engine.persistence.model import digest
        return digest(json.dumps({
            "proposal_id": self.proposal_id,
            "onboarding_operation_id": self.onboarding_operation_id,
            "project_root": self.project_root,
            "target_path": self.target_path,
            "operation": self.operation,
            "content_digest": self.content_digest,
            "expected_current_digest": self.expected_current_digest,
        }, sort_keys=True))

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "onboarding_operation_id": self.onboarding_operation_id,
            "project_root": self.project_root,
            "target_path": self.target_path,
            "operation": self.operation,
            "content_digest": self.content_digest,
            "expected_current_digest": self.expected_current_digest,
            "content_bytes": len(self.content.encode("utf-8")),
            "reason": self.reason,
        }


FOUNDATION_CURRENT_DIGEST_REQUIRED = "FOUNDATION_CURRENT_DIGEST_REQUIRED"
NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True)
class FoundationProposalBatch:
    """M7.2 adapter consumed by the existing M4.4 writer boundary."""

    proposal_id: str
    onboarding_operation_id: str
    project_root: str
    proposals: tuple[FoundationProposal, ...]

    @property
    def proposal_kind(self) -> str:
        return "FOUNDATION_BATCH"


@dataclass(frozen=True)
class OnboardingResult:
    status: str
    target: str
    profile: ProjectProfile | None = None
    proposal: OnboardingProposal | None = None
    code: str | None = None
    message: str | None = None
    created: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "target": self.target,
            "profile": self.profile.to_dict() if self.profile else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "created": list(self.created),
            "reason_codes": list(self.reason_codes),
        }
        if self.code:
            result["code"] = self.code
        if self.message:
            result["message"] = self.message
        return result


def _safe_label(value: str, limit: int = 120) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in value)
    return cleaned[:limit]


def _relative(root: Path, path: Path) -> str | None:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return None
        return _safe_label(resolved.relative_to(root).as_posix())
    except (OSError, RuntimeError, ValueError):
        return None


def _is_secret_filename(name: str) -> bool:
    lower = name.lower()
    return (
        lower == ".env"
        or lower.startswith(".env.")
        or lower in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
        or lower.endswith((".pem", ".key", ".p12", ".pfx"))
        or lower in {"credentials.json", "credential.json", "secrets.json"}
        or "credential" in lower
        or lower.startswith("secret")
    )


def _is_readme(name: str) -> bool:
    lower = name.lower()
    suffix = lower.rsplit(".", 1)[-1] if "." in lower else ""
    return lower.startswith("readme") and suffix in {"md", "markdown", "rst", "txt"}


def _is_metadata(name: str) -> bool:
    lower = name.lower()
    return (
        lower in PROJECT_TYPE_BY_FILE
        or lower.startswith("requirements") and lower.endswith((".txt", ".in"))
        or lower in {"poetry.lock", "pipfile.lock", "dockerfile"}
        or lower.startswith("compose") and lower.endswith((".yml", ".yaml"))
    )


def _is_ci_file(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    return ".github" in parts and "workflows" in parts


def _add_fact(facts: set[tuple[str, str, str, str]], kind: str,
              value: str, source: str,
              classification: str = "DIRECT_OBSERVATION") -> None:
    facts.add((kind, _safe_label(value), _safe_label(source), classification))


def _parse_metadata(text: str, name: str) -> bool:
    """Parse only to classify malformed metadata; never retain parsed values."""
    try:
        if name.lower().endswith(".json"):
            json.loads(text)
        elif name.lower().endswith(".toml"):
            import tomllib
            tomllib.loads(text)
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
    return True


def discover_project(raw_root: str | Path | None = None) -> ProjectDiscovery:
    """Inspect bounded project signals without copying source bodies."""
    root = _canonical_target(raw_root)
    facts: set[tuple[str, str, str, str]] = set()
    excluded = Counter()
    limits: set[str] = set()
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    safe_candidate_files = files_considered = files_inspected = files_excluded = 0
    safe_candidate_bytes = bytes_read = 0
    stopped = False

    git_entry = root / ".git"
    if git_entry.exists() and not git_entry.is_symlink():
        _add_fact(facts, "repository", "Git repository present", ".git")

    while queue and not stopped:
        directory, depth = queue.popleft()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            excluded["READ_ERROR"] += 1
            continue

        for entry in entries:
            if files_considered >= MAX_FILES:
                limits.add("MAX_FILES")
                stopped = True
                break

            name = entry.name
            try:
                is_link = entry.is_symlink()
                is_dir = entry.is_dir() if not is_link else False
                is_file = entry.is_file() if not is_link else False
            except OSError:
                excluded["READ_ERROR"] += 1
                continue

            if is_link:
                try:
                    resolved = entry.resolve(strict=True)
                    if not resolved.is_relative_to(root):
                        excluded["PATH_ESCAPE"] += 1
                    else:
                        excluded["SYMLINK_SKIPPED"] += 1
                except (OSError, RuntimeError, ValueError):
                    excluded["PATH_ESCAPE"] += 1
                continue

            if is_dir:
                if name.casefold() in IGNORED_DIRECTORIES:
                    excluded["IGNORED_DIRECTORY"] += 1
                    continue
                rel = _relative(root, entry)
                if rel is None:
                    excluded["PATH_ESCAPE"] += 1
                    continue
                lower = name.casefold()
                if lower in SOURCE_DIRECTORIES:
                    _add_fact(facts, "directory", "Common source directory", rel)
                elif lower in TEST_DIRECTORIES:
                    _add_fact(facts, "directory", "Test directory", rel)
                elif lower in DOC_DIRECTORIES:
                    _add_fact(facts, "directory", "Documentation directory", rel)
                if _is_ci_file(entry):
                    _add_fact(facts, "ci", "CI workflow directory", rel)
                if depth >= MAX_DEPTH:
                    limits.add("MAX_DEPTH")
                else:
                    queue.append((entry, depth + 1))
                continue

            if not is_file:
                continue

            rel = _relative(root, entry)
            if rel is None:
                excluded["PATH_ESCAPE"] += 1
                files_excluded += 1
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                excluded["READ_ERROR"] += 1
                files_excluded += 1
                continue
            if _is_secret_filename(name):
                excluded["SECRET_FILE"] += 1
                files_excluded += 1
                continue

            if size > MAX_FILE_SIZE:
                excluded["FILE_TOO_LARGE"] += 1
                limits.add("MAX_FILE_SIZE")
                files_excluded += 1
                continue

            files_available_before_inspection = safe_candidate_files
            safe_candidate_files += 1
            files_considered += 1
            safe_candidate_bytes += size

            if _is_readme(name):
                _add_fact(facts, "file", "README file present", rel)
                continue

            if _is_ci_file(entry):
                _add_fact(facts, "ci", "CI configuration present", rel)
                continue

            if not _is_metadata(name):
                continue

            if bytes_read + size > MAX_BYTES_READ:
                excluded["BYTE_LIMIT"] += 1
                limits.add("MAX_BYTES_READ")
                files_excluded += 1
                continue

            try:
                raw = entry.read_bytes()
                bytes_read += len(raw)
                files_inspected += 1
                if b"\x00" in raw:
                    safe_candidate_files = files_available_before_inspection
                    files_considered -= 1
                    safe_candidate_bytes -= size
                    excluded["BINARY_FILE"] += 1
                    files_excluded += 1
                    continue
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                excluded["READ_ERROR"] += 1
                files_excluded += 1
                continue

            lower = name.casefold()
            if not _parse_metadata(text, name):
                excluded["MALFORMED_METADATA"] += 1
            _add_fact(facts, "metadata", "Project metadata file present", rel)
            if lower in PROJECT_TYPE_BY_FILE:
                _add_fact(facts, "project_type", PROJECT_TYPE_BY_FILE[lower], rel)
            elif lower.startswith("requirements") or lower in {"poetry.lock", "pipfile.lock"}:
                _add_fact(facts, "project_type", "Python", rel)
            elif lower in {"dockerfile"} or lower.startswith("compose"):
                _add_fact(facts, "project_type", "Containerized", rel)

    fact_objects = tuple(
        DiscoveryFact(kind, value, source, classification)
        for kind, value, source, classification in sorted(facts)
    )
    project_types = tuple(sorted({fact.value for fact in fact_objects if fact.kind == "project_type"}))
    if not project_types:
        _add_fact(facts, "project_type", "Unknown", "bounded discovery")
        fact_objects = tuple(
            DiscoveryFact(kind, value, source, classification)
            for kind, value, source, classification in sorted(facts)
        )
        project_types = ("Unknown",)

    return ProjectDiscovery(
        project_root=str(root),
        project_types=project_types,
        facts=fact_objects,
        safe_candidate_files=safe_candidate_files,
        files_considered=files_considered,
        files_inspected=files_inspected,
        files_excluded=files_excluded,
        safe_candidate_bytes=safe_candidate_bytes,
        bytes_read=bytes_read,
        excluded_by_reason=tuple(sorted(excluded.items())),
        limits_reached=tuple(sorted(limits)),
    )


def _profile_for(discovery: ProjectDiscovery,
                 declarations: UserDeclarations | None = None) -> ProjectProfile:
    declaration_fields = declarations.fields_received() if declarations else ()
    base = {
        "discovered": discovery.to_dict(),
        "user_declaration_fields": list(declaration_fields),
        "authority_boundary": {
            "discovered_facts_are_project_truth": False,
            "user_declarations_are_project_truth": False,
            "persistence_requires_explicit_confirmation": True,
        },
    }
    raw = json.dumps(base, ensure_ascii=False, sort_keys=True).encode("utf-8")
    size = len(raw)
    ratio = None
    if discovery.safe_candidate_bytes > 0:
        ratio = round(max(0.0, 100.0 * (1.0 - size / discovery.safe_candidate_bytes)), 3)
    return ProjectProfile(
        discovery=discovery,
        declaration_fields=declaration_fields,
        profile_bytes=size,
        profile_tokens=math.ceil(size / 4),
        profile_vs_safe_candidate_reduction_pct=ratio,
    )


def _clean_declarations(values: Mapping[str, Any]) -> tuple[UserDeclarations | None, str | None, str | None]:
    cleaned: dict[str, Any] = {}
    for field_name in ("title", "purpose", "scope", "constraints"):
        value = " ".join(str(values.get(field_name, "")).split())
        if len(value) > 1000:
            return None, "DECLARATION_TOO_LONG", field_name
        if field_name != "constraints" and not value:
            return None, "DECLARATION_REQUIRED", field_name
        if "{{" in value or "}}" in value:
            return None, "DECLARATION_MARKER_REFUSED", field_name
        if value:
            try:
                from tabularium.engine.persistence.model import contains_secret
                if contains_secret(value):
                    return None, "SECRET_INPUT_REFUSED", field_name
            except ImportError:
                return None, "SECRET_CHECK_UNAVAILABLE", field_name
        cleaned[field_name] = value

    raw_cmd = values.get("test_command")
    if raw_cmd:
        if isinstance(raw_cmd, str):
            try:
                tokens = tuple(shlex.split(raw_cmd))
            except ValueError:
                tokens = ()
        elif isinstance(raw_cmd, (list, tuple)):
            tokens = tuple(str(x) for x in raw_cmd)
        else:
            tokens = ()
        from tabularium.change_config import ChangeConfigError, render_change_yaml
        try:
            render_change_yaml(tokens)
        except ChangeConfigError:
            return None, "TEST_COMMAND_INVALID", "test_command"
        cleaned["test_command"] = tokens
    else:
        cleaned["test_command"] = ()

    return UserDeclarations(**cleaned), None, None


def _needs_onboarding(project_text: str, current_state: str) -> bool:
    del current_state
    return all(marker in project_text for marker in (
        "No human-confirmed project title has been recorded yet",
        "No human-confirmed project purpose has been recorded yet",
        "No human-confirmed broad project scope has been recorded yet",
    ))


def _discovery_summary(discovery: ProjectDiscovery) -> str:
    lines = [
        "Observed project signals (not project declarations):",
        f"- Project type indicators: {', '.join(discovery.project_types)}",
        f"- Safe candidate files: {discovery.safe_candidate_files}",
        f"- Files considered: {discovery.files_considered}",
        f"- Metadata files inspected: {discovery.files_inspected}",
        f"- Bytes read: {discovery.bytes_read}",
    ]
    visible_facts = [
        fact for fact in discovery.facts
        if not (fact.source.startswith(".ai/") or fact.source == ".ai" or fact.source.startswith(".ai\\"))
    ]
    for fact in visible_facts[:12]:
        lines.append(f"- {fact.value}: `{fact.source}`")
    if discovery.limits_reached:
        lines.append("- Limits reached: " + ", ".join(discovery.limits_reached))
    lines.append("No source bodies or secret-bearing files are copied into onboarding or safe metrics.")
    return "\n".join(lines)


def _render_contents(discovery: ProjectDiscovery,
                     declarations: UserDeclarations) -> dict[str, str]:
    project = _resource_text("project/project.md")
    project = project.replace("{{ HUMAN-CONFIRMED PROJECT TITLE }}", declarations.title)
    project = project.replace("{{ HUMAN-CONFIRMED PROJECT PURPOSE }}", declarations.purpose)
    project = project.replace("{{ HUMAN-CONFIRMED BROAD PROJECT SCOPE }}", declarations.scope)
    project = project.replace(
        "All three markers must be replaced with explicitly confirmed content before\nthis file is stored as `.ai/project.md`. If any value is unknown, stop durable\nbootstrap; do not persist this template, a guess, or `TBD`.",
        "The title, purpose, and scope above were explicitly supplied during human-reviewed onboarding. Observed repository signals remain separate from this charter.",
    )

    constraints = _resource_text("project/constraints.md")
    if declarations.constraints:
        constraints = constraints.replace(
            "No project-specific constraints are currently recorded.\n\nThis statement describes the register. It does not assert that no\nproject-specific constraints exist. Replace it with a list only when\nproject-specific constraints have been explicitly confirmed; do not retain the\nsentence beside recorded constraints.",
            "The following constraint was explicitly confirmed during onboarding:\n\n- " + declarations.constraints,
        )

    current_state = _resource_text("project/current-state.md")
    current_state = current_state.replace(
        ".ai/tasks/{{ BOOTSTRAP_TASK_ID }}.md",
        "No automatic onboarding Task created by M7.2 project configuration",
    )
    current_state = current_state.replace(
        "{{ SOURCE-LINKED ZERO-STATE SUMMARY }}",
        _discovery_summary(discovery) +
        "\n\nThe observations above do not establish project intent or architecture.",
    )
    current_state = current_state.replace(
        "Before storing this file as `.ai/current-state.md`, replace both markers with\nthe canonical bootstrap/onboarding Task id and a freshly inspected,\nsource-linked summary. Do not copy a full repository inventory, infer Task\nstatus, claim validation truth, or convert observed implementation into intent.",
        "M7.2 configuration creates no Task. The summary above is freshly inspected\nand source-linked; it does not copy a full repository inventory, claim validation\ntruth, or convert observed implementation into intent.",
    )

    contents = {
        ".ai/project.md": _strip_template_banner(project, "project.md"),
        ".ai/constraints.md": constraints,
        ".ai/current-state.md": _strip_template_banner(current_state, "current-state.md"),
    }
    if declarations.test_command:
        from tabularium.change_config import render_change_yaml
        contents[CHANGE_CONFIG_PATH] = render_change_yaml(declarations.test_command)
    return contents


def _proposal_batch(target: Path, contents: Mapping[str, str]) -> FoundationProposalBatch:
    from tabularium.engine.persistence.model import digest
    content_digests = {
        relative: digest(text) for relative, text in sorted(contents.items())
    }
    operation_id = "ONBOARDING-" + digest(json.dumps({
        "project_root": str(target),
        "content_digests": content_digests,
    }, sort_keys=True))[:24]
    proposals = []
    for relative, text in sorted(contents.items()):
        if relative == CHANGE_CONFIG_PATH:
            # Only ever created, never read here: _proposal_for leaves an
            # existing file out of the proposal.
            op, expected = "CREATE", ""
        else:
            op = "UPDATE"
            expected = digest((target / relative).read_text(encoding="utf-8"))
        proposals.append(
            FoundationProposal(
                proposal_id=f"{operation_id}-{digest(relative)[:12]}",
                onboarding_operation_id=operation_id,
                project_root=str(target),
                target_path=relative,
                operation=op,
                content=text,
                reason="Human-confirmed project foundation onboarding",
                expected_current_digest=expected,
            )
        )
    return FoundationProposalBatch(
        proposal_id=operation_id,
        onboarding_operation_id=operation_id,
        project_root=str(target),
        proposals=tuple(proposals),
    )


def _proposal_for(target: Path, discovery: ProjectDiscovery,
                 declarations: UserDeclarations) -> OnboardingProposal:
    contents = _render_contents(discovery, declarations)
    if CHANGE_CONFIG_PATH in contents and os.path.lexists(target / CHANGE_CONFIG_PATH):
        # An existing .ai/change.yaml is the user's; onboarding keeps it.
        del contents[CHANGE_CONFIG_PATH]
    batch = _proposal_batch(target, contents)
    return OnboardingProposal(batch.onboarding_operation_id, batch.proposals)


def build_authorizations(proposal: OnboardingProposal, authorized_by: str,
                         authorized_at: str) -> dict[str, object]:
    """Build existing M4.4 authorizations from caller-supplied provenance."""
    from tabularium.engine.persistence.model import PersistenceAuthorization
    return {
        item.proposal_id: PersistenceAuthorization(
            proposal_id=item.proposal_id,
            execution_id=item.onboarding_operation_id,
            target_path=item.target_path,
            operation=item.operation,
            content_digest=item.content_digest,
            authorized_by=authorized_by,
            authorized_at=authorized_at,
            binding=item.binding(),
            note="Caller-supplied authorization for exact M7.2 foundation content",
        )
        for item in proposal.durable_proposals
    }


def apply_onboarding_proposal(target: Path, proposal: OnboardingProposal,
                              authorizations: Mapping[str, object]) -> tuple[str, ...] | str:
    """Apply exact foundation proposals (and a new .ai/change.yaml) through the M4.4 writer.

    One batch: the writer checks every member's authorization, secrets and
    current state before writing anything, and rolls back on a write error.
    """
    from tabularium.engine.persistence import writer
    from tabularium.change_config import ChangeConfigError, _StrictLoader, validate_change_data
    import yaml

    for item in proposal.durable_proposals:
        if item.target_path == CHANGE_CONFIG_PATH:
            try:
                validate_change_data(yaml.load(item.content, Loader=_StrictLoader))
            except (yaml.YAMLError, ChangeConfigError):
                return "CHANGE_CONFIG_INVALID"
    batch = FoundationProposalBatch(
        proposal_id=proposal.operation_id,
        onboarding_operation_id=proposal.operation_id,
        project_root=str(target),
        proposals=proposal.durable_proposals,
    )
    result = writer.apply_authorized_proposal(batch, str(target), authorizations)
    if result.status == "NO_CHANGE":
        return "NO_CHANGE"
    if result.status != "WRITTEN":
        return result.reason_codes[0] if result.reason_codes else "WRITE_FAILED"
    return tuple(item.target_path for item in proposal.durable_proposals)


def onboard_project(raw_target: str | Path | None = None,
                    declarations: Mapping[str, str] | None = None,
                    authorizations: Mapping[str, object] | None = None) -> OnboardingResult:
    """Discover, propose, and apply only exactly authorized onboarding content."""
    target = _canonical_target(raw_target)
    target_label = str(target)
    ai_dir = target / ".ai"
    classification = _classify_ai(ai_dir, target)
    if classification == "ABSENT":
        return OnboardingResult("NOT_INITIALIZED", target_label,
                                code="NOT_INITIALIZED",
                                message="run `tabu init` before onboarding")
    if classification == "PARTIAL":
        return OnboardingResult("PARTIAL_INITIALIZATION", target_label,
                                code="PARTIAL_INITIALIZATION",
                                message=".ai exists but required scaffold content is missing")
    if classification == "VERSION_MISMATCH":
        return OnboardingResult("VERSION_MISMATCH", target_label,
                                code="VERSION_MISMATCH",
                                message=".ai is governed by a different "
                                        "Tabularium ruleset version than this "
                                        "installation provides")
    if classification == "CONFLICT":
        return OnboardingResult("EXISTING_AI_DIRECTORY_CONFLICT", target_label,
                                code="EXISTING_AI_DIRECTORY_CONFLICT",
                                message=".ai exists but is malformed or redirected")

    try:
        from tabularium.engine.context_loader import load_context
        package = load_context(target)
    except Exception:
        return OnboardingResult("PROJECT_NOT_GOVERNED", target_label,
                                code="PROJECT_NOT_GOVERNED",
                                message="the production loader could not inspect this project")
    if package.entry_mode != "GOVERNED":
        return OnboardingResult("PROJECT_NOT_GOVERNED", target_label,
                                code="PROJECT_NOT_GOVERNED",
                                message="the project manifest or ruleset is not compatible")
    if package.package_status != "READY":
        return OnboardingResult("PROJECT_CONTEXT_BLOCKED", target_label,
                                code="PROJECT_CONTEXT_BLOCKED",
                                message="the production loader reported a blocked project context")

    project_text = (target / ".ai" / "project.md").read_text(encoding="utf-8")
    current_text = (target / ".ai" / "current-state.md").read_text(encoding="utf-8")
    discovery = discover_project(target)
    profile = _profile_for(discovery)
    reasons = []
    if discovery.limits_reached:
        reasons.append("DISCOVERY_LIMIT_REACHED")
    if discovery.project_types == ("Unknown",):
        reasons.append("UNKNOWN_PROJECT_TYPE")

    if not _needs_onboarding(project_text, current_text):
        message = "project declarations already exist; no files changed"
        if not os.path.lexists(target / CHANGE_CONFIG_PATH):
            # Onboarding never adds the test command to an onboarded project;
            # say so instead of dropping a supplied command without a word.
            supplied = bool(declarations and declarations.get("test_command"))
            message += (
                f". {CHANGE_CONFIG_PATH} is not configured, so `tabu run --change` skips tests"
                + ("; the supplied test command was not written" if supplied else "")
                + f". Onboarding does not add it to an onboarded project: create {CHANGE_CONFIG_PATH}"
                " by hand (see README)."
            )
        return OnboardingResult("ALREADY_ONBOARDED", target_label, profile=profile,
                                code="ALREADY_ONBOARDED",
                                message=message,
                                reason_codes=tuple(reasons))

    if declarations is None:
        return OnboardingResult("PROPOSAL_READY", target_label, profile=profile,
                                code="ONBOARDING_INPUT_REQUIRED",
                                message="human-confirmed title, purpose, scope, and exact authorization are required",
                                reason_codes=tuple(reasons))

    user_declarations, error, field_name = _clean_declarations(declarations)
    if user_declarations is None:
        return OnboardingResult("ONBOARDING_REFUSED", target_label, profile=profile,
                                code=error,
                                message=f"declaration refused: {field_name}",
                                reason_codes=tuple(reasons))
    profile = _profile_for(discovery, user_declarations)
    proposal = _proposal_for(target, discovery, user_declarations)
    kept_note = None
    if user_declarations.test_command and os.path.lexists(target / CHANGE_CONFIG_PATH):
        kept_note = (f"The existing {CHANGE_CONFIG_PATH} was kept; "
                     "the entered test command was not written.")
    if authorizations is None:
        message = "review the exact proposed contents, then provide caller-supplied authorization"
        return OnboardingResult("PROPOSAL_READY", target_label, profile=profile,
                                proposal=proposal, code="AUTHORIZATION_REQUIRED",
                                message=f"{message}. {kept_note}" if kept_note else message,
                                reason_codes=tuple(reasons))
    created = apply_onboarding_proposal(target, proposal, authorizations)
    if isinstance(created, str):
        return OnboardingResult("ONBOARDING_REFUSED", target_label,
                                profile=profile, proposal=proposal,
                                code=created,
                                message=("writing the onboarding files failed and was rolled back; no files were changed"
                                         if created == "WRITE_FAILED" else
                                         "exact onboarding authorization was refused; no files were changed"),
                                reason_codes=tuple(reasons))
    checked = load_context(target)
    if checked.entry_mode != "GOVERNED" or checked.package_status != "READY":
        return OnboardingResult("ONBOARDING_FAILED", target_label,
                                profile=profile, proposal=proposal, created=created,
                                code="POST_WRITE_LOADER_FAILED",
                                message="post-write production loader validation failed",
                                reason_codes=tuple(reasons))
    return OnboardingResult("ONBOARDED", target_label, profile=profile,
                            proposal=proposal, created=created,
                            message=kept_note,
                            reason_codes=tuple(reasons))


def prompt_declarations(input_func: Callable[[str], str] = input,
                        output: Callable[[str], None] = print) -> UserDeclarations | None:
    """Collect only explicit human declarations; EOF/cancel writes nothing."""
    try:
        output("\nHuman-confirmed project information")
        title = input_func("Project title: ")
        purpose = input_func("Project purpose: ")
        scope = input_func("Broad project scope: ")
        constraints = input_func("Project constraints (optional): ")
    except (EOFError, KeyboardInterrupt):
        return None

    test_command_tokens: tuple[str, ...] = ()
    prompt_str = "Test command for --change (optional, e.g. .venv/bin/python -m pytest -q): "
    for attempt in range(2):
        try:
            val = input_func(prompt_str).strip()
        except EOFError:
            # Ctrl-C is a cancel and reaches the caller like on every other prompt.
            val = ""
        if not val:
            break
        try:
            tokens = shlex.split(val)
            # The same rules `--change` applies when it loads the file later.
            from tabularium.change_config import render_change_yaml
            render_change_yaml(tokens)
            test_command_tokens = tuple(tokens)
            break
        except ValueError as exc:
            if attempt == 0:
                output(f"Invalid test command: {exc}; please try again.")
            else:
                output("Could not parse test command; change.yaml will not be written.")
                test_command_tokens = ()
                break

    declarations, error, field_name = _clean_declarations({
        "title": title, "purpose": purpose, "scope": scope,
        "constraints": constraints,
        "test_command": test_command_tokens,
    })
    if declarations is None:
        output(f"Onboarding refused: {error} ({field_name}).")
        return None
    return declarations


def render_proposal_preview(proposal: OnboardingProposal) -> str:
    """Render proposed durable files for exact draft preview."""
    lines = ["Proposed durable files:"]
    for p in proposal.durable_proposals:
        lines.append(f"- {p.target_path} [{p.operation}; digest {p.content_digest}]")
        lines.append("  Exact proposed content:")
        lines.extend(f"    {line}" for line in p.content.splitlines())
    return "\n".join(lines)


def render_human(result: OnboardingResult) -> str:
    lines = [f"Project onboarding: {result.status}", f"Target: {result.target}"]
    if result.profile and result.status != "ONBOARDED":
        lines.extend([
            "",
            _discovery_summary(result.profile.discovery),
            f"Profile bytes: {result.profile.profile_bytes}",
            f"Profile tokens: {result.profile.profile_tokens}",
            f"Safe-candidate reduction: {result.profile.profile_vs_safe_candidate_reduction_pct}%",
        ])
    if result.proposal and result.status != "ONBOARDED":
        # B6-D: the exact content is shown at the review/authorization step.
        # Once ONBOARDED, re-printing the identical authorized text is noise;
        # the durable outcome is listed under "Created or updated" and the
        # machine-readable proposal (digests included) is unchanged.
        lines.extend(["", "Proposed durable files:"])
        for proposal in result.proposal.durable_proposals:
            lines.append(f"- {proposal.target_path} [{proposal.operation}; digest {proposal.content_digest}]")
            lines.append("  Exact proposed content:")
            lines.extend(f"    {line}" for line in proposal.content.splitlines())
    if result.created:
        lines.extend(["", "Created or updated:"])
        lines.extend(f"- {path}" for path in result.created)
    if result.message:
        lines.extend(["", result.message])
    if result.reason_codes:
        # B6-D: on success these codes are discovery OBSERVATIONS (e.g. an
        # unrecognized project type), not failure causes; label them as notes
        # while every non-success status keeps "Reasons:". Values unchanged.
        label = ("Discovery notes:"
                 if result.status in {"ONBOARDED", "ALREADY_ONBOARDED"}
                 else "Reasons:")
        lines.append(f"{label} " + ", ".join(result.reason_codes))
    return "\n".join(lines)


def render_json(result: OnboardingResult) -> str:
    return json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True)
