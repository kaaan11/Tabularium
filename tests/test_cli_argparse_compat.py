"""`tabu <cmd> ... --opt X PATH` must work on Python 3.12.3 (Ubuntu 24.04)."""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from unittest.mock import patch

from tabularium.cli import _parse_args, build_parser


class TrailingPathTests(unittest.TestCase):
    def test_trailing_word_left_over_by_old_argparse_becomes_the_path(self):
        # What argparse < 3.12.7 returns for: explain work --task T /p --json
        leftover = (argparse.Namespace(command="explain", instruction="work", path=None,
                                       task="T", json=True), ["/p"])
        parser = build_parser()
        with patch.object(parser, "parse_known_args", return_value=leftover):
            args = _parse_args(parser, [])
        self.assertEqual(args.path, "/p")

    def test_other_leftovers_are_still_errors(self):
        for extras, path in ((["--bogus"], None), (["/a", "/b"], None), (["/b"], "/a")):
            leftover = (argparse.Namespace(command="explain", path=path), extras)
            parser = build_parser()
            with self.subTest(extras=extras, path=path), \
                    patch.object(parser, "parse_known_args", return_value=leftover), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                _parse_args(parser, [])
            self.assertEqual(raised.exception.code, 2)

    def test_real_parse_puts_path_after_options(self):
        args = _parse_args(build_parser(), ["explain", "work", "--task", "TASK-2026-0001", "/p", "--json"])
        self.assertEqual((args.instruction, args.path, args.task, args.json),
                         ("work", "/p", "TASK-2026-0001", True))


if __name__ == "__main__":
    unittest.main()
