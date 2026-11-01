"""Tests for Agent Skills packaging and the Tabularium governance sidecar (M3.4).

Package loading, the narrowing-only governance rule, worker and context
compatibility, filesystem safety, and token overhead.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import copy
import hashlib
import re
import shutil
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from tabularium.engine.projection import PAYLOAD_SECTIONS  # noqa: E402
from tabularium.engine.projection.budget import measure  # noqa: E402
from tabularium.engine.skills import (CONTEXT_CLASSES, GOVERNANCE_FIELDS,  # noqa: E402
                            OUTPUT_CONTRACTS, PERSISTENCE_NONE,
                            PERSISTENCE_PROPOSE, SIDECAR_FILENAME,
                            ControlPlaneGrant, check_payload, check_tools,
                            check_worker, evaluate, load_skill)
from tabularium.engine.skills.loader import _contained, _frontmatter  # noqa: E402
from tabularium.engine.skills.model import SIDECAR_FIELDS, SkillSidecar  # noqa: E402
from tabularium.engine.workers import (API_MODEL, CLI_CODING, LOCAL,  # noqa: E402
                             WORKER_CATEGORIES, WorkerDescriptor)

REFERENCE = Path(__file__).resolve().parents[1] / "examples" / "skills" \
    / "architecture-review"

VALID_SIDECAR = {
    "sidecar_version": "1.0",
    "allowed_context_classes": ["required", "constraints"],
    "allowed_worker_categories": ["CLI_CODING", "API_MODEL"],
    "allowed_tool_classes": ["NONE"],
    "approval_required": False,
    "output_contract": "REVIEW",
    "persistence": "PROPOSE",
}

VALID_SKILL_MD = """---
name: test-skill
version: "1.0"
description: A skill used only by tests.
---

# Test Skill

Instruction only.
"""


def sidecar(**overrides) -> dict:
    data = copy.deepcopy(VALID_SIDECAR)
    data.update(overrides)
    return data


def make_sidecar(**overrides) -> SkillSidecar:
    data = sidecar(**overrides)
    return SkillSidecar(
        sidecar_version=data["sidecar_version"],
        allowed_context_classes=tuple(data["allowed_context_classes"]),
        allowed_worker_categories=tuple(data["allowed_worker_categories"]),
        allowed_tool_classes=tuple(data["allowed_tool_classes"]),
        approval_required=data["approval_required"],
        output_contract=data["output_contract"],
        persistence=data["persistence"])


class PackageFixture:
    """A throwaway skill package directory."""

    def __init__(self, skill_md=VALID_SKILL_MD, sidecar_data=None,
                 sidecar_text=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="skillpkg-"))
        self.root = self.tmp / "test-skill"
        self.root.mkdir()
        if skill_md is not None:
            (self.root / "SKILL.md").write_text(skill_md, encoding="utf-8")
        if sidecar_text is not None:
            (self.root / SIDECAR_FILENAME).write_text(sidecar_text,
                                                      encoding="utf-8")
        elif sidecar_data is not None:
            (self.root / SIDECAR_FILENAME).write_text(
                yaml.safe_dump(sidecar_data, sort_keys=False),
                encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SkillTestCase(unittest.TestCase):
    def package(self, **kwargs) -> PackageFixture:
        if "sidecar_data" not in kwargs and "sidecar_text" not in kwargs:
            kwargs["sidecar_data"] = sidecar()
        fx = PackageFixture(**kwargs)
        self.addCleanup(fx.cleanup)
        return fx

    def assertCodes(self, result, *expected):
        for code in expected:
            self.assertIn(code, result.reason_codes)
        self.assertFalse(result.valid)


# --- reference package ------------------------------------------------------

class TestReferenceSkill(SkillTestCase):

    def test_reference_package_loads(self):
        result = load_skill(REFERENCE)
        self.assertTrue(result.valid, result.reason_codes)
        self.assertEqual(result.package.identity.name, "architecture-review")
        self.assertEqual(result.package.identity.version, "1.0")
        self.assertTrue(result.package.identity.description)

    def test_reference_package_uses_the_agent_skills_convention(self):
        self.assertTrue((REFERENCE / "SKILL.md").is_file())
        self.assertTrue((REFERENCE / "tabu.yaml").is_file())

    def test_reference_sidecar_has_exactly_the_defined_fields(self):
        raw = yaml.safe_load((REFERENCE / "tabu.yaml").read_text(
            encoding="utf-8"))
        self.assertEqual(set(raw), set(SIDECAR_FIELDS))
        # Six governance dimensions plus one format-version key that carries
        # no governance meaning.
        self.assertEqual(len(GOVERNANCE_FIELDS), 6)
        self.assertEqual(set(SIDECAR_FIELDS) - set(GOVERNANCE_FIELDS),
                         {"sidecar_version"})

    def test_reference_skill_declares_no_tools(self):
        package = load_skill(REFERENCE).package
        self.assertEqual(package.sidecar.allowed_tool_classes, ("NONE",))

    def test_skill_md_declares_no_governance(self):
        # SKILL.md may DISCUSS governance in prose — the reference skill talks
        # about the persistence gate. What it must not do is DECLARE any
        # governance field, because enforcement must never depend on parsing
        # instructional prose.
        text = (REFERENCE / "SKILL.md").read_text(encoding="utf-8")
        metadata, _ = _frontmatter(text)
        for field in GOVERNANCE_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, metadata)

    def test_skill_md_states_it_is_not_authority(self):
        text = (REFERENCE / "SKILL.md").read_text(encoding="utf-8").lower()
        self.assertIn("instruction, not authority", text)


# --- package validation -----------------------------------------------------

class TestPackageValidation(SkillTestCase):

    def test_valid_package(self):
        fx = self.package()
        self.assertTrue(load_skill(fx.root).valid)

    def test_missing_skill_md(self):
        fx = self.package(skill_md=None)
        self.assertCodes(load_skill(fx.root), "MISSING_INSTRUCTIONS")

    def test_missing_sidecar(self):
        fx = PackageFixture(sidecar_data=None)
        self.addCleanup(fx.cleanup)
        self.assertCodes(load_skill(fx.root), "MISSING_SIDECAR")

    def test_sidecar_without_a_sidecar_grants_nothing(self):
        # Losing the sidecar loses privileges; it never grants them.
        fx = PackageFixture(sidecar_data=None)
        self.addCleanup(fx.cleanup)
        self.assertIsNone(load_skill(fx.root).package)

    def test_malformed_yaml(self):
        fx = self.package(sidecar_text="allowed_worker_categories: [oops\n")
        self.assertCodes(load_skill(fx.root), "SIDECAR_UNPARSEABLE")

    def test_sidecar_not_a_mapping(self):
        fx = self.package(sidecar_text="- a\n- b\n")
        self.assertCodes(load_skill(fx.root), "SIDECAR_NOT_MAPPING")

    def test_unknown_field(self):
        fx = self.package(sidecar_data=sidecar(allow_everything=True))
        self.assertCodes(load_skill(fx.root),
                         "UNKNOWN_FIELD:allow_everything")

    def test_missing_field(self):
        data = sidecar()
        del data["persistence"]
        fx = self.package(sidecar_data=data)
        self.assertCodes(load_skill(fx.root), "MISSING_FIELD:persistence")

    def test_unsupported_sidecar_version(self):
        fx = self.package(sidecar_data=sidecar(sidecar_version="2.0"))
        self.assertCodes(load_skill(fx.root), "SIDECAR_VERSION_UNSUPPORTED")

    def test_invalid_worker_category(self):
        fx = self.package(
            sidecar_data=sidecar(allowed_worker_categories=["CLUSTER"]))
        self.assertCodes(load_skill(fx.root),
                         "INVALID_VALUE:allowed_worker_categories.CLUSTER")

    def test_human_worker_category_is_rejected(self):
        # A human is an authority, never an execution environment. The
        # vocabulary comes from M3.3, where HUMAN does not exist.
        for value in ("HUMAN", "USER", "REVIEWER", "APPROVER"):
            with self.subTest(value=value):
                fx = self.package(
                    sidecar_data=sidecar(allowed_worker_categories=[value]))
                self.assertCodes(
                    load_skill(fx.root),
                    f"INVALID_VALUE:allowed_worker_categories.{value}")

    def test_invalid_context_class(self):
        fx = self.package(
            sidecar_data=sidecar(allowed_context_classes=["EVERYTHING"]))
        self.assertCodes(load_skill(fx.root),
                         "INVALID_VALUE:allowed_context_classes.EVERYTHING")

    def test_invalid_output_contract(self):
        fx = self.package(sidecar_data=sidecar(output_contract="ANYTHING"))
        self.assertCodes(load_skill(fx.root), "INVALID_VALUE:output_contract")

    def test_invalid_persistence(self):
        for value in ("ALLOWED", "WRITE", "ALWAYS", True):
            with self.subTest(value=value):
                fx = self.package(sidecar_data=sidecar(persistence=value))
                self.assertCodes(load_skill(fx.root),
                                 "INVALID_VALUE:persistence")

    def test_approval_required_must_be_a_real_boolean(self):
        for value in ("false", "no", 0, None):
            with self.subTest(value=value):
                fx = self.package(sidecar_data=sidecar(approval_required=value))
                self.assertCodes(load_skill(fx.root),
                                 "INVALID_VALUE:approval_required")

    def test_invalid_skill_name(self):
        for name in ("Architecture Review", "ARCH", "arch_review", "", 7):
            with self.subTest(name=name):
                text = VALID_SKILL_MD.replace("name: test-skill",
                                              f"name: {name!r}")
                fx = self.package(skill_md=text)
                self.assertCodes(load_skill(fx.root), "INVALID_SKILL_NAME")

    def test_invalid_version(self):
        text = VALID_SKILL_MD.replace('version: "1.0"', 'version: "v1"')
        fx = self.package(skill_md=text)
        self.assertCodes(load_skill(fx.root), "INVALID_SKILL_VERSION")

    def test_missing_description(self):
        text = VALID_SKILL_MD.replace(
            "description: A skill used only by tests.", "description: '  '")
        fx = self.package(skill_md=text)
        self.assertCodes(load_skill(fx.root), "MISSING_DESCRIPTION")

    def test_missing_frontmatter(self):
        fx = self.package(skill_md="# No frontmatter\n\nBody.\n")
        self.assertCodes(load_skill(fx.root), "INSTRUCTIONS_UNPARSEABLE")

    def test_not_a_package(self):
        self.assertCodes(load_skill(Path(tempfile.gettempdir()) / "nope-xyz"),
                         "NOT_A_SKILL_PACKAGE")

    def test_load_is_deterministic(self):
        fx = self.package(sidecar_data=sidecar(allow_everything=True,
                                               persistence="WRITE"))
        first = load_skill(fx.root)
        second = load_skill(fx.root)
        self.assertEqual(first.reason_codes, second.reason_codes)
        self.assertEqual(list(first.reason_codes), sorted(first.reason_codes))


# --- secrets ----------------------------------------------------------------

class TestSecretRejection(SkillTestCase):

    def test_credential_like_fields_make_a_package_malformed(self):
        for key in ("api_key", "apiKey", "token", "secret", "password",
                    "credential", "provider_auth", "private_key",
                    "access_key", "bearer_token"):
            with self.subTest(key=key):
                fx = self.package(sidecar_data=sidecar(**{key: "x"}))
                result = load_skill(fx.root)
                self.assertFalse(result.valid)
                self.assertIn(f"SECRET_FIELD:{key}", result.reason_codes)

    def test_secret_is_rejected_not_silently_stripped(self):
        fx = self.package(sidecar_data=sidecar(api_key="x"))
        self.assertIsNone(load_skill(fx.root).package)

    def test_reference_package_contains_no_secret_like_key(self):
        raw = yaml.safe_load((REFERENCE / "tabu.yaml").read_text(
            encoding="utf-8"))
        for key in raw:
            for forbidden in ("key", "secret", "token", "password", "auth",
                              "credential"):
                with self.subTest(key=key, forbidden=forbidden):
                    self.assertNotIn(forbidden, key.lower())


# --- governance: narrowing only ---------------------------------------------

class TestGovernanceNarrowing(SkillTestCase):

    def test_skill_narrows_the_worker_set(self):
        result = evaluate(make_sidecar(allowed_worker_categories=["CLI_CODING"]),
                          ControlPlaneGrant(worker_categories=(CLI_CODING,
                                                               API_MODEL)))
        self.assertTrue(result.allowed)
        self.assertEqual(result.constraints.worker_categories, (CLI_CODING,))

    def test_skill_cannot_widen_the_worker_set(self):
        # The skill asks for all three; the control plane grants one.
        result = evaluate(
            make_sidecar(allowed_worker_categories=list(WORKER_CATEGORIES)),
            ControlPlaneGrant(worker_categories=(API_MODEL,)))
        self.assertEqual(result.constraints.worker_categories, (API_MODEL,))

    def test_disjoint_worker_sets_leave_nothing_to_run_on(self):
        result = evaluate(make_sidecar(allowed_worker_categories=["CLI_CODING"]),
                          ControlPlaneGrant(worker_categories=(LOCAL,)))
        self.assertFalse(result.allowed)
        self.assertIn("NO_ELIGIBLE_WORKER_CATEGORY", result.reason_codes)
        self.assertEqual(result.constraints.worker_categories, ())

    def test_context_classes_intersect(self):
        result = evaluate(
            make_sidecar(allowed_context_classes=["required", "constraints",
                                                  "decisions"]),
            ControlPlaneGrant(context_classes=("required", "decisions",
                                               "charter")))
        self.assertEqual(result.constraints.context_classes,
                         ("required", "decisions"))

    def test_skill_cannot_widen_context_classes(self):
        result = evaluate(
            make_sidecar(allowed_context_classes=list(CONTEXT_CLASSES)),
            ControlPlaneGrant(context_classes=("required",)))
        self.assertEqual(result.constraints.context_classes, ("required",))

    def test_tool_classes_intersect(self):
        result = evaluate(make_sidecar(allowed_tool_classes=["NONE"]),
                          ControlPlaneGrant(tool_classes=()))
        self.assertEqual(result.constraints.tool_classes, ())

    def test_intersection_order_follows_the_grant(self):
        # A skill must not influence anything by reordering its own list.
        forward = evaluate(
            make_sidecar(allowed_worker_categories=["CLI_CODING", "API_MODEL"]),
            ControlPlaneGrant(worker_categories=(API_MODEL, CLI_CODING)))
        reversed_ = evaluate(
            make_sidecar(allowed_worker_categories=["API_MODEL", "CLI_CODING"]),
            ControlPlaneGrant(worker_categories=(API_MODEL, CLI_CODING)))
        self.assertEqual(forward.constraints.worker_categories,
                         reversed_.constraints.worker_categories)
        self.assertEqual(forward.constraints.worker_categories,
                         (API_MODEL, CLI_CODING))


class TestApprovalSemantics(SkillTestCase):
    """approval_required is a LOWER bound: stricter only, never laxer."""

    def test_skill_false_cannot_override_control_plane_true(self):
        result = evaluate(make_sidecar(approval_required=False),
                          ControlPlaneGrant(approval_required=True))
        self.assertTrue(result.constraints.approval_required)
        self.assertIn("APPROVAL_REQUIRED", result.reason_codes)

    def test_skill_true_makes_approval_required(self):
        result = evaluate(make_sidecar(approval_required=True),
                          ControlPlaneGrant(approval_required=False))
        self.assertTrue(result.constraints.approval_required)

    def test_both_false_requires_no_approval(self):
        result = evaluate(make_sidecar(approval_required=False),
                          ControlPlaneGrant(approval_required=False))
        self.assertFalse(result.constraints.approval_required)
        self.assertNotIn("APPROVAL_REQUIRED", result.reason_codes)

    def test_approval_is_computed_never_granted(self):
        # M3.4 computes the effective requirement only. Nothing here asks a
        # human, records an approval, or resumes after one.
        result = evaluate(make_sidecar(approval_required=True))
        self.assertTrue(result.allowed)
        self.assertTrue(result.constraints.approval_required)


class TestPersistenceSemantics(SkillTestCase):
    """persistence is an UPPER bound on what may be REQUESTED."""

    def test_skill_cannot_widen_control_plane_persistence(self):
        result = evaluate(make_sidecar(persistence=PERSISTENCE_PROPOSE),
                          ControlPlaneGrant(persistence=PERSISTENCE_NONE))
        self.assertEqual(result.constraints.persistence, PERSISTENCE_NONE)

    def test_skill_can_narrow_persistence(self):
        result = evaluate(make_sidecar(persistence=PERSISTENCE_NONE),
                          ControlPlaneGrant(persistence=PERSISTENCE_PROPOSE))
        self.assertEqual(result.constraints.persistence, PERSISTENCE_NONE)

    def test_propose_requires_both_sides(self):
        result = evaluate(make_sidecar(persistence=PERSISTENCE_PROPOSE),
                          ControlPlaneGrant(persistence=PERSISTENCE_PROPOSE))
        self.assertEqual(result.constraints.persistence, PERSISTENCE_PROPOSE)

    def test_vocabulary_cannot_express_write_authority(self):
        # Gate condition 6 requires explicit human authorization for the exact
        # write. No value checked into a repository can satisfy that, so no
        # such value exists.
        from tabularium.engine.skills.model import PERSISTENCE_LEVELS
        self.assertEqual(set(PERSISTENCE_LEVELS),
                         {PERSISTENCE_NONE, PERSISTENCE_PROPOSE})
        for forbidden in ("ALLOWED", "WRITE", "COMMIT", "AUTO"):
            self.assertNotIn(forbidden, PERSISTENCE_LEVELS)


class TestOutputContract(SkillTestCase):

    def test_output_contract_passes_through_when_granted(self):
        result = evaluate(make_sidecar(output_contract="REVIEW"))
        self.assertEqual(result.constraints.output_contract, "REVIEW")

    def test_output_contract_mismatch_is_denied(self):
        result = evaluate(make_sidecar(output_contract="REVIEW"),
                          ControlPlaneGrant(output_contracts=("CODE_CHANGE",)))
        self.assertFalse(result.allowed)
        self.assertIn("OUTPUT_CONTRACT_MISMATCH:REVIEW", result.reason_codes)
        self.assertIsNone(result.constraints.output_contract)

    def test_output_contract_is_not_a_record_contract(self):
        for record in ("Evidence", "Decision", "Report", "Handoff", "Task"):
            self.assertNotIn(record.upper(), OUTPUT_CONTRACTS)


# --- compatibility ----------------------------------------------------------

class TestWorkerCompatibility(SkillTestCase):

    def setUp(self):
        self.constraints = evaluate(make_sidecar()).constraints

    def test_compatible_worker(self):
        result = check_worker(self.constraints,
                              WorkerDescriptor("w", CLI_CODING))
        self.assertTrue(result.compatible)
        self.assertEqual(result.reason_codes, ())

    def test_denied_worker(self):
        result = check_worker(self.constraints, WorkerDescriptor("w", LOCAL))
        self.assertFalse(result.compatible)
        self.assertEqual(result.reason_codes, ("WORKER_CATEGORY_DENIED:LOCAL",))

    def test_check_is_compatibility_not_routing(self):
        # Two eligible workers both pass; nothing here chooses between them.
        for category in (CLI_CODING, API_MODEL):
            with self.subTest(category=category):
                self.assertTrue(check_worker(
                    self.constraints, WorkerDescriptor("w", category)).compatible)


class TestContextCompatibility(SkillTestCase):

    def setUp(self):
        self.constraints = evaluate(
            make_sidecar(allowed_context_classes=["required",
                                                  "constraints"])).constraints

    def test_allowed_payload(self):
        class Payload:
            def section_names(self):
                return ("required", "constraints")
        self.assertTrue(check_payload(self.constraints, Payload()).compatible)

    def test_denied_payload_names_the_offending_class(self):
        class Payload:
            def section_names(self):
                return ("required", "constraints", "current_state")
        result = check_payload(self.constraints, Payload())
        self.assertFalse(result.compatible)
        self.assertEqual(result.reason_codes,
                         ("CONTEXT_CLASS_DENIED:current_state",))

    def test_payload_is_not_stripped_or_reprojected(self):
        # Silently removing a section after projection would leave the
        # payload's reported economy metrics describing something else. The
        # fix belongs upstream, in projection policy.
        class Payload:
            def __init__(self):
                self.names = ["required", "charter"]

            def section_names(self):
                return tuple(self.names)
        payload = Payload()
        check_payload(self.constraints, payload)
        self.assertEqual(payload.names, ["required", "charter"])

    def test_context_classes_are_payload_sections_not_a_second_taxonomy(self):
        self.assertEqual(CONTEXT_CLASSES, PAYLOAD_SECTIONS)

    def test_real_projected_payload_is_checkable(self):
        from tabularium.engine.projection import project
        from tests.test_projection import INTENT, small_project
        fx = small_project()
        self.addCleanup(fx.cleanup)
        result = project(fx.load(task="TASK-2026-0001"), intent=INTENT)
        wide = evaluate(make_sidecar(
            allowed_context_classes=list(CONTEXT_CLASSES))).constraints
        self.assertTrue(check_payload(wide, result.payload).compatible)
        narrow = evaluate(
            make_sidecar(allowed_context_classes=["required"])).constraints
        self.assertFalse(check_payload(narrow, result.payload).compatible)


class TestToolCompatibility(SkillTestCase):

    def test_requested_tool_outside_the_effective_set_is_denied(self):
        constraints = evaluate(make_sidecar(),
                               ControlPlaneGrant(tool_classes=("NONE",))
                               ).constraints
        self.assertTrue(check_tools(constraints, ()).compatible)
        result = check_tools(constraints, ("WRITE_FS",))
        self.assertFalse(result.compatible)
        self.assertEqual(result.reason_codes, ("TOOL_CLASS_DENIED:WRITE_FS",))


# --- determinism and immutability -------------------------------------------

class TestDeterminismAndImmutability(SkillTestCase):

    def test_evaluation_is_deterministic(self):
        sidecar_obj = make_sidecar()
        grant = ControlPlaneGrant()
        self.assertEqual(evaluate(sidecar_obj, grant).to_dict(),
                         evaluate(sidecar_obj, grant).to_dict())

    def test_evaluation_does_not_mutate_its_inputs(self):
        sidecar_obj = make_sidecar()
        grant = ControlPlaneGrant(worker_categories=(CLI_CODING, API_MODEL))
        before = (copy.deepcopy(sidecar_obj.to_dict()),
                  copy.deepcopy(grant.worker_categories))
        evaluate(sidecar_obj, grant)
        self.assertEqual(sidecar_obj.to_dict(), before[0])
        self.assertEqual(grant.worker_categories, before[1])

    def test_result_types_are_frozen(self):
        result = evaluate(make_sidecar())
        with self.assertRaises(FrozenInstanceError):
            result.allowed = False
        with self.assertRaises(FrozenInstanceError):
            result.constraints.persistence = PERSISTENCE_PROPOSE

    def test_skill_identity_implies_no_authority(self):
        package = load_skill(REFERENCE).package
        fields = set(type(package.identity).__dataclass_fields__)
        self.assertEqual(fields, {"name", "version", "description"})
        for forbidden in ("provider", "model", "credentials", "api_key",
                          "authority", "approved_by", "path"):
            self.assertNotIn(forbidden, fields)


# --- filesystem safety ------------------------------------------------------

class TestFilesystemSafety(SkillTestCase):

    def test_path_escape_is_rejected(self):
        root = REFERENCE.resolve()
        for name in ("../../VERSION", "..", "../tabu.yaml",
                     str(Path(root.anchor) / "Windows" / "system.ini"),
                     "../../../etc/passwd"):
            with self.subTest(name=name):
                self.assertIsNone(_contained(root, name))

    def test_contained_paths_are_accepted(self):
        root = REFERENCE.resolve()
        self.assertIsNotNone(_contained(root, "SKILL.md"))
        self.assertIsNotNone(_contained(root, "tabu.yaml"))

    def test_loading_writes_nothing(self):
        fx = self.package()
        def snapshot():
            return {str(p.relative_to(fx.root)):
                    hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(fx.root.rglob("*")) if p.is_file()}
        before = snapshot()
        load_skill(fx.root)
        load_skill(fx.root, include_instructions=True)
        self.assertEqual(snapshot(), before)

    def test_reference_package_is_not_modified_by_loading(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(REFERENCE.iterdir()) if p.is_file()}
        load_skill(REFERENCE, include_instructions=True)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(REFERENCE.iterdir()) if p.is_file()}
        self.assertEqual(after, before)

    def test_skills_package_performs_no_network_and_no_execution(self):
        forbidden = ("socket", "urllib", "requests", "httpx", "subprocess",
                     "importlib", "runpy", "pickle", "shutil")
        root = Path(__file__).resolve().parents[1] / "tabularium" / "engine" / "skills"
        for path in sorted(root.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    with self.subTest(module=path.name, line=stripped):
                        self.assertNotIn(stripped.split()[1].split(".")[0],
                                         forbidden)
                        self.assertNotIn("context_loader", stripped)
            # `re.compile` is fine; a bare `compile(` is not, hence the
            # word-boundary match rather than a substring search.
            for call in (r"\bexec\(", r"\beval\(", r"(?<!re\.)\bcompile\(",
                         r"\.write_text\(", r"\.write_bytes\(", r"\.mkdir\(",
                         r"\.unlink\(", r"\.rmdir\(", r"\.touch\("):
                with self.subTest(module=path.name, call=call):
                    self.assertIsNone(re.search(call, text))


# --- token economy ----------------------------------------------------------

class TestTokenOverhead(SkillTestCase):

    def test_sidecar_is_small(self):
        raw = (REFERENCE / "tabu.yaml").read_text(encoding="utf-8")
        size = measure(raw)
        self.assertLess(size["estimated_tokens"], 400,
                        f"sidecar grew to {size} — it is governance metadata, "
                        "not documentation")

    def test_governance_metadata_alone_is_tiny(self):
        # What actually travels is the parsed sidecar, not its comments.
        package = load_skill(REFERENCE).package
        size = measure(package.sidecar.to_dict())
        self.assertLess(size["estimated_tokens"], 120)

    def test_instructions_are_not_loaded_until_the_skill_is_selected(self):
        package = load_skill(REFERENCE).package
        self.assertIsNone(package.instructions)
        self.assertFalse(package.instructions_loaded)
        selected = load_skill(REFERENCE, include_instructions=True).package
        self.assertTrue(selected.instructions_loaded)
        self.assertIn("Architecture Review", selected.instructions)

    def test_instructions_cost_is_measured_and_bounded(self):
        package = load_skill(REFERENCE, include_instructions=True).package
        size = measure(package.instructions)
        self.assertLess(size["estimated_tokens"], 1200,
                        "a single skill must not consume a whole HOT budget")

    def test_loading_one_skill_pulls_in_no_other_skill(self):
        package = load_skill(REFERENCE, include_instructions=True).package
        self.assertEqual(package.path, "architecture-review")
        siblings = [p.name for p in REFERENCE.parent.iterdir() if p.is_dir()]
        for other in siblings:
            if other != "architecture-review":
                with self.subTest(other=other):
                    self.assertNotIn(other, package.instructions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DuplicateSidecarKeyTests(unittest.TestCase):
    def test_duplicate_governance_key_invalidates_the_skill(self):
        # `approval_required: true` then `false`: safe_load keeps the last value,
        # silently dropping the lower bound a reader saw first.
        import shutil
        import tempfile
        from tabularium.engine.skills.model import SIDECAR_UNPARSEABLE
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "skill"
            shutil.copytree(REFERENCE, skill)
            with (skill / "tabu.yaml").open("a", encoding="utf-8") as f:
                f.write("\napproval_required: true\napproval_required: false\n")
            result = load_skill(skill)
        self.assertFalse(result.valid)
        self.assertIn(SIDECAR_UNPARSEABLE, result.reason_codes)
