"""Deterministic Read-Only Context Loader (M2.4).

The loader reads a project and produces an ephemeral Context Package.
It never writes to the project.
"""

from .loader import Loader, load_context
from .model import ContextPackage, Diagnostic, SelectedItem

__all__ = ["Loader", "load_context", "ContextPackage", "Diagnostic",
           "SelectedItem"]
