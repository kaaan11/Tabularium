"""TABU_WORKER_ENV: operator-named variables cross the worker boundary."""
from __future__ import annotations

import unittest

from tabularium.engine.workers.cli.transport import _environment, worker_env_passthrough


class TestWorkerEnvPassthrough(unittest.TestCase):

    def test_default_allowlist_unchanged(self):
        env = _environment({"PATH": "/bin", "HOME": "/h", "EVREN_API_KEY": "k", "SECRET": "s"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h"})

    def test_named_variable_is_passed(self):
        parent = {"PATH": "/bin", "EVREN_API_KEY": "k", "OTHER_KEY": "o",
                  "TABU_WORKER_ENV": "EVREN_API_KEY"}
        env = _environment(parent)
        self.assertEqual(env.get("EVREN_API_KEY"), "k")
        self.assertNotIn("OTHER_KEY", env)
        self.assertNotIn("TABU_WORKER_ENV", env)

    def test_dangerous_and_malformed_names_are_refused(self):
        parent = {"PATH": "/bin", "LD_PRELOAD": "/x.so", "PYTHONPATH": "/p",
                  "GIT_DIR": "/g", "OPENCODE_PERMISSION": "{}", "OPENCODE_CONFIG": "/c",
                  "NODE_OPTIONS": "--require x", "BASH_ENV": "/b", "DYLD_INSERT_LIBRARIES": "/d",
                  # Present in the environment, so only the name syntax check refuses them.
                  "A-B": "x", "*": "y", "EVREN_*": "z",
                  "TABU_WORKER_ENV": "LD_PRELOAD, PYTHONPATH,GIT_DIR,OPENCODE_PERMISSION,"
                                     "OPENCODE_CONFIG,NODE_OPTIONS,BASH_ENV,DYLD_INSERT_LIBRARIES,"
                                     "*,EVREN_*,A-B,MISSING_KEY"}
        passed, refused = worker_env_passthrough(parent)
        self.assertEqual(passed, ())
        self.assertEqual(len(refused), 12)
        env = _environment(parent)
        for name in ("LD_PRELOAD", "PYTHONPATH", "GIT_DIR", "OPENCODE_PERMISSION",
                     "OPENCODE_CONFIG", "NODE_OPTIONS", "BASH_ENV", "DYLD_INSERT_LIBRARIES"):
            self.assertNotIn(name, env)

    def test_opt_in_cannot_override_worker_permission(self):
        """OpenCode adds OPENCODE_PERMISSION itself; an opted-in value must never replace it."""
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        import json, tempfile
        from tabularium.engine.workers.cli.opencode_worker import OpenCodeCLIWorker
        from tabularium.engine.workers.cli.bounded_process import Capture
        from tabularium.engine.workers import WorkerRequest

        with tempfile.TemporaryDirectory() as d, \
                patch("tabularium.engine.workers.cli.opencode_worker.BoundedProcess") as bp, \
                patch.dict("os.environ", {"OPENCODE_PERMISSION": '{"bash":"allow"}',
                                          "TABU_WORKER_ENV": "OPENCODE_PERMISSION"}):
            bp.return_value = MagicMock(run=MagicMock(return_value=Capture("EXITED", 0, "")))
            OpenCodeCLIWorker(executable="/bin/true", cwd=d, change_mode=True).execute(
                WorkerRequest(request_id="r", intent="t", payload={}))
            env = bp.call_args.kwargs["env"]
        self.assertEqual(json.loads(env["OPENCODE_PERMISSION"])["bash"], "deny")


if __name__ == "__main__":
    unittest.main()
