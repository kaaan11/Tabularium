"""Test support: neutralize ambient facts the suite must not depend on.

A source checkout whose installed `tabularium-os` distribution metadata carries a
different version than the checkout's own VERSION file makes the diagnostics
emit `VERSION_SOURCE_INSTALLED_MISMATCH`, which drives `overall_state` to
DEGRADED. That is CORRECT product behaviour - it truthfully reports an
incoherent environment, and `test_version_source_installed_mismatch_is_warned`
pins it deliberately.

It is not what the health, task-selection, or refusal-diagnostic tests are
about, though, and depending on it made the whole suite's result vary with
whatever happened to be pip-installed on the machine: a release version bump
leaves every previously-installed copy stale, and 13 cases turn red without a
line of product code changing. Pinning the coherence here states the
precondition instead of inheriting it.

`tabularium.diagnostics._package_versions` is the seam the suite already uses for
this (see the mismatch test in test_m73_diagnostics.py).
"""

from __future__ import annotations

from unittest.mock import patch

from tabularium import diagnostics as _diagnostics
from tabularium.version import get_version


def pin_coherent_installation(test_case) -> None:
    """Report the running version as source, installed, and active alike.

    Call from `setUp`. The patch is released through `addCleanup`, so a test
    that needs an INCOHERENT environment simply patches the same seam itself;
    its inner patch wins.
    """
    version = get_version()
    patcher = patch.object(
        _diagnostics, "_package_versions",
        return_value=(version, version, version, "repository"))
    patcher.start()
    test_case.addCleanup(patcher.stop)
