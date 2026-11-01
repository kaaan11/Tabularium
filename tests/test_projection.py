"""Tests for deterministic token-budgeted projection (M3.3).

Fixtures A-F from the M3.3 scope:

    A  SMALL / HOT          fits comfortably
    B  LARGE / HOT          lower-priority material is dropped
    C  LARGE / WARM         the same package carries more at WARM
    D  REQUIRED OVERFLOW    non-droppable material alone exceeds the budget
    E  BLOCKED package      no executable payload
    F  DIAGNOSTIC_ONLY      no executable payload

Every package under test comes from the real M2 loader. Projection is never
given a hand-written package shape, because the point is that it consumes
what M2 actually emits.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import copy
import sys
import tokenize
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tabularium.engine.projection import (BUDGET_OVERFLOW,  # noqa: E402
                                MISSING_REQUIRED_EXECUTION_CONTEXT,
                                PACKAGE_NOT_READY, PROJECTED, REFUSED,
                                UNSUPPORTED_PROJECTION_PROFILE, project)
from tabularium.engine.projection.budget import (DEFAULT_ESTIMATOR,  # noqa: E402
                                       CharRatioEstimator, DEFAULT_LIMITS,
                                       HOT, WARM, compact, resolve_profile)
from tabularium.engine.projection.projector import MIN_TRUNCATION_CHARS  # noqa: E402
from tests.test_context_loader import Fixture  # noqa: E402

INTENT = "continue the project"
PROSE = ("Narrative paragraph that exists to occupy budget without changing "
         "meaning. ")


def long_text(chars: int) -> str:
    return (PROSE * (chars // len(PROSE) + 1))[:chars]


# Size pins over a whole package were taken with the fixture under
# /tmp/ctxload-XXXXXXXX. The package carries that root twice (project_root
# and compatibility.ai_os_root), so a longer or shorter TMPDIR shifts the
# character count by twice the difference.
PINNED_FIXTURE_ROOT_CHARS = len("/tmp/ctxload-XXXXXXXX")


def pinned_size(fx: Fixture, chars_at_pin: int) -> dict[str, int]:
    chars = chars_at_pin + 2 * (len(str(fx.tmp)) - PINNED_FIXTURE_ROOT_CHARS)
    return {"chars": chars, "estimated_tokens": DEFAULT_ESTIMATOR.tokens(chars)}


def small_project() -> Fixture:
    """Fixture A: a modest package that fits comfortably at HOT."""
    fx = Fixture()
    fx.task("TASK-2026-0001", decisions=["DEC-2026-0001"],
            questions=["QST-2026-0001"], plan="Do the thing carefully.")
    fx.decision("DEC-2026-0001", task="TASK-2026-0001")
    fx.question("QST-2026-0001", task="TASK-2026-0001", impact="BLOCKING")
    fx.handoff("HND-2026-0001")
    return fx


def large_project() -> Fixture:
    """Fixtures B and C: far more eligible material than HOT can carry."""
    fx = Fixture()
    fx.doc("constraints.md", "# Constraints\n\n" + long_text(3000))
    fx.doc("project.md", "# Project\n\n" + long_text(2000))
    fx.doc("current-state.md", "# Current State\n\n" + long_text(2000))
    fx.rec("TASK-2026-0001",
           {"schema_version": "1.10", "title": "Root", "objective": "Do it",
            "status": "IN_PROGRESS", "created_at": "2026-01-01T00:00:00Z",
            "created_by": "USER", "plan": long_text(1500),
            "decisions": ["DEC-2026-0001", "DEC-2026-0002"],
            "questions": ["QST-2026-0001", "QST-2026-0002"]},
           "# Root Task\n\n" + long_text(3000))
    for n in (1, 2):
        fx.decision(f"DEC-2026-000{n}", task="TASK-2026-0001",
                    body="# Decision\n\n" + long_text(2000))
        fx.investigation(f"INV-2026-000{n}", task="TASK-2026-0001",
                         body="# Investigation\n\n" + long_text(2000)
                         + "\n\n## Current conclusion\n\nNone yet.\n")
        fx.evidence(f"EVD-2026-000{n}", task="TASK-2026-0001")
    fx.question("QST-2026-0001", task="TASK-2026-0001", impact="BLOCKING")
    fx.question("QST-2026-0002", task="TASK-2026-0001", impact="NON_BLOCKING")
    fx.handoff("HND-2026-0001",
               body="# Handoff\n\n## Current State\n\n" + long_text(1500)
                    + "\n\n## Continuation\n\nContinue safely.\n")
    fx.report("RPT-2026-0001",
              body="# Report\n\n## Outcome\n\n" + long_text(1500)
                   + "\n\n## Validation\n\nChecked.\n\n"
                     "## Limitations\n\nNone known.\n")
    return fx


class ProjectionTestCase(unittest.TestCase):
    def load(self, fx: Fixture, task="TASK-2026-0001"):
        self.addCleanup(fx.cleanup)
        return fx.load(task=task)


# --- A: small / HOT ---------------------------------------------------------

class TestSmallHot(ProjectionTestCase):

    def setUp(self):
        self.pkg = self.load(small_project())
        self.result = project(self.pkg, intent=INTENT, profile=HOT)

    def test_projects_within_budget(self):
        self.assertEqual(self.result.outcome, PROJECTED)
        self.assertTrue(self.result.within_budget)
        self.assertEqual(self.result.reason_codes, ())
        self.assertLessEqual(self.result.estimated_payload_tokens,
                             DEFAULT_LIMITS[HOT])

    def test_required_section_is_always_first_and_present(self):
        names = self.result.payload.section_names()
        self.assertEqual(names[0], "required")
        required = self.result.payload.get("required")
        self.assertEqual(required["intent"], INTENT)
        self.assertEqual(required["task"]["id"], "TASK-2026-0001")
        self.assertEqual(required["governance"]["package_status"], "READY")
        self.assertIn("task_actionability", required["governance"])

    def test_nothing_is_truncated_when_everything_fits(self):
        self.assertEqual(self.result.truncations, ())

    def test_payload_carries_no_absolute_paths_or_project_root(self):
        # `entry` holds absolute machine paths. They are not execution input
        # and must never reach a model payload.
        text = self.result.payload.to_json()
        self.assertNotIn(str(self.pkg.project_root), text)
        self.assertNotIn("ai_os_root", text)
        self.assertNotIn("project_root", text)


# --- B and C: large / HOT vs WARM -------------------------------------------

class TestLargeBudgets(ProjectionTestCase):

    def setUp(self):
        self.pkg = self.load(large_project())
        self.hot = project(self.pkg, intent=INTENT, profile=HOT)
        self.warm = project(self.pkg, intent=INTENT, profile=WARM)

    def test_both_profiles_project(self):
        self.assertEqual(self.hot.outcome, PROJECTED)
        self.assertEqual(self.warm.outcome, PROJECTED)
        self.assertTrue(self.hot.within_budget)
        self.assertTrue(self.warm.within_budget)

    def test_hot_respects_its_budget(self):
        self.assertLessEqual(self.hot.estimated_payload_tokens,
                             DEFAULT_LIMITS[HOT])

    def test_warm_respects_its_budget(self):
        self.assertLessEqual(self.warm.estimated_payload_tokens,
                             DEFAULT_LIMITS[WARM])

    def test_hot_sends_materially_less_than_governed_context(self):
        governed = self.hot.economy.governed_context["estimated_tokens"]
        payload = self.hot.economy.payload_size["estimated_tokens"]
        self.assertLess(payload, governed / 2,
                        "HOT should send materially less than the governed "
                        f"package: payload={payload} governed={governed}")
        self.assertGreater(self.hot.economy.reduction_pct, 50.0)

    def test_warm_carries_at_least_as_much_as_hot(self):
        self.assertGreaterEqual(
            self.warm.economy.payload_size["estimated_tokens"],
            self.hot.economy.payload_size["estimated_tokens"])
        self.assertGreaterEqual(
            len(self.warm.payload.section_names()),
            len(self.hot.payload.section_names()))

    def test_hot_drops_lower_priority_sections(self):
        dropped = {o.section for o in self.hot.omissions
                   if o.reason == "BUDGET"}
        self.assertTrue(dropped, "HOT should have dropped something")
        # Priority order decides what goes first: the derived, explicitly
        # non-authoritative current-state index outranks nothing.
        self.assertIn("current_state", dropped)
        self.assertIn("required", self.hot.payload.section_names())

    def test_dropping_follows_the_documented_priority_order(self):
        kept = self.hot.payload.section_names()
        dropped = {o.section for o in self.hot.omissions
                   if o.reason == "BUDGET"}
        self.assertNotIn("constraints", dropped)
        for section in kept:
            self.assertNotIn(section, dropped)

    def test_truncation_is_marked_explicitly(self):
        marked = [t for t in self.hot.truncations]
        self.assertTrue(marked, "the large fixture should force truncation")
        for truncation in marked:
            with self.subTest(section=truncation.section):
                self.assertLess(truncation.kept_chars,
                                truncation.original_chars)
                self.assertGreaterEqual(truncation.kept_chars,
                                        MIN_TRUNCATION_CHARS)
        text = self.hot.payload.to_json()
        self.assertIn("[TRUNCATED:", text)

    def test_truncation_never_touches_identity_or_state(self):
        for name, value in self.hot.payload.sections:
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for field in ("id", "load_mode", "status", "impact",
                              "approval_status", "category", "code"):
                    if field in item and isinstance(item[field], str):
                        with self.subTest(section=name, field=field):
                            self.assertNotIn("[TRUNCATED:", item[field])

    def test_load_mode_is_never_upgraded(self):
        by_id = {s["id"]: s for s in self.pkg.to_dict()["selected"]}
        for _, value in self.hot.payload.sections:
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, dict) and item.get("id") in by_id:
                    with self.subTest(item=item["id"]):
                        self.assertEqual(item["load_mode"],
                                         by_id[item["id"]]["load_mode"])

    def test_metadata_only_records_never_gain_a_body(self):
        for _, value in self.hot.payload.sections:
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, dict) and \
                        item.get("load_mode") == "METADATA_ONLY":
                    with self.subTest(item=item.get("id")):
                        self.assertNotIn("body", item)

    def test_every_payload_character_came_from_the_package(self):
        # Extractive, not generative: each included body is a prefix of the
        # body M2 loaded, plus an explicit truncation marker.
        by_id = {s["id"]: s for s in self.pkg.to_dict()["selected"]}
        for _, value in self.hot.payload.sections:
            items = value if isinstance(value, list) else [value]
            for item in items:
                if not isinstance(item, dict) or "body" not in item \
                        or "id" not in item:
                    continue
                source = by_id.get(item["id"], {}).get("body") or ""
                body = item["body"].split("\n...[TRUNCATED:")[0]
                with self.subTest(item=item["id"]):
                    self.assertTrue(source.startswith(body))


# --- D: required overflow ---------------------------------------------------

class TestRequiredOverflow(ProjectionTestCase):

    def test_overflow_is_explicit_and_produces_no_payload(self):
        pkg = self.load(small_project())
        # A budget too small to hold the non-droppable material. Never
        # silently truncate governance-critical context, and never claim a fit
        # the estimator does not support.
        result = project(pkg, intent=INTENT, profile=HOT, limit_tokens=10)
        self.assertEqual(result.outcome, REFUSED)
        self.assertEqual(result.reason_codes, (BUDGET_OVERFLOW,))
        self.assertIsNone(result.payload)
        self.assertFalse(result.within_budget)

    def test_overflow_still_reports_metrics(self):
        pkg = self.load(small_project())
        result = project(pkg, intent=INTENT, profile=HOT, limit_tokens=10)
        self.assertGreater(result.economy.governed_context["chars"], 0)
        self.assertGreater(result.estimated_payload_tokens,
                           result.budget_limit_tokens)

    def test_no_summary_is_invented_to_make_it_fit(self):
        pkg = self.load(large_project())
        result = project(pkg, intent=INTENT, profile=HOT, limit_tokens=5)
        self.assertEqual(result.outcome, REFUSED)
        self.assertIsNone(result.payload)


# --- E and F: not worker-ready ----------------------------------------------

class TestNotReady(ProjectionTestCase):

    def test_blocked_package_yields_no_executable_payload(self):
        fx = small_project()
        fx.remove("project.md")          # required foundation -> BLOCKED
        pkg = self.load(fx)
        self.assertEqual(pkg.package_status, "BLOCKED")
        result = project(pkg, intent=INTENT, profile=HOT)
        self.assertEqual(result.outcome, REFUSED)
        self.assertEqual(result.reason_codes, (PACKAGE_NOT_READY,))
        self.assertIsNone(result.payload)

    def test_diagnostic_only_package_yields_no_executable_payload(self):
        fx = small_project()
        fx.manifest({"manifest_version": "1.0", "tabularium_ruleset": "9.9.9"})
        pkg = self.load(fx)
        self.assertEqual(pkg.package_status, "DIAGNOSTIC_ONLY")
        result = project(pkg, intent=INTENT, profile=HOT)
        self.assertEqual(result.outcome, REFUSED)
        self.assertEqual(result.reason_codes, (PACKAGE_NOT_READY,))
        self.assertIsNone(result.payload)

    def test_project_orientation_package_has_no_execution_target(self):
        fx = small_project()
        pkg = fx.load()                  # no root Task
        self.addCleanup(fx.cleanup)
        result = project(pkg, intent=INTENT, profile=HOT)
        self.assertEqual(result.reason_codes,
                         (MISSING_REQUIRED_EXECUTION_CONTEXT,))
        self.assertIsNone(result.payload)

    def test_unsupported_profile_is_refused_not_downgraded(self):
        pkg = self.load(small_project())
        for profile in ("COLD", "TEPID", ""):
            with self.subTest(profile=profile):
                result = project(pkg, intent=INTENT, profile=profile)
                self.assertEqual(result.reason_codes,
                                 (UNSUPPORTED_PROJECTION_PROFILE,))
                self.assertIsNone(result.payload)

    def test_refusals_are_results_not_exceptions(self):
        pkg = self.load(small_project())
        for kwargs in ({"profile": "COLD"}, {"profile": HOT,
                                             "limit_tokens": 1}):
            with self.subTest(kwargs=kwargs):
                result = project(pkg, intent=INTENT, **kwargs)
                self.assertEqual(result.outcome, REFUSED)


# --- determinism ------------------------------------------------------------

class TestDeterminism(ProjectionTestCase):

    def setUp(self):
        self.pkg = self.load(large_project())

    def test_repeated_calls_are_byte_identical(self):
        first = project(self.pkg, intent=INTENT, profile=HOT)
        second = project(self.pkg, intent=INTENT, profile=HOT)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.payload.to_json(), second.payload.to_json())

    def test_reordered_input_mappings_produce_the_same_payload(self):
        data = self.pkg.to_dict()
        baseline = project(data, intent=INTENT, profile=HOT)
        shuffled = {k: data[k] for k in reversed(list(data))}
        shuffled["selected"] = list(reversed(data["selected"]))
        shuffled["diagnostics"] = list(data["diagnostics"])
        reordered = project(shuffled, intent=INTENT, profile=HOT)
        self.assertEqual(baseline.payload.to_json(),
                         reordered.payload.to_json())
        self.assertEqual(baseline.economy.to_dict(),
                         reordered.economy.to_dict())

    def test_no_generated_identifiers_or_timestamps(self):
        text = project(self.pkg, intent=INTENT, profile=HOT).to_json()
        for token in ("uuid", "generated_at", "projected_at", "timestamp"):
            self.assertNotIn(token, text.lower())

    def test_estimator_is_documented_and_replaceable(self):
        result = project(self.pkg, intent=INTENT, profile=HOT)
        self.assertEqual(result.estimator, "char-ratio-v1")
        custom = CharRatioEstimator(chars_per_token=2, name="char-ratio-half")
        swapped = project(self.pkg, intent=INTENT, profile=HOT,
                          estimator=custom)
        self.assertEqual(swapped.estimator, "char-ratio-half")

    def test_payload_sections_constant_matches_the_real_sections(self):
        # PAYLOAD_SECTIONS is the published context-class taxonomy that M3.4
        # skill governance intersects against. If it ever drifts from what the
        # projector actually emits, skill context checks would be validating
        # against a section list that does not exist.
        from tabularium.engine.projection.projector import PAYLOAD_SECTIONS, _candidates
        data = self.pkg.to_dict()
        actual = ("required",) + tuple(name for name, _, _ in _candidates(data))
        self.assertEqual(actual, PAYLOAD_SECTIONS)

    def test_estimator_is_deterministic(self):
        estimator = CharRatioEstimator()
        self.assertEqual(estimator.tokens(0), 0)
        self.assertEqual(estimator.tokens(1), 1)
        self.assertEqual(estimator.tokens(4), 1)
        self.assertEqual(estimator.tokens(5), 2)


# --- immutability and isolation ---------------------------------------------

class TestImmutability(ProjectionTestCase):

    def test_projection_does_not_mutate_the_package(self):
        pkg = self.load(large_project())
        data = pkg.to_dict()
        before = copy.deepcopy(data)
        project(data, intent=INTENT, profile=HOT)
        project(data, intent=INTENT, profile=WARM)
        self.assertEqual(data, before)

    def test_projection_does_not_mutate_the_package_object(self):
        pkg = self.load(large_project())
        before = compact(pkg.to_dict())
        project(pkg, intent=INTENT, profile=HOT)
        self.assertEqual(compact(pkg.to_dict()), before)

    def test_worker_request_creation_does_not_mutate_the_payload(self):
        from tabularium.engine.workers import MockWorker, WorkerRequest
        pkg = self.load(small_project())
        result = project(pkg, intent=INTENT, profile=HOT)
        before = result.payload.to_json()
        request = WorkerRequest(request_id="req-1", intent=INTENT,
                                payload=result.payload,
                                task_id="TASK-2026-0001")
        MockWorker().execute(request)
        self.assertEqual(result.payload.to_json(), before)

    def test_projection_writes_nothing_to_the_project(self):
        fx = large_project()
        pkg = self.load(fx)
        snapshot = fx.snapshot()
        project(pkg, intent=INTENT, profile=HOT)
        project(pkg, intent=INTENT, profile=WARM)
        self.assertEqual(fx.snapshot(), snapshot)

    def test_projection_package_performs_no_io_and_never_imports_the_loader(self):
        forbidden = ("os", "pathlib", "socket", "urllib", "requests", "httpx",
                     "subprocess", "sqlite3", "shutil")
        root = Path(__file__).resolve().parents[1] / "tabularium" / "engine" / "projection"
        for path in sorted(root.glob("*.py")):
            with tokenize.open(str(path)) as handle:
                tokens = list(tokenize.generate_tokens(handle.readline))
            code = " ".join(tok.string for tok in tokens
                            if tok.type not in (tokenize.COMMENT,
                                                tokenize.STRING))
            with self.subTest(module=path.name):
                self.assertNotIn("context_loader", code)
                self.assertNotIn("open", code.split())
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    with self.subTest(module=path.name, line=stripped):
                        self.assertNotIn(stripped.split()[1].split(".")[0],
                                         forbidden)


# --- context economy --------------------------------------------------------

class TestContextEconomy(ProjectionTestCase):

    def setUp(self):
        self.pkg = self.load(large_project())
        self.result = project(self.pkg, intent=INTENT, profile=HOT)

    def test_four_metrics_exist_in_both_units(self):
        economy = self.result.economy.to_dict()
        for metric in ("available_context", "governed_context",
                       "payload_size", "context_avoided"):
            with self.subTest(metric=metric):
                self.assertIn("chars", economy[metric])
                self.assertIn("estimated_tokens", economy[metric])

    def test_context_avoided_is_governed_minus_payload(self):
        economy = self.result.economy
        self.assertEqual(
            economy.context_avoided["chars"],
            economy.governed_context["chars"] - economy.payload_size["chars"])

    def test_governed_is_the_eligible_subset_of_available(self):
        economy = self.result.economy
        self.assertLess(economy.governed_context["chars"],
                        economy.available_context["chars"])

    def test_cold_material_is_reported_but_never_injected(self):
        self.assertTrue(self.result.cold_context_available)
        text = self.result.payload.to_json()
        for cold in self.result.cold_context_available:
            with self.subTest(cold=cold):
                self.assertNotIn(f'"{cold}"', text)

    def test_reduction_percent_is_defined_or_absent(self):
        self.assertIsNotNone(self.result.economy.reduction_pct)
        empty = resolve_profile(HOT)
        self.assertIsNotNone(empty)
        from tabularium.engine.projection.model import ContextEconomy
        zero = ContextEconomy(available_context={"chars": 0, "estimated_tokens": 0},
                              governed_context={"chars": 0, "estimated_tokens": 0},
                              payload_size={"chars": 0, "estimated_tokens": 0},
                              context_avoided={"chars": 0, "estimated_tokens": 0})
        # No denominator, so no manufactured percentage.
        self.assertIsNone(zero.reduction_pct)

    def test_savings_report_is_readable(self):
        report = self.result.report()
        self.assertIn("governed context", report)
        self.assertIn("projected payload", report)
        self.assertIn("context avoided", report)


class TestTokenEconomyDemonstration(ProjectionTestCase):
    """Proof that the architecture can measure the product goal."""

    def test_demonstration(self):
        pkg = self.load(large_project())
        hot = project(pkg, intent=INTENT, profile=HOT)
        warm = project(pkg, intent=INTENT, profile=WARM)
        governed = hot.economy.governed_context["estimated_tokens"]

        self.assertGreater(governed, DEFAULT_LIMITS[HOT])
        self.assertLessEqual(hot.economy.payload_size["estimated_tokens"],
                             DEFAULT_LIMITS[HOT])
        self.assertGreater(hot.economy.context_avoided["estimated_tokens"], 0)
        self.assertGreaterEqual(
            warm.economy.payload_size["estimated_tokens"],
            hot.economy.payload_size["estimated_tokens"])

        if "-v" in sys.argv or "--verbose" in sys.argv:  # pragma: no cover
            print("\n" + hot.report() + "\n" + warm.report())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
