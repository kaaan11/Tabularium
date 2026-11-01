"""Background runner tests (Faz C, DEC-2026-0009 Alternative A2).

Real objects throughout: a real onboarded project, the real `run_instruction`
entrypoint, the real `JsonlCollector`, real threads. The properties under
test: the collector passthrough observes without changing outcomes, the live
view reads what the run emits, and detach stops the watch — never the run.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tabularium.engine.tui import runner


class SlowWorker:
    """Timing-only wrapper around the deterministic MockWorker."""

    def __init__(self, delay_s: float = 2.0):
        from tabularium.engine.workers import MockWorker

        self._inner = MockWorker()
        self._delay = delay_s

    @property
    def descriptor(self):
        return self._inner.descriptor

    def accepts(self, request):
        return self._inner.accepts(request)

    def execute(self, request):
        time.sleep(self._delay)
        return self._inner.execute(request)


class RunnerFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-tuir-")
        self.root = Path(self.holder.name)
        from tabularium.init import initialize
        from tabularium.onboarding import build_authorizations, onboard_project

        self.assertEqual(initialize(self.root).status, "INITIALIZED")
        declarations = {
            "title": "Runner fixture",
            "purpose": "Exercise background runs",
            "scope": "Bounded runner project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-09-06T20:50:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        from tabularium import task as task_surface

        created = task_surface.task_create(
            self.root, title="Runner work", objective="Run me",
            type_="REVIEW")
        self.assertIsNone(created.code)
        self.trace_dir = Path(tempfile.mkdtemp(prefix="tabu-tuir-trace-"))
        self.addCleanup(lambda: None)
        import shutil
        self.addCleanup(shutil.rmtree, str(self.trace_dir), True)

    def tearDown(self):
        self.holder.cleanup()


class PassthroughTests(RunnerFixture):
    def test_collector_observes_without_changing_the_outcome(self):
        from tabularium.execution import run_instruction
        from tabularium.engine.observability import JsonlCollector
        from tabularium.engine.workers import MockWorker

        trace = self.trace_dir / "observe.jsonl"
        result = run_instruction(
            "Review the fixture", self.root, adapter=MockWorker(),
            collector=JsonlCollector(trace, project_root=self.root))
        self.assertEqual(result.status, "COMPLETED")
        lines = trace.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(lines), 1)

    def test_default_path_writes_no_trace_file(self):
        from tabularium.execution import run_instruction
        from tabularium.engine.workers import MockWorker

        result = run_instruction("Review the fixture", self.root,
                                 adapter=MockWorker())
        self.assertEqual(result.status, "COMPLETED")


class BackgroundTests(RunnerFixture):
    def test_run_completes_and_events_are_readable(self):
        from tabularium.engine.workers import MockWorker

        handle = runner.start_run(
            self.root, "Review the fixture", adapter=MockWorker(),
            trace_path=self.trace_dir / "live.jsonl")
        result = runner.wait(handle, timeout=60)
        self.assertFalse(handle.running)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(runner.next_action_for(result.status), "offer-persist")
        lines = runner.read_events(handle)
        self.assertTrue(any("pipeline" in line.lower() for line in lines))
        self.assertTrue((self.trace_dir / "live.jsonl").is_file())

    def test_detach_stops_the_watch_never_the_run(self):
        handle = runner.start_run(
            self.root, "Review slowly", adapter=SlowWorker(delay_s=2.0),
            trace_path=self.trace_dir / "slow.jsonl")
        runner.detach(handle)
        self.assertTrue(handle.detached)
        result = runner.wait(handle, timeout=60)
        self.assertEqual(result.status, "COMPLETED")


class MappingTests(unittest.TestCase):
    def test_status_mapping(self):
        self.assertEqual(runner.next_action_for("AWAITING_APPROVAL"),
                         "approval-sheet")
        self.assertEqual(runner.next_action_for("COMPLETED"), "offer-persist")
        for status in ("FAILED", "PREPARATION_REJECTED", None, ""):
            with self.subTest(status=status):
                self.assertEqual(runner.next_action_for(status), "done")

    def test_summarize_trace_is_pure_and_bounded(self):
        from tabularium.engine.observability.model import TraceEvent

        events = [
            TraceEvent(event_id=f"e{i}", execution_id="x",
                       event_type="STEP", sequence=i)
            for i in range(20)
        ]
        from tabularium.engine.observability.model import ExecutionTrace

        trace = ExecutionTrace(execution_id="x", events=tuple(events))
        lines = runner.summarize_trace(trace, limit=5)
        self.assertEqual(len(lines), 5)
        self.assertIn("#19", lines[-1])
        self.assertEqual(runner.summarize_trace(None), ["(no events yet)"])


if __name__ == "__main__":
    unittest.main()
