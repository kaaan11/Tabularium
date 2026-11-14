"""`--change` refuses the API worker before any provider call.

The API worker returns model text and writes no files, while the change path
discovers proposed edits from the worktree diff. Until a validated
API-to-patch pipeline exists, `--change --worker api` is refused at preflight
with a specific reason code. A fake completion function stands in for the
provider; no network call is made.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from tabularium.engine.workers.api import APIWorker
from tabularium.execution import run_instruction

from tests.test_v2_09_change_path import ChangePathFixture, FakeWorkerAdapter


class FakeCompletion:
    """Provider stand-in: records calls and returns an OpenAI-shaped reply."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return {"model": kwargs.get("model"),
                "choices": [{"message": {"content": "--- a/src/sample.py\n+++ b/src/sample.py\n"}}]}


class TestChangeRefusesApiWorker(ChangePathFixture):

    def _tasks(self) -> set[str]:
        return {p.name for p in (self.root / ".ai" / "tasks").glob("*.md")}

    def _head(self) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.root),
                              capture_output=True, text=True, check=True).stdout.strip()

    def _assert_refused_before_call(self, result, fake: FakeCompletion,
                                    tasks_before: set[str], head_before: str) -> None:
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "CHANGE_WORKER_UNSUPPORTED")
        self.assertIn("--worker native", result.message)
        self.assertEqual(fake.calls, 0, "no provider call may be made")
        self.assertEqual(self._tasks(), tasks_before, "no candidate Task is created")
        self.assertEqual(self.list_worktrees(), [str(self.root)])
        self.assertEqual(self._head(), head_before)

    def test_explicit_api_worker_is_refused_before_provider_call(self):
        fake = FakeCompletion()
        tasks, head = self._tasks(), self._head()
        for name in ("api", "API-model", "litellm"):
            with self.subTest(worker=name), \
                    patch("tabularium.engine.workers.api.worker.load_litellm_completion",
                          return_value=fake):
                result = run_instruction("Edit sample", self.root, worker=name,
                                         model="fake/model", change=True,
                                         confirm=lambda _: True)
                self._assert_refused_before_call(result, fake, tasks, head)

    def test_api_worker_without_model_gets_the_same_refusal(self):
        fake = FakeCompletion()
        tasks, head = self._tasks(), self._head()
        with patch("tabularium.engine.workers.api.worker.load_litellm_completion",
                   return_value=fake), \
                patch.dict("os.environ", {"TABU_MODEL": ""}):
            result = run_instruction("Edit sample", self.root, worker="api",
                                     change=True, confirm=lambda _: True)
        self._assert_refused_before_call(result, fake, tasks, head)

    def test_default_selection_never_falls_back_to_api_worker(self):
        """With no CLI worker installed, a configured model must not select the API worker."""
        fake = FakeCompletion()
        with patch("tabularium.engine.workers.api.worker.load_litellm_completion",
                   return_value=fake), \
                patch("tabularium.change_path.find_native_executable", return_value=None), \
                patch("tabularium.change_path.find_opencode_executable", return_value=None):
            result = run_instruction("Edit sample", self.root, model="fake/model",
                                     change=True, confirm=lambda _: True)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "WORKER_SELECTION_REQUIRED")
        self.assertIn("--worker api", result.message)
        self.assertEqual(fake.calls, 0)

    def test_injected_api_model_adapter_is_refused(self):
        fake = FakeCompletion()
        tasks, head = self._tasks(), self._head()
        adapter = APIWorker(completion_fn=fake)
        result = run_instruction("Edit sample", self.root, adapter=adapter,
                                 model="fake/model", change=True,
                                 confirm=lambda _: True)
        self._assert_refused_before_call(result, fake, tasks, head)

    def test_cli_coding_adapter_path_is_unchanged(self):
        def mutate(wt):
            (wt / "src" / "sample.py").write_text(
                "def calculate_total(a, b):\n    return b + a\n", encoding="utf-8")

        adapter = FakeWorkerAdapter(mutate_fn=mutate)
        result = run_instruction("Swap operands", self.root, adapter=adapter,
                                 change=True, confirm=lambda _: True)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(adapter.call_count, 1)


class TestApiWorkerOutsideChangeIsUnaffected(ChangePathFixture):

    def test_read_only_api_run_still_reaches_the_worker(self):
        fake = FakeCompletion()
        with patch("tabularium.engine.workers.api.worker.load_litellm_completion",
                   return_value=fake):
            result = run_instruction("Summarize sample", self.root, worker="api",
                                     model="fake/model", task_id=self.task_id)
        self.assertNotEqual(result.code, "CHANGE_WORKER_UNSUPPORTED")
        self.assertEqual(fake.calls, 1)
