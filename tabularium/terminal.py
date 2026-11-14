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


# Output-boundary bound for one surfaced diagnostic fragment. Mirrors the
# established per-diagnostic detail bound already used for worker-facing
# context diagnostics in tabularium/engine/execution/pipeline.py (_context_gate).
_DIAGNOSTIC_MAX_CHARS = 160


def _sanitize_fragment(text: str, *,
                       limit: int = _DIAGNOSTIC_MAX_CHARS) -> str:
    """Make one user-derived diagnostic fragment terminal-safe and bounded.

    Every non-printable character (C0/C1 controls, DEL, Unicode line and
    format separators included) becomes its visible ``ascii()`` escape, so a
    record can never inject terminal control sequences or fabricate extra
    output lines. Whitespace runs collapse deterministically and the fragment
    is hard-bounded to ``limit``. Escaping runs BEFORE truncation, so no live
    escape sequence can be split. This is the user-facing representation only;
    internal validation evidence is never rewritten.

    The default limit is the established Task-diagnostic bound; callers may
    pass a larger bound when the evidence was already stored under that larger
    bound elsewhere (worker failure detail uses the worker-side 300).
    """
    if not isinstance(text, str):
        return ""
    escaped = "".join(ch if ch.isprintable() else ascii(ch)[1:-1]
                      for ch in text)
    collapsed = " ".join(escaped.split())
    if len(collapsed) > limit:
        return collapsed[:limit - 3].rstrip() + "..."
    return collapsed
