# Contributing

Thanks for your interest in Tabularium. It is alpha software; interfaces and
record formats may still change (see [CHANGELOG.md](CHANGELOG.md)).

External pull requests are welcome. Maintainers respond to issues and pull
requests as time permits, with no guaranteed response time.

Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development setup

Use Linux, Python 3.12 or 3.13, and Git with `user.name` and `user.email` set.
The isolation tests require bubblewrap (`bwrap`) and usable Linux namespaces.
The remote worker tests use local loopback sockets.
[uv](https://docs.astral.sh/uv/) is used below because CI uses it; plain
`pip` in a virtual environment works too.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -e ".[api,runtime,observability,dev]"
```

## Checks

Run these local equivalents of the CI checks before opening a pull request.

```bash
# Full test suite. Sandbox tests skip when bwrap is unusable; CI treats
# that skip as a failure, so check the -rs summary for it.
.venv/bin/python -m pytest tests -q -rs

# Lint baseline (syntax errors and undefined names).
uvx ruff@0.8.6 check --select E9,F63,F7,F82 tabularium tests

# Non-editable wheel smoke test without checkout imports.
uv build --wheel --python 3.12
uv venv --python 3.12 /tmp/wheel-venv
uv pip install --python /tmp/wheel-venv dist/*.whl 'pydantic>=2.9,<3'
/tmp/wheel-venv/bin/python -I tests/wheel_smoke.py
```

CI also runs a `pip-audit` dependency audit and checks that no generated
artifacts (`__pycache__`, `build/`, `dist/`, `*.egg-info`, `*.pyc`) are
tracked. See the [CI workflow](.github/workflows/ci.yml).

The default suite uses offline worker fixtures and needs no provider
credentials. Live smoke checks are available to maintainers as an explicit
opt-in through `TABULARIUM_LIVE_SMOKE`. Keep calls to external services out of
the default suite. See the [README development guide](README.md#development).

## Pull requests

- One topic per pull request; keep unrelated refactors and formatting out.
- Add or update tests for behaviour changes. A bug fix should come with a test
  that fails without the fix.
- Keep the fail-closed behaviour described in the README: when a check cannot
  decide, refuse with a reason code rather than continue.
- Update the README when a command, guarantee or limit changes, and add an
  entry under **Unreleased** in `CHANGELOG.md`.
- All CI jobs must pass.

## Release checklist

Tabularium is not currently published to PyPI.

1. Set the version in `VERSION` and move the **Unreleased** entries in
   `CHANGELOG.md` under that version with the release date.
2. Confirm CI is green on the release commit: tests on 3.12 and 3.13, wheel
   smoke, hygiene, lint and dependency audit. Resolve or document any audit
   finding.
3. Build the wheel from a clean checkout and install it into a fresh virtual
   environment; run `tests/wheel_smoke.py` with `python -I` as above.
4. Check package metadata in `pyproject.toml` (name, version source,
   `requires-python`, dependency ranges, package data).
5. Re-read the README "Status", "Five-minute tour" and "What it guarantees"
   sections against the release and run the tour on the demo project.
6. Review security-relevant changes since the previous release (change gates,
   test sandbox, record authorization, worker integrations).
7. Tag the release commit.

TODO(owner): Decide who may tag releases.

TODO(owner): Decide whether to publish GitHub Releases for those tags.
