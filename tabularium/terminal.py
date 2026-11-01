"""Terminal-safe rendering of text that a worker or test command produced."""

from __future__ import annotations

import unicodedata

_NAMED = {"\r": r"\r", "\b": r"\b", "\x1b": r"\x1b", "\x07": r"\x07"}
# Controls, format characters (bidi overrides, isolates, zero-width), line and
# paragraph separators, surrogates, private use and unassigned code points can
# move the cursor, reorder or hide text; all of them are shown as escapes.
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def sanitize_terminal(text: str) -> str:
    """Return text with every non-printing character shown as a visible escape.

    ``\\n`` and ``\\t`` are kept. This is for display only; stored results keep
    the raw text.
    """
    if not text:
        return text
    out: list[str] = []
    for ch in text:
        if ch in ("\n", "\t"):
            out.append(ch)
        elif ch in _NAMED:
            out.append(_NAMED[ch])
        elif unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            cp = ord(ch)
            if cp <= 0xFF:
                out.append(f"\\x{cp:02x}")
            elif cp <= 0xFFFF:
                out.append(f"\\u{cp:04x}")
            else:
                out.append(f"\\U{cp:08x}")
        else:
            out.append(ch)
    return "".join(out)
