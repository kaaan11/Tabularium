"""Thin command-line entry point for the Context Loader.

This is not a general Tabularium CLI. It exposes exactly one operation: build an
ephemeral Context Package and print it as deterministic JSON to stdout.

    python -m tabularium.engine.context_loader --project <project-root> [--task TASK-...]

Exit codes report the package's derived status; they never repair anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .loader import load_context
from .model import BLOCKED, DIAGNOSTIC_ONLY


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="context-loader",
        description="Deterministic, read-only Tabularium Context Loader. Produces "
                    "an ephemeral Context Package as JSON on stdout. Writes "
                    "nothing to the project.")
    p.add_argument("--project", required=True, metavar="PATH",
                   help="project root (the directory containing .ai/)")
    p.add_argument("--task", metavar="TASK-YYYY-NNNN", default=None,
                   help="root Task id; omit for project orientation mode")
    p.add_argument("--select", action="append", default=[],
                   metavar="ID[#Hn]",
                   help="explicitly select a canonical record id or an "
                        "INV-YYYY-NNNN#Hn fragment (repeatable)")
    p.add_argument("--include-architecture", action="store_true",
                   help="load .ai/architecture.md (on-demand only)")
    p.add_argument("--ai-os-root", metavar="PATH", default=None,
                   help="Tabularium ruleset root (defaults to the installation "
                        "containing this loader)")
    p.add_argument("--no-recent", action="store_true",
                   help="do not annotate the newest Handoff/Report candidate "
                        "as RECENT")
    p.add_argument("--compact", action="store_true",
                   help="emit single-line JSON instead of indented JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project)
    if not root.is_dir():
        print(f"context-loader: not a directory: {args.project}",
              file=sys.stderr)
        return 2
    pkg = load_context(root, task=args.task, select=args.select,
                       include_architecture=args.include_architecture,
                       ai_os_root=args.ai_os_root,
                       recent=not args.no_recent)
    sys.stdout.write(pkg.to_json(indent=None if args.compact else 2) + "\n")
    if pkg.package_status == DIAGNOSTIC_ONLY:
        return 3
    if pkg.package_status == BLOCKED:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
