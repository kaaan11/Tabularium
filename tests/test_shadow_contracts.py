"""Tests for the executable shadow contracts (M3.2).

These test the shadow layer on its own terms: version binding, per-family
baselines, representative malformed shapes, conditional and lifecycle rules,
determinism, and the isolation properties the shadow claims.

Behavioral equality with the frozen M2 validator is tested separately, in
test_shadow_conformance.py.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pydantic  # noqa: F401
except ImportError:  # pragma: no cover - environment without dev deps
    raise unittest.SkipTest(
        "pydantic is not installed; install tabularium/engine/requirements-dev.txt to "
        "run the shadow contract tests")

from tabularium.engine.shadow_contracts import (  # noqa: E402
    FAMILIES, SHADOW_MANIFEST_VERSION, SHADOW_VERSIONS, shadow_version,
    validate_manifest_shadow, validate_shadow)
from tabularium.engine.shadow_contracts.models import (  # noqa: E402
    FIELD_CLASS, SHADOW_MODEL)

# --- baselines --------------------------------------------------------------
#
# One valid record per family, mirroring the shapes the M2 conformance
# fixtures build. Every baseline must validate clean.

BASELINE = {
    "Task": {
        "schema_version": "1.10", "id": "TASK-2026-0001", "title": "Root",
        "objective": "Do it", "status": "IN_PROGRESS",
        "created_at": "2026-01-01T00:00:00Z", "created_by": "USER"},
    "Question": {
        "schema_version": "1.0", "id": "QST-2026-0001", "title": "Q",
        "question": "Why?", "status": "OPEN", "impact": "NON_BLOCKING",
        "created_at": "2026-01-02T00:00:00Z", "created_by": "human:user"},
    "Decision": {
        "schema_version": "1.2", "id": "DEC-2026-0001", "title": "D",
        "status": "APPROVED", "question": "What?",
        "approved_by": "human:user", "approved_at": "2026-01-03T01:00:00Z",
        "created_at": "2026-01-03T00:00:00Z", "created_by": "human:user"},
    "Investigation": {
        "schema_version": "1.4", "id": "INV-2026-0001", "title": "I",
        "status": "OPEN", "objective": "Find out",
        "created_at": "2026-01-04T00:00:00Z", "created_by": "ai:claude"},
    "Evidence": {
        "schema_version": "1.4", "id": "EVD-2026-0001", "title": "E",
        "source_type": "OBSERVATION", "status": "ACTIVE",
        "observation": "It failed.", "collected_at": "2026-01-05T00:00:00Z",
        "collected_by": "ai:claude"},
    "Handoff": {
        "schema_version": "1.0", "id": "HND-2026-0001", "title": "H",
        "task": "TASK-2026-0001", "created_at": "2026-01-06T00:00:00Z",
        "created_by": "ai:claude"},
    "Report": {
        "schema_version": "1.0", "id": "RPT-2026-0001", "title": "R",
        "task": "TASK-2026-0001", "created_at": "2026-01-07T00:00:00Z",
        "created_by": "ai:claude"},
}

MANIFEST_BASELINE = {"manifest_version": "1.0", "tabularium_ruleset": "0.1.0",
                     "title": "Fixture Project"}


def mut(family: str, **overrides) -> dict:
    """A baseline with fields overridden; `None` as a value means present-null."""
    return {**BASELINE[family], **overrides}


def without(family: str, *fields: str) -> dict:
    return {k: v for k, v in BASELINE[family].items() if k not in fields}


class ShadowTestCase(unittest.TestCase):
    def assertValid(self, family, data, msg=None):
        res = validate_shadow(family, data)
        self.assertTrue(res.valid,
                        msg or f"{family} expected valid; got {res.reason_codes}")

    def assertCodes(self, family, data, *expected):
        res = validate_shadow(family, data)
        self.assertEqual(res.reason_codes, tuple(sorted(set(expected))))
        self.assertFalse(res.valid)

    def assertHasCode(self, family, data, expected):
        res = validate_shadow(family, data)
        self.assertIn(expected, res.reason_codes)


# --- version binding --------------------------------------------------------

class TestVersionBinding(ShadowTestCase):

    def test_bound_versions_are_exactly_the_frozen_contract_versions(self):
        self.assertEqual(SHADOW_VERSIONS, {
            "Task": "1.10", "Evidence": "1.4", "Investigation": "1.4",
            "Decision": "1.2", "Question": "1.0", "Handoff": "1.0",
            "Report": "1.0"})
        self.assertEqual(SHADOW_MANIFEST_VERSION, "1.0")

    def test_every_family_has_a_model_and_a_binding(self):
        self.assertEqual(set(SHADOW_VERSIONS), set(SHADOW_MODEL))
        self.assertEqual(set(FAMILIES), set(SHADOW_MODEL))

    def test_unknown_family_is_refused_not_guessed(self):
        with self.assertRaises(KeyError):
            shadow_version("Worker")
        with self.assertRaises(KeyError):
            validate_shadow("Worker", {})

    def test_other_contract_version_acquires_no_shadow_semantics(self):
        # Compatibility is never inferred: a neighbouring version is not
        # "close enough", in either direction.
        for wrong in ("1.9", "1.11", "2.0", "1.10.0"):
            self.assertHasCode("Task", mut("Task", schema_version=wrong),
                               "SCHEMA_VERSION_UNSUPPORTED")

    def test_field_class_table_matches_declared_model_fields(self):
        for family, model in SHADOW_MODEL.items():
            self.assertEqual(set(model.model_fields), set(FIELD_CLASS[family]),
                             f"{family} field-class table drifted from model")


# --- valid baselines --------------------------------------------------------

class TestValidBaselines(ShadowTestCase):

    def test_every_family_baseline_is_valid(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                self.assertValid(family, BASELINE[family])

    def test_manifest_baseline_is_valid(self):
        self.assertTrue(validate_manifest_shadow(MANIFEST_BASELINE).valid)

    def test_optional_fields_may_be_populated(self):
        self.assertValid("Task", mut(
            "Task", type="IMPLEMENTATION", priority="HIGH", risk="MEDIUM",
            uncertainty_level="LOW", plan="A plan", project="proj",
            assigned_model="unspecified", result="done",
            updated_at="2026-01-02T00:00:00Z", tests=["t1"],
            actual_changes=["c1"], context_items=["ctx"], evaluations=["e"],
            parent_task="TASK-2026-0009", report="RPT-2026-0001",
            related_tasks=["TASK-2026-0002"], decisions=["DEC-2026-0001"],
            investigations=["INV-2026-0001"], questions=["QST-2026-0001"],
            evidence=["EVD-2026-0001"], handoffs=["HND-2026-0001"],
            reports=["RPT-2026-0001"], approval_required=False))

    def test_empty_reference_lists_are_valid(self):
        self.assertValid("Task", mut("Task", decisions=[], questions=[]))


# --- shape --------------------------------------------------------------

class TestShape(ShadowTestCase):

    def test_missing_required_field(self):
        self.assertCodes("Task", without("Task", "objective"),
                         "REQUIRED_STRING_INVALID:objective")

    def test_explicit_null_is_present_and_wrong_typed(self):
        # An explicit null is RECORDED AS NOTHING, which is a different fact
        # from an omitted field. It must not be silently treated as absent.
        self.assertCodes("Task", mut("Task", plan=None),
                         "OPTIONAL_STRING_TYPE:plan")
        self.assertCodes("Task", mut("Task", title=None),
                         "REQUIRED_STRING_INVALID:title")

    def test_integer_where_string_required(self):
        self.assertCodes("Task", mut("Task", title=7),
                         "REQUIRED_STRING_INVALID:title")

    def test_bool_is_not_a_string(self):
        self.assertCodes("Task", mut("Task", title=True),
                         "REQUIRED_STRING_INVALID:title")

    def test_non_bool_where_boolean_required(self):
        # Strict typing: "true" and 1 are not booleans. Coercion here would
        # manufacture a recorded value that was never written.
        for value in ("true", 1, 0, "yes"):
            with self.subTest(value=value):
                self.assertCodes("Task", mut("Task", approval_required=value),
                                 "BOOLEAN_TYPE:approval_required")

    def test_empty_and_whitespace_required_string(self):
        for value in ("", "   ", "\t\n"):
            with self.subTest(value=repr(value)):
                self.assertCodes("Task", mut("Task", objective=value),
                                 "REQUIRED_STRING_INVALID:objective")

    def test_unknown_field_is_a_contract_violation_not_an_extension(self):
        self.assertCodes("Task", mut("Task", velocity=3),
                         "UNRECOGNIZED_FIELD:velocity")

    def test_multiple_unknown_fields_are_reported_individually(self):
        self.assertCodes("Task", mut("Task", alpha=1, beta=2),
                         "UNRECOGNIZED_FIELD:alpha", "UNRECOGNIZED_FIELD:beta")

    def test_invalid_enum(self):
        self.assertCodes("Task", mut("Task", type="REFACTORING"),
                         "ENUM_INVALID:type")

    def test_status_is_both_a_required_string_and_an_enum(self):
        self.assertCodes("Task", mut("Task", status=5),
                         "REQUIRED_STRING_INVALID:status",
                         "ENUM_INVALID:status")

    def test_mapping_where_scalar_expected(self):
        self.assertCodes("Task", mut("Task", title={"a": 1}),
                         "REQUIRED_STRING_INVALID:title")

    def test_list_where_scalar_expected(self):
        self.assertCodes("Task", mut("Task", plan=["a"]),
                         "OPTIONAL_STRING_TYPE:plan")

    def test_scalar_where_list_expected(self):
        self.assertCodes("Task", mut("Task", tests="t1"),
                         "STRING_LIST_INVALID:tests")
        self.assertCodes("Task", mut("Task", decisions="DEC-2026-0001"),
                         "REF_LIST_TYPE:decisions")

    def test_string_list_with_non_string_entry(self):
        self.assertCodes("Task", mut("Task", tests=["ok", 3]),
                         "STRING_LIST_INVALID:tests")

    def test_malformed_single_reference(self):
        self.assertCodes("Task", mut("Task", parent_task="TASK-26-1"),
                         "REF_SINGLE_INVALID:parent_task")
        self.assertCodes("Task", mut("Task", parent_task=["TASK-2026-0002"]),
                         "REF_SINGLE_INVALID:parent_task")

    def test_reference_must_match_its_own_family(self):
        self.assertCodes("Task", mut("Task", parent_task="RPT-2026-0001"),
                         "REF_SINGLE_INVALID:parent_task")
        self.assertCodes("Task", mut("Task", decisions=["TASK-2026-0002"]),
                         "REF_LIST_ENTRY_INVALID:decisions")

    def test_malformed_reference_entry_collapses_to_one_code(self):
        self.assertCodes("Task", mut("Task", decisions=["bad1", "bad2"]),
                         "REF_LIST_ENTRY_INVALID:decisions")

    def test_malformed_id(self):
        for value in ("TASK-26-1", "QST-2026-0001", 7, None, ""):
            with self.subTest(value=value):
                self.assertHasCode("Task", mut("Task", id=value), "ID_INVALID")

    def test_schema_version_must_be_a_string(self):
        self.assertHasCode("Task", mut("Task", schema_version=1.10),
                           "SCHEMA_VERSION_TYPE")

    def test_input_that_is_not_a_mapping(self):
        for value in ([], "text", 7, None):
            with self.subTest(value=value):
                res = validate_shadow("Task", value)
                self.assertEqual(res.reason_codes, ("INPUT_NOT_MAPPING",))


# --- Task approval and archival --------------------------------------------

class TestTaskApproval(ShadowTestCase):

    def test_approval_required_without_approval_status_is_valid_structure(self):
        # THE M3.2 EDGE CASE. approval_required records that approval is
        # needed; approval_status records where approval stands. A Task may
        # require approval that has not yet been sought. Deriving
        # NOT_ACTIONABLE from this is M2's job, not the contract's.
        data = mut("Task", approval_required=True)
        self.assertNotIn("approval_status", data)
        self.assertNotIn("approved_scope", data)
        self.assertValid("Task", data)

    def test_approval_required_false_is_present_not_absent(self):
        self.assertValid("Task", mut("Task", approval_required=False))

    def test_approved_task_requires_both_provenance_fields(self):
        self.assertCodes("Task", mut("Task", approval_status="APPROVED"),
                         "APPROVAL_PROVENANCE_REQUIRED")
        self.assertCodes(
            "Task", mut("Task", approval_status="APPROVED",
                        approved_by="human:user"),
            "APPROVAL_PROVENANCE_REQUIRED")
        self.assertValid("Task", mut("Task", approval_status="APPROVED",
                                     approved_by="human:user",
                                     approved_at="2026-01-01T01:00:00Z"))

    def test_unapproved_task_must_not_carry_provenance(self):
        for status in ("NOT_REQUIRED", "PENDING", "REJECTED"):
            with self.subTest(status=status):
                self.assertCodes(
                    "Task", mut("Task", approval_status=status,
                                approved_by="human:user",
                                approved_at="2026-01-01T01:00:00Z"),
                    "APPROVAL_PROVENANCE_FORBIDDEN")

    def test_provenance_without_any_approval_status_is_forbidden(self):
        self.assertCodes("Task", mut("Task", approved_by="human:user",
                                     approved_at="2026-01-01T01:00:00Z"),
                         "APPROVAL_PROVENANCE_FORBIDDEN")

    def test_approved_provenance_must_be_non_empty(self):
        self.assertCodes(
            "Task", mut("Task", approval_status="APPROVED", approved_by="  ",
                        approved_at="2026-01-01T01:00:00Z"),
            "APPROVAL_PROVENANCE_EMPTY:approved_by")

    def test_malformed_approval_status_disables_its_own_rules(self):
        # A status the contract does not define cannot key a conditional rule.
        # It is reported as an invalid enum and nothing further is derived.
        self.assertCodes("Task", mut("Task", approval_status="MAYBE",
                                     approved_by="human:user"),
                         "ENUM_INVALID:approval_status")

    def test_approved_scope_requires_approval_status_approved(self):
        self.assertCodes("Task", mut("Task", approved_scope={}),
                         "APPROVED_SCOPE_REQUIRES_APPROVED")
        self.assertValid("Task", mut(
            "Task", approval_status="APPROVED", approved_by="human:user",
            approved_at="2026-01-01T01:00:00Z",
            approved_scope={"approved_files": ["a.py"],
                            "approved_operations": ["edit"],
                            "approved_risk": "LOW", "approved_plan": "p"}))

    def test_approved_scope_shape(self):
        def scope(value):
            return mut("Task", approval_status="APPROVED",
                       approved_by="human:user",
                       approved_at="2026-01-01T01:00:00Z",
                       approved_scope=value)

        self.assertCodes("Task", scope("everything"), "SCOPE_OBJECT_TYPE")
        self.assertCodes("Task", scope({"approved_everything": True}),
                         "SCOPE_UNRECOGNIZED_FIELD:approved_everything")
        self.assertCodes("Task", scope({"approved_files": "a.py"}),
                         "SCOPE_STRING_LIST_INVALID:approved_files")
        self.assertCodes("Task", scope({"approved_files": ["a.py", 2]}),
                         "SCOPE_STRING_LIST_INVALID:approved_files")
        self.assertCodes("Task", scope({"approved_risk": "EXTREME"}),
                         "SCOPE_RISK_INVALID")
        self.assertCodes("Task", scope({"approved_plan": ["p"]}),
                         "SCOPE_PLAN_TYPE")

    def test_archived_requires_outcome(self):
        self.assertCodes("Task", mut("Task", status="ARCHIVED"),
                         "ARCHIVED_OUTCOME_REQUIRED")
        self.assertValid("Task", mut("Task", status="ARCHIVED",
                                     archived_outcome="COMPLETED"))
        self.assertCodes("Task", mut("Task", status="ARCHIVED",
                                     archived_outcome="ABANDONED"),
                         "ENUM_INVALID:archived_outcome")

    def test_outcome_forbidden_unless_archived(self):
        self.assertCodes("Task", mut("Task", status="IN_PROGRESS",
                                     archived_outcome="COMPLETED"),
                         "ARCHIVED_OUTCOME_FORBIDDEN")

    def test_malformed_status_disables_archival_rules(self):
        self.assertCodes("Task", mut("Task", status="DONE",
                                     archived_outcome="COMPLETED"),
                         "ENUM_INVALID:status")


# --- other lifecycle rules --------------------------------------------------

class TestQuestionRules(ShadowTestCase):

    def test_answered_requires_full_answer_provenance(self):
        self.assertCodes(
            "Question", mut("Question", status="ANSWERED"),
            "ANSWER_PROVENANCE_REQUIRED:answer",
            "ANSWER_PROVENANCE_REQUIRED:answered_by",
            "ANSWER_PROVENANCE_REQUIRED:answered_at")
        self.assertValid("Question", mut(
            "Question", status="ANSWERED", answer="Because.",
            answered_by="human:user", answered_at="2026-01-03T00:00:00Z"))

    def test_answer_provenance_requires_answered_status(self):
        self.assertCodes("Question", mut("Question", status="OPEN",
                                         answer="Because."),
                         "ANSWER_PROVENANCE_FORBIDDEN")

    def test_impact_enum(self):
        self.assertCodes("Question", mut("Question", impact="CRITICAL"),
                         "ENUM_INVALID:impact")


class TestDecisionRules(ShadowTestCase):

    def test_approved_and_superseded_require_provenance(self):
        for status in ("APPROVED", "SUPERSEDED"):
            with self.subTest(status=status):
                data = mut("Decision", status=status)
                data.pop("approved_by")
                data.pop("approved_at")
                if status == "SUPERSEDED":
                    data["superseded_by"] = ["DEC-2026-0002"]
                self.assertHasCode("Decision", data,
                                   "DECISION_PROVENANCE_REQUIRED")

    def test_unapproved_decision_must_not_carry_provenance(self):
        for status in ("PROPOSED", "UNDER_REVIEW", "REJECTED", "WITHDRAWN"):
            with self.subTest(status=status):
                self.assertCodes("Decision", mut("Decision", status=status),
                                 "DECISION_PROVENANCE_FORBIDDEN")

    def test_superseded_requires_non_empty_superseded_by(self):
        self.assertCodes("Decision", mut("Decision", status="SUPERSEDED"),
                         "SUPERSEDED_BY_REQUIRED")
        self.assertCodes("Decision", mut("Decision", status="SUPERSEDED",
                                         superseded_by=[]),
                         "SUPERSEDED_BY_REQUIRED")
        self.assertValid("Decision", mut("Decision", status="SUPERSEDED",
                                         superseded_by=["DEC-2026-0002"]))

    def test_superseded_by_forbidden_otherwise(self):
        self.assertCodes("Decision", mut("Decision", status="APPROVED",
                                         superseded_by=["DEC-2026-0002"]),
                         "SUPERSEDED_BY_FORBIDDEN")

    def test_malformed_status_disables_provenance_rules(self):
        # The baseline carries provenance. A status outside the closed enum
        # cannot key the provenance rule, so only the enum failure is
        # reported — no provenance verdict is derived from an undefined state.
        self.assertCodes("Decision", mut("Decision", status="RATIFIED"),
                         "ENUM_INVALID:status")

    def test_decision_question_is_a_string_not_a_reference(self):
        self.assertValid("Decision", mut("Decision",
                                         question="Should we cache?"))
        self.assertCodes("Decision", mut("Decision", question=["Should we?"]),
                         "REQUIRED_STRING_INVALID:question")


class TestInvestigationRules(ShadowTestCase):

    def test_concluded_requires_conclusion_status(self):
        self.assertCodes("Investigation",
                         mut("Investigation", status="CONCLUDED"),
                         "CONCLUSION_STATUS_REQUIRED")
        self.assertValid("Investigation", mut(
            "Investigation", status="CONCLUDED",
            conclusion_status="SUPPORTED"))
        self.assertCodes("Investigation", mut(
            "Investigation", status="CONCLUDED", conclusion_status="PROVEN"),
            "ENUM_INVALID:conclusion_status")

    def test_abandoned_requires_no_valid_conclusion(self):
        self.assertCodes("Investigation",
                         mut("Investigation", status="ABANDONED"),
                         "ABANDONED_CONCLUSION_INVALID")
        self.assertValid("Investigation", mut(
            "Investigation", status="ABANDONED",
            conclusion_status="NO_VALID_CONCLUSION"))

    def test_open_investigation_may_carry_a_valid_conclusion_status(self):
        self.assertValid("Investigation", mut(
            "Investigation", status="OPEN", conclusion_status="INCONCLUSIVE"))
        self.assertCodes("Investigation", mut(
            "Investigation", status="OPEN", conclusion_status="MAYBE"),
            "ENUM_INVALID:conclusion_status")


class TestEvidenceRules(ShadowTestCase):

    def test_enums(self):
        self.assertCodes("Evidence", mut("Evidence", source_type="RUMOUR"),
                         "ENUM_INVALID:source_type")
        self.assertCodes("Evidence", mut("Evidence", reliability="PERFECT"),
                         "ENUM_INVALID:reliability")
        self.assertValid("Evidence", mut("Evidence", reliability="UNKNOWN"))

    def test_reliability_is_optional(self):
        self.assertValid("Evidence", BASELINE["Evidence"])


class TestImmutableRecordFamilies(ShadowTestCase):

    def test_handoff_and_report_have_no_status_field(self):
        for family in ("Handoff", "Report"):
            with self.subTest(family=family):
                self.assertNotIn("status", SHADOW_MODEL[family].model_fields)
                self.assertCodes(family, mut(family, status="OPEN"),
                                 "UNRECOGNIZED_FIELD:status")

    def test_task_binding_is_required_and_must_be_canonical(self):
        for family in ("Handoff", "Report"):
            with self.subTest(family=family):
                self.assertCodes(family, without(family, "task"),
                                 "REQUIRED_STRING_INVALID:task")
                self.assertCodes(family, mut(family, task="TASK-1"),
                                 "REF_SINGLE_INVALID:task")

    def test_body_sections_are_not_shadow_concerns(self):
        # The frozen contracts require exact body sections on Handoff and
        # Report. That is Markdown body semantics owned by the loader, so a
        # record valid here may still be rejected there. This test records
        # the boundary deliberately.
        self.assertValid("Handoff", BASELINE["Handoff"])
        self.assertValid("Report", BASELINE["Report"])


# --- manifest boundary ------------------------------------------------------

class TestManifestShadow(ShadowTestCase):

    def test_required_keys(self):
        res = validate_manifest_shadow({"manifest_version": "1.0"})
        self.assertEqual(res.reason_codes,
                         ("MANIFEST_MISSING_KEY:tabularium_ruleset",))

    def test_unknown_key(self):
        res = validate_manifest_shadow(
            {**MANIFEST_BASELINE, "token_budget": "1500"})
        self.assertEqual(res.reason_codes,
                         ("MANIFEST_UNKNOWN_KEY:token_budget",))

    def test_unsupported_version(self):
        res = validate_manifest_shadow({**MANIFEST_BASELINE,
                                        "manifest_version": "1.1"})
        self.assertEqual(res.reason_codes, ("MANIFEST_VERSION_UNSUPPORTED",))

    def test_value_types(self):
        res = validate_manifest_shadow({**MANIFEST_BASELINE,
                                        "tabularium_ruleset": 0.1})
        self.assertEqual(res.reason_codes,
                         ("MANIFEST_VALUE_TYPE:tabularium_ruleset",))
        res = validate_manifest_shadow({**MANIFEST_BASELINE,
                                        "tabularium_ruleset": "  "})
        self.assertEqual(res.reason_codes,
                         ("MANIFEST_VALUE_EMPTY:tabularium_ruleset",))

    def test_non_mapping_root(self):
        self.assertEqual(validate_manifest_shadow(["a"]).reason_codes,
                         ("MANIFEST_ROOT_TYPE",))

    def test_node_level_rules_are_out_of_scope(self):
        # Quoting style and duplicate keys are YAML NODE properties. They are
        # gone by the time a mapping exists, so the shadow cannot and must not
        # claim to check them; tabularium/engine/context_loader/manifest.py owns them.
        # An unquoted "0.1.0" parses to the float 0.1 and is caught only
        # because of its type, never because of its style.
        self.assertTrue(validate_manifest_shadow(
            {"manifest_version": "1.0", "tabularium_ruleset": "0.1.0"}).valid)


# --- shadow properties ------------------------------------------------------

class TestShadowProperties(ShadowTestCase):

    def test_input_is_never_mutated(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                data = mut(family, unknown_field=["x"], plan=None) \
                    if family == "Task" else mut(family, unknown_field=["x"])
                before = copy.deepcopy(data)
                validate_shadow(family, data)
                self.assertEqual(data, before)

    def test_manifest_input_is_never_mutated(self):
        data = {**MANIFEST_BASELINE, "extra": 1}
        before = copy.deepcopy(data)
        validate_manifest_shadow(data)
        self.assertEqual(data, before)

    def test_reason_codes_are_deterministic_and_sorted(self):
        data = mut("Task", zeta=1, alpha=2, title=None, tests="x")
        first = validate_shadow("Task", data)
        second = validate_shadow("Task", dict(reversed(list(data.items()))))
        self.assertEqual(first.reason_codes, second.reason_codes)
        self.assertEqual(list(first.reason_codes),
                         sorted(first.reason_codes))
        self.assertEqual(len(set(first.reason_codes)),
                         len(first.reason_codes))

    def test_result_is_frozen(self):
        res = validate_shadow("Task", BASELINE["Task"])
        with self.assertRaises(Exception):
            res.valid = False

    def test_shadow_does_not_import_the_loader(self):
        # The two implementations must stay independent, or the golden
        # conformance comparison would be comparing one thing with itself.
        for path in self.shadow_sources():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    with self.subTest(module=path.name, line=stripped):
                        self.assertNotIn("context_loader", stripped)

    def test_shadow_modules_touch_no_filesystem_or_network(self):
        forbidden = ("os", "os.path", "pathlib", "socket", "urllib",
                     "requests", "httpx", "subprocess", "sqlite3", "shutil")
        for path in self.shadow_sources():
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                module = stripped.split()[1].split(".")[0]
                with self.subTest(module=path.name, imports=module):
                    self.assertNotIn(module, forbidden)
                with self.subTest(module=path.name):
                    self.assertNotIn("open(", stripped)

    @staticmethod
    def shadow_sources():
        import tabularium.engine.shadow_contracts as pkg
        return sorted(Path(pkg.__file__).parent.glob("*.py"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
