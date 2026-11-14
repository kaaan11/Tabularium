# tempconv (Tabularium demo project)

A deliberately small Python project used to show what a Tabularium governed
project looks like. The `.ai/` directory was created with `tabu init` and
`tabu onboard`, then extended with a few records by hand.

Use Linux and Python 3.12+, matching Tabularium and public CI. From the
Tabularium repository root, install into a virtualenv using the extras in
[pyproject.toml](../../pyproject.toml):

```sh
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[runtime,dev]"
```

The `dev` extra supplies pytest for this demo's
[test command](.ai/change.yaml). `tabu run --change` also requires a worker
and bubblewrap (`bwrap`) with usable unprivileged user namespaces. Follow the
[root tour](../../README.md) to copy the demo into a disposable git repository
and configure git identity. For read-only diagnostics, from this directory:

```sh
tabu status
tabu doctor
tabu explain "Add a kelvin_to_celsius function with a test"
```

The test suite also uses this project as a realistic fixture.
