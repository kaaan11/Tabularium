"""Version lookup bound to the repository's single VERSION source."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path


DIST_NAME = "tabularium-os"


def get_version() -> str:
    """Return the source VERSION during development or installed metadata."""
    source_version = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        value = source_version.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        value = ""
    if value:
        return value
    try:
        return installed_version(DIST_NAME)
    except PackageNotFoundError as exc:
        raise RuntimeError("Tabularium version metadata is unavailable") from exc
