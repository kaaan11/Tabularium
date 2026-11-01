"""Golden conformance: frozen M2 validator vs Pydantic shadow contracts.

WHAT THIS TEST IS FOR

`tabularium/engine/context_loader/validate.py` is production behavior and stays that
way. `tabularium/engine/shadow_contracts/` is an independent executable expression of
the same consumed boundary. This module runs both over one corpus and asserts
they agree, so that implementation drift is caught by a failing test rather
than discovered later in a governed decision.

The two implementations share no code and no tables by design. If the shadow
imported the frozen validator's tables, this comparison would be comparing one
thing with itself.

WHAT IS COMPARED

1. ACCEPT vs REJECT — the primary invariant. The shadow must not accept a
   consumed shape the frozen validator rejects, nor reject one it accepts.

2. NORMALIZED TABULARIUM REASON CATEGORY — which rule fired. The frozen validator
   emits human-readable prose that was never designed as a machine contract,
   so it is translated by the documented map below before comparison. Pydantic
   prose is never compared to anything; the shadow emits Tabularium codes only.

WHAT IS NOT COMPARED

Markdown body semantics. The frozen validator checks required body sections
for Handoff and Report; the shadow deliberately does not model bodies. Every
corpus record is therefore given a conforming body, and the boundary itself is
asserted separately in `TestBoundary`.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pydantic  # noqa: F401
except ImportError:  # pragma: no cover - environment without dev deps
    raise unittest.SkipTest(
        "pydantic is not installed; install tabularium/engine/requirements-dev.txt to "
        "run the golden conformance tests")

from tabularium.engine.context_loader import manifest as frozen_manifest  # noqa: E402
from tabularium.engine.context_loader import validate as frozen  # noqa: E402
from tabularium.engine.shadow_contracts import (SHADOW_MANIFEST_VERSION,  # noqa: E402
                                      SHADOW_VERSIONS,
                                      validate_manifest_shadow,
                                      validate_shadow)
from tests.test_shadow_contracts import (BASELINE,  # noqa: E402
                                                 mut, without)

# --- normalization map ------------------------------------------------------
#
# Frozen validator prose -> Tabularium reason code. This map is the documented
# normalization boundary: it exists because the frozen messages are prose, not
# because either implementation is being bent to match the other. Every
# message the frozen validator can emit must be matched here; an unmatched
# message fails the test rather than being silently dropped.

BODY_SECTION_MISSING = "BODY_SECTION_MISSING"

EXACT = {
    "schema_version must be an explicitly quoted string": "SCHEMA_VERSION_TYPE",
    "id is not a canonical record id": "ID_INVALID",
    "approval_required must be a boolean": "BOOLEAN_TYPE:approval_required",
    "approved_scope must be an object when present": "SCOPE_OBJECT_TYPE",
    "approved_scope.approved_risk is not LOW/MEDIUM/HIGH/CRITICAL":
        "SCOPE_RISK_INVALID",
    "approved_scope.approved_plan must be a string": "SCOPE_PLAN_TYPE",
    "approved_scope must be absent unless approval_status is APPROVED":
        "APPROVED_SCOPE_REQUIRES_APPROVED",
    "archived_outcome is required": "ARCHIVED_OUTCOME_REQUIRED",
    "archived_outcome must be absent unless ARCHIVED":
        "ARCHIVED_OUTCOME_FORBIDDEN",
    "answer provenance requires status ANSWERED":
        "ANSWER_PROVENANCE_FORBIDDEN",
    "SUPERSEDED requires non-empty superseded_by": "SUPERSEDED_BY_REQUIRED",
    "superseded_by must be absent unless SUPERSEDED": "SUPERSEDED_BY_FORBIDDEN",
    "conclusion_status is required": "CONCLUSION_STATUS_REQUIRED",
    "ABANDONED requires conclusion_status NO_VALID_CONCLUSION":
        "ABANDONED_CONCLUSION_INVALID",
}

# Ordered: the "to be absent" variants must be tried before the plain ones.
PATTERNS = [
    (r"^schema_version .+ is not the supported .+ contract .+$",
     lambda m: ["SCHEMA_VERSION_UNSUPPORTED"]),
    (r"^approval_status \S+ requires approved_by and approved_at to be absent$",
     lambda m: ["APPROVAL_PROVENANCE_FORBIDDEN"]),
    (r"^approval_status \S+ requires approved_by and approved_at$",
     lambda m: ["APPROVAL_PROVENANCE_REQUIRED"]),
    (r"^approval_status APPROVED requires non-empty (\w+)$",
     lambda m: [f"APPROVAL_PROVENANCE_EMPTY:{m.group(1)}"]),
    (r"^status \S+ requires approved_by and approved_at to be absent$",
     lambda m: ["DECISION_PROVENANCE_FORBIDDEN"]),
    (r"^status \S+ requires approved_by and approved_at$",
     lambda m: ["DECISION_PROVENANCE_REQUIRED"]),
    (r"^status \S+ requires non-empty (\w+)$",
     lambda m: [f"DECISION_PROVENANCE_EMPTY:{m.group(1)}"]),
    (r"^ANSWERED requires (\w+)$",
     lambda m: [f"ANSWER_PROVENANCE_REQUIRED:{m.group(1)}"]),
    (r"^(\w+) is required and must be a non-empty string$",
     lambda m: [f"REQUIRED_STRING_INVALID:{m.group(1)}"]),
    (r"^unrecognized frontmatter fields: (.+)$",
     lambda m: [f"UNRECOGNIZED_FIELD:{f}" for f in m.group(1).split(", ")]),
    (r"^approved_scope contains unrecognized fields: (.+)$",
     lambda m: [f"SCOPE_UNRECOGNIZED_FIELD:{f}"
                for f in m.group(1).split(", ")]),
    (r"^approved_scope\.(\w+) must be a list of strings$",
     lambda m: [f"SCOPE_STRING_LIST_INVALID:{m.group(1)}"]),
    (r"^(\w+) must be a string when present$",
     lambda m: [f"OPTIONAL_STRING_TYPE:{m.group(1)}"]),
    (r"^(\w+) must be a list of strings when present$",
     lambda m: [f"STRING_LIST_INVALID:{m.group(1)}"]),
    (r"^(\w+) must be a single canonical \w+ id$",
     lambda m: [f"REF_SINGLE_INVALID:{m.group(1)}"]),
    (r"^(\w+) must be a list of canonical \w+ ids$",
     lambda m: [f"REF_LIST_TYPE:{m.group(1)}"]),
    (r"^(\w+) contains a value that is not a canonical \w+ id$",
     lambda m: [f"REF_LIST_ENTRY_INVALID:{m.group(1)}"]),
    (r"^(\w+) is not one of .+$",
     lambda m: [f"ENUM_INVALID:{m.group(1)}"]),
    (r"^body requires exact section '## .+'$",
     lambda m: [BODY_SECTION_MISSING]),
]

COMPILED = [(re.compile(p), fn) for p, fn in PATTERNS]


def normalize(messages) -> tuple[str, ...]:
    """Translate frozen validator prose into Tabularium reason codes."""
    codes: list[str] = []
    for message in messages:
        if message in EXACT:
            codes.append(EXACT[message])
            continue
        for pattern, build in COMPILED:
            match = pattern.match(message)
            if match:
                codes.extend(build(match))
                break
        else:
            raise AssertionError(
                f"frozen validator emitted a message the normalization map "
                f"does not cover: {message!r}. Extend the map deliberately; "
                f"do not drop the message.")
    return tuple(sorted(set(codes)))


# --- corpus -----------------------------------------------------------------
#
# Representative, boundary-focused mutations. Deliberately not a Cartesian
# product: each case isolates one defect class so a divergence names a rule
# rather than a pile of interacting ones.

VALID_BODY = {
    "Task": "# Task\n\nBody.\n",
    "Question": "# Question\n\nBody.\n",
    "Decision": "# Decision\n\nBody.\n",
    "Investigation": "# Investigation\n\n### H1\n\nFirst.\n\n"
                     "## Current conclusion\n\nNone yet.\n",
    "Evidence": "# Evidence\n\nNarrative.\n",
    "Handoff": "# Handoff\n\n## Current State\n\nIn progress.\n\n"
               "## Continuation\n\nContinue safely.\n",
    "Report": "# Report\n\n## Outcome\n\nDone.\n\n## Validation\n\nChecked.\n\n"
              "## Limitations\n\nNone known.\n",
}

APPROVED = {"approval_status": "APPROVED", "approved_by": "human:user",
            "approved_at": "2026-01-01T01:00:00Z"}


def build_corpus() -> list[tuple[str, str, dict]]:
    """(family, label, frontmatter) covering every defect class in scope."""
    cases: list[tuple[str, str, dict]] = []

    def add(family, label, data):
        cases.append((family, label, data))

    # --- valid baselines, one per family
    for family, data in BASELINE.items():
        add(family, "valid-baseline", data)

    # --- version binding
    for family in BASELINE:
        add(family, "version-unsupported", mut(family, schema_version="9.9"))
        add(family, "version-not-a-string", mut(family, schema_version=1.0))
        add(family, "version-missing", without(family, "schema_version"))

    # --- canonical id
    for family in BASELINE:
        add(family, "id-malformed", mut(family, id="XXX-1"))
        add(family, "id-wrong-family", mut(family, id="TASK-2026-0001")
            if family != "Task" else mut(family, id="RPT-2026-0001"))
        add(family, "id-missing", without(family, "id"))
        add(family, "id-null", mut(family, id=None))

    # --- required fields: missing, null, empty, wrong primitive
    for family, fields in frozen.REQUIRED_STRINGS.items():
        add(family, "required-missing", without(family, fields[0]))
        add(family, "required-null", mut(family, **{fields[0]: None}))
        add(family, "required-empty", mut(family, **{fields[0]: ""}))
        add(family, "required-whitespace", mut(family, **{fields[0]: "   "}))
        add(family, "required-integer", mut(family, **{fields[0]: 7}))
        add(family, "required-bool", mut(family, **{fields[0]: True}))
        add(family, "required-list", mut(family, **{fields[0]: ["x"]}))
        add(family, "required-mapping", mut(family, **{fields[0]: {"a": 1}}))

    # --- unknown fields
    for family in BASELINE:
        add(family, "unknown-field", mut(family, velocity=3))
    add("Task", "unknown-fields-multiple", mut("Task", alpha=1, beta=2))
    add("Task", "unknown-field-shadowing-another-family",
        mut("Task", conclusion_status="SUPPORTED"))

    # --- enums
    add("Task", "enum-status-invalid", mut("Task", status="DONE"))
    add("Task", "enum-status-integer", mut("Task", status=5))
    add("Task", "enum-status-null", mut("Task", status=None))
    add("Task", "enum-type-invalid", mut("Task", type="REFACTORING"))
    add("Task", "enum-priority-invalid", mut("Task", priority="P1"))
    add("Task", "enum-risk-invalid", mut("Task", risk="EXTREME"))
    add("Task", "enum-uncertainty-invalid",
        mut("Task", uncertainty_level="TOTAL"))
    add("Task", "enum-created-by-invalid", mut("Task", created_by="human:user"))
    add("Question", "enum-impact-invalid", mut("Question", impact="CRITICAL"))
    add("Question", "enum-status-invalid", mut("Question", status="CLOSED"))
    add("Decision", "enum-status-invalid", mut("Decision", status="RATIFIED"))
    add("Evidence", "enum-source-type-invalid",
        mut("Evidence", source_type="RUMOUR"))
    add("Evidence", "enum-status-invalid", mut("Evidence", status="STALE"))
    add("Evidence", "enum-reliability-invalid",
        mut("Evidence", reliability="PERFECT"))
    add("Evidence", "enum-reliability-valid",
        mut("Evidence", reliability="UNKNOWN"))
    add("Investigation", "enum-status-invalid",
        mut("Investigation", status="FINISHED"))

    # --- booleans
    for value in (True, False):
        add("Task", f"bool-approval-required-{value}",
            mut("Task", approval_required=value))
    for value in ("true", 1, 0, None, ["true"]):
        add("Task", f"bool-approval-required-invalid-{value!r}",
            mut("Task", approval_required=value))

    # --- optional strings
    add("Task", "optional-string-integer", mut("Task", plan=7))
    add("Task", "optional-string-null", mut("Task", plan=None))
    add("Task", "optional-string-list", mut("Task", plan=["a"]))
    add("Task", "optional-string-mapping", mut("Task", plan={"a": 1}))
    add("Task", "optional-string-empty-is-recorded", mut("Task", plan=""))
    add("Question", "optional-string-integer", mut("Question", updated_at=7))
    add("Evidence", "optional-string-integer", mut("Evidence", source=7))
    add("Handoff", "optional-string-integer",
        mut("Handoff", intended_recipient=7))

    # --- plain string lists
    add("Task", "string-list-valid", mut("Task", tests=["a", "b"]))
    add("Task", "string-list-empty", mut("Task", tests=[]))
    add("Task", "string-list-scalar", mut("Task", tests="a"))
    add("Task", "string-list-null", mut("Task", tests=None))
    add("Task", "string-list-bad-entry", mut("Task", tests=["a", 2]))
    add("Task", "string-list-mapping", mut("Task", context_items={"a": 1}))
    add("Task", "string-list-nested", mut("Task", evaluations=[["a"]]))

    # --- single references
    add("Task", "ref-single-valid", mut("Task", parent_task="TASK-2026-0002"))
    add("Task", "ref-single-malformed", mut("Task", parent_task="TASK-26-2"))
    add("Task", "ref-single-wrong-family",
        mut("Task", parent_task="DEC-2026-0001"))
    add("Task", "ref-single-list", mut("Task", parent_task=["TASK-2026-0002"]))
    add("Task", "ref-single-null", mut("Task", parent_task=None))
    add("Task", "ref-single-integer", mut("Task", report=7))
    add("Question", "ref-single-malformed", mut("Question", task="TASK-1"))
    add("Handoff", "ref-single-required-and-malformed",
        mut("Handoff", task="TASK-1"))
    add("Handoff", "ref-single-required-and-integer", mut("Handoff", task=7))
    add("Report", "ref-single-previous", mut("Report", previous_report="RPT-1"))

    # --- list references
    add("Task", "ref-list-valid", mut("Task", decisions=["DEC-2026-0001"]))
    add("Task", "ref-list-empty", mut("Task", decisions=[]))
    add("Task", "ref-list-scalar", mut("Task", decisions="DEC-2026-0001"))
    add("Task", "ref-list-null", mut("Task", decisions=None))
    add("Task", "ref-list-bad-entry", mut("Task", decisions=["nope"]))
    add("Task", "ref-list-wrong-family",
        mut("Task", decisions=["TASK-2026-0002"]))
    add("Task", "ref-list-multiple-bad-entries",
        mut("Task", decisions=["nope", "also-nope"]))
    add("Task", "ref-list-mixed-entries",
        mut("Task", decisions=["DEC-2026-0001", 7]))
    add("Task", "ref-list-mapping", mut("Task", questions={"a": 1}))
    add("Investigation", "ref-list-supporting-evidence-bad",
        mut("Investigation", supporting_evidence=["EVD-1"]))
    add("Evidence", "ref-list-supersedes-bad",
        mut("Evidence", supersedes=["DEC-2026-0001"]))

    # --- Task approval: the edge case and its neighbours
    add("Task", "approval-required-without-status",
        mut("Task", approval_required=True))
    add("Task", "approval-required-with-pending",
        mut("Task", approval_required=True, approval_status="PENDING"))
    add("Task", "approval-approved-complete", mut("Task", **APPROVED))
    add("Task", "approval-approved-missing-both",
        mut("Task", approval_status="APPROVED"))
    add("Task", "approval-approved-missing-at",
        mut("Task", approval_status="APPROVED", approved_by="human:user"))
    add("Task", "approval-approved-missing-by",
        mut("Task", approval_status="APPROVED",
            approved_at="2026-01-01T01:00:00Z"))
    add("Task", "approval-approved-empty-by",
        mut("Task", **{**APPROVED, "approved_by": "  "}))
    add("Task", "approval-approved-empty-at",
        mut("Task", **{**APPROVED, "approved_at": ""}))
    for status in ("NOT_REQUIRED", "PENDING", "REJECTED"):
        add("Task", f"approval-{status.lower()}-with-provenance",
            mut("Task", **{**APPROVED, "approval_status": status}))
        add("Task", f"approval-{status.lower()}-clean",
            mut("Task", approval_status=status))
    add("Task", "approval-provenance-without-status",
        mut("Task", approved_by="human:user",
            approved_at="2026-01-01T01:00:00Z"))
    add("Task", "approval-partial-provenance-without-status",
        mut("Task", approved_by="human:user"))
    add("Task", "approval-status-invalid-with-provenance",
        mut("Task", approval_status="MAYBE", approved_by="human:user"))

    # --- Task approved_scope
    add("Task", "scope-without-approval", mut("Task", approved_scope={}))
    add("Task", "scope-with-pending",
        mut("Task", approval_status="PENDING", approved_scope={}))
    add("Task", "scope-approved-empty", mut("Task", **APPROVED,
                                            approved_scope={}))
    add("Task", "scope-approved-full",
        mut("Task", **APPROVED,
            approved_scope={"approved_files": ["a.py"],
                            "approved_operations": ["edit"],
                            "approved_risk": "LOW", "approved_plan": "p"}))
    add("Task", "scope-not-an-object",
        mut("Task", **APPROVED, approved_scope="everything"))
    add("Task", "scope-list", mut("Task", **APPROVED, approved_scope=["a"]))
    add("Task", "scope-unknown-subfield",
        mut("Task", **APPROVED, approved_scope={"approved_everything": True}))
    add("Task", "scope-unknown-subfields-multiple",
        mut("Task", **APPROVED, approved_scope={"zeta": 1, "alpha": 2}))
    add("Task", "scope-files-scalar",
        mut("Task", **APPROVED, approved_scope={"approved_files": "a.py"}))
    add("Task", "scope-files-bad-entry",
        mut("Task", **APPROVED, approved_scope={"approved_files": ["a", 2]}))
    add("Task", "scope-operations-scalar",
        mut("Task", **APPROVED, approved_scope={"approved_operations": "e"}))
    add("Task", "scope-risk-invalid",
        mut("Task", **APPROVED, approved_scope={"approved_risk": "EXTREME"}))
    add("Task", "scope-risk-integer",
        mut("Task", **APPROVED, approved_scope={"approved_risk": 1}))
    add("Task", "scope-plan-list",
        mut("Task", **APPROVED, approved_scope={"approved_plan": ["p"]}))

    # --- Task archival
    add("Task", "archived-with-outcome",
        mut("Task", status="ARCHIVED", archived_outcome="COMPLETED"))
    add("Task", "archived-with-cancelled",
        mut("Task", status="ARCHIVED", archived_outcome="CANCELLED"))
    add("Task", "archived-missing-outcome", mut("Task", status="ARCHIVED"))
    add("Task", "archived-invalid-outcome",
        mut("Task", status="ARCHIVED", archived_outcome="ABANDONED"))
    add("Task", "archived-outcome-integer",
        mut("Task", status="ARCHIVED", archived_outcome=1))
    add("Task", "outcome-without-archived",
        mut("Task", status="IN_PROGRESS", archived_outcome="COMPLETED"))
    add("Task", "outcome-with-invalid-status",
        mut("Task", status="DONE", archived_outcome="COMPLETED"))

    # --- Question answer provenance
    add("Question", "answered-complete",
        mut("Question", status="ANSWERED", answer="Because.",
            answered_by="human:user", answered_at="2026-01-03T00:00:00Z"))
    add("Question", "answered-missing-all", mut("Question", status="ANSWERED"))
    add("Question", "answered-missing-answer",
        mut("Question", status="ANSWERED", answered_by="human:user",
            answered_at="2026-01-03T00:00:00Z"))
    add("Question", "answered-empty-answer",
        mut("Question", status="ANSWERED", answer="  ",
            answered_by="human:user", answered_at="2026-01-03T00:00:00Z"))
    add("Question", "open-with-answer",
        mut("Question", status="OPEN", answer="Because."))
    add("Question", "withdrawn-with-answered-at",
        mut("Question", status="WITHDRAWN",
            answered_at="2026-01-03T00:00:00Z"))
    add("Question", "invalid-status-with-answer",
        mut("Question", status="CLOSED", answer="Because."))

    # --- Decision provenance and supersession
    for status in ("PROPOSED", "UNDER_REVIEW", "REJECTED", "WITHDRAWN"):
        clean = without("Decision", "approved_by", "approved_at")
        add("Decision", f"decision-{status.lower()}-clean",
            {**clean, "status": status})
        add("Decision", f"decision-{status.lower()}-with-provenance",
            mut("Decision", status=status))
    add("Decision", "decision-approved-missing-provenance",
        without("Decision", "approved_by", "approved_at"))
    add("Decision", "decision-approved-partial-provenance",
        without("Decision", "approved_at"))
    add("Decision", "decision-approved-empty-by",
        mut("Decision", approved_by=""))
    add("Decision", "decision-superseded-complete",
        mut("Decision", status="SUPERSEDED",
            superseded_by=["DEC-2026-0002"]))
    add("Decision", "decision-superseded-missing-target",
        mut("Decision", status="SUPERSEDED"))
    add("Decision", "decision-superseded-empty-target",
        mut("Decision", status="SUPERSEDED", superseded_by=[]))
    add("Decision", "decision-superseded-scalar-target",
        mut("Decision", status="SUPERSEDED", superseded_by="DEC-2026-0002"))
    add("Decision", "decision-superseded-by-without-superseded",
        mut("Decision", superseded_by=["DEC-2026-0002"]))
    add("Decision", "decision-supersedes-is-allowed",
        mut("Decision", supersedes=["DEC-2026-0002"]))

    # --- Investigation conclusions
    add("Investigation", "concluded-supported",
        mut("Investigation", status="CONCLUDED",
            conclusion_status="SUPPORTED"))
    add("Investigation", "concluded-missing",
        mut("Investigation", status="CONCLUDED"))
    add("Investigation", "concluded-invalid",
        mut("Investigation", status="CONCLUDED", conclusion_status="PROVEN"))
    add("Investigation", "concluded-integer",
        mut("Investigation", status="CONCLUDED", conclusion_status=1))
    add("Investigation", "abandoned-correct",
        mut("Investigation", status="ABANDONED",
            conclusion_status="NO_VALID_CONCLUSION"))
    add("Investigation", "abandoned-missing",
        mut("Investigation", status="ABANDONED"))
    add("Investigation", "abandoned-wrong-value",
        mut("Investigation", status="ABANDONED",
            conclusion_status="SUPPORTED"))
    add("Investigation", "open-with-conclusion",
        mut("Investigation", status="OPEN",
            conclusion_status="INCONCLUSIVE"))
    add("Investigation", "open-with-invalid-conclusion",
        mut("Investigation", status="OPEN", conclusion_status="MAYBE"))
    add("Investigation", "paused-with-conclusion",
        mut("Investigation", status="PAUSED",
            conclusion_status="INCONCLUSIVE"))

    # --- immutable families carry no invented lifecycle
    for family in ("Handoff", "Report"):
        add(family, "no-status-field", mut(family, status="OPEN"))
        add(family, "no-updated-at", mut(family, updated_at="2026-01-08T00:00:00Z"))

    return cases


CORPUS = build_corpus()

MANIFEST_CORPUS = [
    ("valid", {"manifest_version": "1.0", "tabularium_ruleset": "0.1.0"}),
    ("valid-with-title", {"manifest_version": "1.0",
                          "tabularium_ruleset": "0.1.0", "title": "P"}),
    ("missing-ruleset", {"manifest_version": "1.0"}),
    ("missing-version", {"tabularium_ruleset": "0.1.0"}),
    ("missing-both", {}),
    ("unknown-key", {"manifest_version": "1.0", "tabularium_ruleset": "0.1.0",
                     "token_budget": "1500"}),
    ("unknown-keys-multiple", {"manifest_version": "1.0",
                               "tabularium_ruleset": "0.1.0", "owner": "k",
                               "project_id": "p"}),
    ("unsupported-version", {"manifest_version": "1.1",
                             "tabularium_ruleset": "0.1.0"}),
    ("empty-ruleset", {"manifest_version": "1.0", "tabularium_ruleset": "   "}),
    ("empty-title", {"manifest_version": "1.0", "tabularium_ruleset": "0.1.0",
                     "title": ""}),
]


# --- the comparison ---------------------------------------------------------

class TestGoldenConformance(unittest.TestCase):
    """Frozen validator and shadow must agree on the whole corpus."""

    def test_corpus_is_not_trivially_small(self):
        self.assertGreaterEqual(len(CORPUS), 150)
        families = {family for family, _, _ in CORPUS}
        self.assertEqual(families, set(SHADOW_VERSIONS))

    def test_accept_reject_and_reason_categories_agree(self):
        divergences = []
        for family, label, data in CORPUS:
            legacy_messages = frozen.validate_consumed(
                family, dict(data), VALID_BODY[family])
            legacy_codes = normalize(legacy_messages)
            shadow = validate_shadow(family, data)

            if bool(legacy_codes) != (not shadow.valid) or \
                    legacy_codes != shadow.reason_codes:
                divergences.append(
                    f"\n  family={family} case={label}"
                    f"\n    frozen accepted={not legacy_codes} "
                    f"codes={list(legacy_codes)}"
                    f"\n    shadow accepted={shadow.valid} "
                    f"codes={list(shadow.reason_codes)}"
                    f"\n    frozen prose={list(legacy_messages)}")
        self.assertFalse(
            divergences,
            "frozen validator and shadow contracts diverged on "
            f"{len(divergences)} of {len(CORPUS)} cases:"
            + "".join(divergences))

    def test_shadow_never_accepts_what_the_frozen_validator_rejects(self):
        # Stated separately from the code comparison because it is the
        # invariant that actually matters: the shadow must not be more
        # permissive than production behavior.
        for family, label, data in CORPUS:
            with self.subTest(family=family, case=label):
                rejected = bool(frozen.validate_consumed(
                    family, dict(data), VALID_BODY[family]))
                if rejected:
                    self.assertFalse(validate_shadow(family, data).valid)

    def test_shadow_never_rejects_what_the_frozen_validator_accepts(self):
        for family, label, data in CORPUS:
            with self.subTest(family=family, case=label):
                accepted = not frozen.validate_consumed(
                    family, dict(data), VALID_BODY[family])
                if accepted:
                    self.assertTrue(validate_shadow(family, data).valid)

    def test_manifest_post_parse_agreement(self):
        divergences = []
        for label, data in MANIFEST_CORPUS:
            # The frozen parser consumes text, so the case is rendered as the
            # explicitly quoted YAML that Manifest v1.0 requires. Every
            # remaining difference is then a post-parse difference, which is
            # the only half the shadow claims.
            text = "".join(f'{k}: "{v}"\n' for k, v in data.items())
            parsed, reason = frozen_manifest.validate(text)
            shadow = validate_manifest_shadow(data)
            if (parsed is not None) != shadow.valid:
                divergences.append(
                    f"\n  case={label}\n    frozen accepted={parsed is not None}"
                    f" reason={reason!r}"
                    f"\n    shadow accepted={shadow.valid} "
                    f"codes={list(shadow.reason_codes)}")
        self.assertFalse(divergences,
                         "manifest post-parse divergence:" + "".join(divergences))


class TestNormalizationMap(unittest.TestCase):
    """The map must be complete and must not quietly swallow messages."""

    def test_every_frozen_message_in_the_corpus_is_covered(self):
        for family, label, data in CORPUS:
            with self.subTest(family=family, case=label):
                normalize(frozen.validate_consumed(
                    family, dict(data), VALID_BODY[family]))

    def test_unmapped_message_fails_loudly(self):
        with self.assertRaises(AssertionError):
            normalize(["something the frozen validator never said"])

    def test_body_messages_normalize_to_a_body_code(self):
        self.assertEqual(normalize(["body requires exact section '## Outcome'"]),
                         (BODY_SECTION_MISSING,))


class TestBoundary(unittest.TestCase):
    """What the shadow deliberately does not model."""

    def test_body_sections_are_frozen_validator_territory_only(self):
        # A Handoff with a conforming frontmatter and an empty body: the
        # frozen validator rejects it on body sections, the shadow accepts it
        # because it never reads a body. This asymmetry is intended and is
        # the reason the corpus supplies conforming bodies everywhere else.
        data = BASELINE["Handoff"]
        self.assertEqual(
            normalize(frozen.validate_consumed("Handoff", dict(data), "")),
            (BODY_SECTION_MISSING,))
        self.assertTrue(validate_shadow("Handoff", data).valid)

    def test_manifest_node_rules_are_frozen_parser_territory_only(self):
        # An unquoted scalar is invalid under Manifest v1.0. The frozen parser
        # sees the style and rejects it; post-parse it is simply a float, and
        # the shadow can only observe the resulting type. The shadow is not
        # a manifest validator and must never be used as one.
        parsed, reason = frozen_manifest.validate(
            'manifest_version: "1.0"\ntabularium_ruleset: 0.1.0\n')
        self.assertIsNone(parsed)
        self.assertIn("quoted", reason)

    def test_duplicate_keys_are_invisible_post_parse(self):
        parsed, reason = frozen_manifest.validate(
            'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\n'
            'tabularium_ruleset: "0.2.0"\n')
        self.assertIsNone(parsed)
        self.assertIn("duplicate", reason)
        # The same document as a mapping has already lost the duplicate.
        self.assertTrue(validate_manifest_shadow(
            {"manifest_version": "1.0", "tabularium_ruleset": "0.2.0"}).valid)


class TestNonStringKeyEdgeCase(unittest.TestCase):
    """A YAML mapping key that is not a string.

    Recorded as a finding, not repaired. `1: x` is valid YAML and yields an
    int key. The frozen contracts define no such field and treat an
    unrecognized field as a contract violation, so the record should be
    rejected — but the frozen validator raises TypeError while building its
    unrecognized-field message instead of returning an error, so a malformed
    record raises rather than producing a diagnostic.

    The shadow rejects it, which is what the frozen CONTRACT requires. The
    frozen VALIDATOR is left exactly as it is: changing production behavior to
    make a comparison green is not this unit's business. The case is excluded
    from the corpus because a raised exception cannot be compared with a
    result.
    """

    DATA = {**BASELINE["Task"], 1: "x"}

    def test_frozen_validator_raises(self):
        with self.assertRaises(TypeError):
            frozen.validate_consumed("Task", dict(self.DATA), VALID_BODY["Task"])

    def test_shadow_rejects_as_an_unrecognized_field(self):
        res = validate_shadow("Task", self.DATA)
        self.assertFalse(res.valid)
        self.assertIn("UNRECOGNIZED_FIELD:1", res.reason_codes)


class TestVersionDriftDetection(unittest.TestCase):
    """Fail loudly if the consumed contract versions move without review."""

    def test_shadow_versions_match_the_frozen_consumed_versions(self):
        self.assertEqual(
            SHADOW_VERSIONS, frozen.SUPPORTED_VERSION,
            "the frozen validator's supported contract versions changed; "
            "the shadow models must be reviewed against the new contract "
            "versions before this binding is updated")

    def test_manifest_version_matches(self):
        self.assertEqual(SHADOW_MANIFEST_VERSION,
                         frozen_manifest.SUPPORTED_MANIFEST_VERSION)

    def test_independent_tables_agree(self):
        # The shadow declares its own tables so that the comparison above is
        # meaningful. That independence is only safe if drift between the two
        # declarations is itself detected.
        from tabularium.engine.shadow_contracts import common as shadow_common
        from tabularium.engine.shadow_contracts.models import FIELD_CLASS

        for family, allowed in frozen.ALLOWED_FIELDS.items():
            with self.subTest(family=family):
                self.assertEqual(set(FIELD_CLASS[family]), allowed)
        for family, required in frozen.REQUIRED_STRINGS.items():
            with self.subTest(family=family):
                self.assertEqual(set(shadow_common.REQUIRED_STRINGS[family]),
                                 set(required))
        for family, refs in frozen.SINGLE_REF.items():
            with self.subTest(family=family):
                self.assertEqual(shadow_common.SINGLE_REF[family], refs)
        for family, refs in frozen.LIST_REF.items():
            with self.subTest(family=family):
                self.assertEqual(shadow_common.LIST_REF[family], refs)
        self.assertEqual(set(shadow_common.TASK_STATUS),
                         set(frozen.TASK_STATUS))
        self.assertEqual(set(shadow_common.DECISION_STATUS),
                         set(frozen.DECISION_STATUS))
        self.assertEqual(shadow_common.DECISION_PROVENANCE,
                         frozen.DECISION_PROVENANCE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
