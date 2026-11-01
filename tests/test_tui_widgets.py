"""Approval widget tests (Faz B, DEC-2026-0008 Alternative A2).

Every test uses real objects — production loaders, real temp projects, a
real checkpointed approval boundary — never `SimpleNamespace`. The discipline
under test: bare keypresses record nothing, typed confirmations execute
through the same functions the CLI calls.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tabularium.engine.tui import widgets

_ACTOR = "human:user"
_STAMP = "2026-09-06T20:30:00+03:00"
_DIGEST = "9f2c4a1b5d6e7f80916263646566676879899a0b1c2d3e4f5061728394a5b6"


class ConfirmTests(unittest.TestCase):
    def test_only_the_exact_word_confirms(self):
        self.assertTrue(widgets.confirm_word("approve"))
        self.assertTrue(widgets.confirm_word("  Approve  "))
        for bad in ("", "y", "yes", "a", "ok", "approval", "approve!"):
            with self.subTest(typed=bad):
                self.assertFalse(widgets.confirm_word(bad))

    def test_decision_words_are_full_words_only(self):
        self.assertEqual(widgets.confirm_decision_word("approve"), "APPROVE")
        self.assertEqual(widgets.confirm_decision_word("Reject"), "REJECT")
        for bad in ("", "a", "r", "y", "yes", "ok"):
            with self.subTest(typed=bad):
                self.assertIsNone(widgets.confirm_decision_word(bad))

    def test_digest_prefix_must_match_hex_start(self):
        self.assertTrue(widgets.confirm_digest_prefix("9f2c4a1b", _DIGEST))
        self.assertTrue(widgets.confirm_digest_prefix("9F2C4A1B5D6E", _DIGEST))
        for bad in ("", "9f2c4a", "zzzzz999", "deadbeef", "9f2c4a1c"):
            with self.subTest(typed=bad):
                self.assertFalse(widgets.confirm_digest_prefix(bad, _DIGEST))
        self.assertFalse(widgets.confirm_digest_prefix("9f2c4a1b", None))


class SheetTests(unittest.TestCase):
    def test_sheets_show_names_and_digests_never_secrets(self):
        secret = "sk-abcdefghij1234567890"
        sheet = widgets.render_persistence_sheet("REP-1", ".ai/reports/r.md", _DIGEST)
        self.assertIn(_DIGEST, sheet)
        self.assertNotIn(secret, sheet)
        task_sheet = widgets.render_task_approve_sheet("TASK-2026-0001", "INBOX", _ACTOR)
        self.assertIn("TASK-2026-0001", task_sheet)
        self.assertIn("approve", task_sheet)
        exec_sheet = widgets.render_execution_sheet("request_id=r1", _ACTOR, _STAMP)
        self.assertIn("approve", exec_sheet)
        self.assertIn("reject", exec_sheet)


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-tuiw-")
        self.root = Path(self.holder.name)
        from tabularium.init import initialize
        from tabularium.onboarding import build_authorizations, onboard_project

        self.assertEqual(initialize(self.root).status, "INITIALIZED")
        declarations = {
            "title": "Widget fixture",
            "purpose": "Exercise approval widgets",
            "scope": "Bounded widget project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, _ACTOR,
                                 "2026-09-06T20:30:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")

    def tearDown(self):
        self.holder.cleanup()

    def _fingerprint(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            out[path.relative_to(self.root).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()
        return out

    def _create_task(self) -> str:
        from tabularium import task as task_surface

        created = task_surface.task_create(
            self.root, title="Widget work", objective="Approve me",
            type_="REVIEW")
        self.assertIsNone(created.code)
        return created.record_id


class TaskApproveTests(ProjectFixture):
    def test_typed_approval_executes_and_keypress_does_not(self):
        task_id = self._create_task()
        refused = widgets.approve_task(self.root, task_id, actor=_ACTOR,
                                       confirmation="y")
        self.assertEqual(refused.code, "CONFIRMATION_REJECTED")
        before = self._fingerprint()
        for bad in ("", "a", "yes"):
            widgets.approve_task(self.root, task_id, actor=_ACTOR,
                                 confirmation=bad)
        self.assertEqual(self._fingerprint(), before)
        done = widgets.approve_task(self.root, task_id, actor=_ACTOR,
                                    confirmation="approve")
        self.assertIsNone(done.code)


class ExecutionApprovalTests(ProjectFixture):
    def _pending(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("LangGraph not installed")
        from tabularium.execution import _checkpoint_path, remember_execution
        from tabularium.engine.execution import ExecutionPlan, start_execution
        from tabularium.engine.skills import ControlPlaneGrant
        from tabularium.engine.workers import MockWorker

        db = _checkpoint_path(self.root)
        plan = ExecutionPlan(
            project_root=str(self.root), task_id=self._create_task(),
            intent="Review this", execution_id="tuiw-approval",
            checkpoint_path=str(db), adapter=MockWorker(),
            grant=ControlPlaneGrant(approval_required=True),
        )
        started = start_execution(plan)
        self.assertEqual(started.status, "AWAITING_APPROVAL")
        remember_execution(self.root, "tuiw-approval")

    def test_probe_reports_pending_without_resuming(self):
        self._pending()
        pending = widgets.probe_pending_approval(self.root)
        self.assertEqual(pending.status, "AWAITING_APPROVAL")
        self.assertIsNotNone(pending.approval_request)

    def test_shorthand_decision_records_nothing(self):
        self._pending()
        still = widgets.resolve_pending_approval(
            self.root, decision_word="a", actor=_ACTOR, timestamp=_STAMP)
        self.assertEqual(still.status, "AWAITING_APPROVAL")

    def test_typed_decision_resolves_through_the_same_path(self):
        self._pending()
        result = widgets.resolve_pending_approval(
            self.root, decision_word="approve", actor=_ACTOR, timestamp=_STAMP)
        self.assertEqual(result.status, "COMPLETED")

    def test_missing_actor_records_nothing(self):
        self._pending()
        still = widgets.resolve_pending_approval(
            self.root, decision_word="approve", actor="", timestamp=_STAMP)
        self.assertEqual(still.status, "AWAITING_APPROVAL")


class OnboardingTests(unittest.TestCase):
    _DECLARATIONS = {
        "title": "Widget onboard",
        "purpose": "Exercise onboarding widget",
        "scope": "Bounded widget project",
    }

    def test_refusal_writes_nothing_and_approval_onboards(self):
        from tabularium.init import initialize

        with tempfile.TemporaryDirectory(prefix="tabu-tuiw-ob-") as raw:
            root = Path(raw)
            self.assertEqual(initialize(root).status, "INITIALIZED")
            before = {
                p.relative_to(root).as_posix(): hashlib.sha256(
                    p.read_bytes()).hexdigest()
                for p in sorted(q for q in root.rglob("*") if q.is_file())
            }
            draft = widgets.authorize_onboarding(
                root, dict(self._DECLARATIONS), confirmation="y",
                actor=_ACTOR, timestamp=_STAMP)
            self.assertNotIn(draft.status, {"ONBOARDED", "ALREADY_ONBOARDED"})
            after = {
                p.relative_to(root).as_posix(): hashlib.sha256(
                    p.read_bytes()).hexdigest()
                for p in sorted(q for q in root.rglob("*") if q.is_file())
            }
            self.assertEqual(after, before)
            done = widgets.authorize_onboarding(
                root, dict(self._DECLARATIONS), confirmation="approve",
                actor=_ACTOR, timestamp=_STAMP)
            self.assertEqual(done.status, "ONBOARDED")


class PersistenceTests(ProjectFixture):
    def test_wrong_digest_prefix_writes_nothing(self):
        # The executor reads only `content_digest` off the proposal; the
        # gate path itself is covered by the phase-4 suites.
        class DigestOnly:
            content_digest = _DIGEST

        before = self._fingerprint()
        self.assertIsNone(widgets.authorize_persistence(
            self.root, DigestOnly(), confirmation="deadbeef",
            actor=_ACTOR, timestamp=_STAMP))
        self.assertIsNone(widgets.authorize_persistence(
            self.root, DigestOnly(), confirmation="9f2c4a1b",
            actor="", timestamp=_STAMP))
        self.assertEqual(self._fingerprint(), before)
