"""Small deterministic Markdown structure scanner used by M2.4.

This is intentionally not a general Markdown parser.  It recognizes only the
ATX headings and fenced-code boundaries needed by the Context Loader.  All
structural consumers share this implementation so a heading cannot be native
structure for one record family and fenced narrative for another.
"""

from __future__ import annotations

import re


ATX_OPEN_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
ATX_CLOSE_RE = re.compile(r"(?:^|[ \t]+)(#+)$")
FENCE_START_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def _fence_parts(line: str) -> tuple[str, int, str] | None:
    """Return (character, run length, trailing text) for a fence candidate."""
    match = FENCE_START_RE.match(line)
    if match is None:
        return None
    token = match.group(2)
    return token[0], len(token), match.group(3)


def parse_atx_heading(line: str) -> tuple[int, str] | None:
    """Parse one heading under the accepted ATX subset.

    A closing hash run is syntax only when separated from preceding content by
    horizontal whitespace (or when it is the entire content). Attached hashes
    remain part of the exact heading text.
    """
    match = ATX_OPEN_RE.match(line)
    if match is None:
        return None
    content = (match.group(2) or "").rstrip(" \t")
    closing = ATX_CLOSE_RE.search(content)
    if closing is not None:
        content = content[:closing.start()].rstrip(" \t")
    return len(match.group(1)), content.strip(" \t")


def iter_headings(body: str):
    """Yield ``(line_index, level, text)`` outside fenced code blocks.

    An opening fence may carry info text.  Once open, only the same delimiter
    character with a run at least as long as the opener and whitespace-only
    trailing content closes it.  Other fence-looking lines are content; fences
    do not nest and EOF never implicitly closes one.
    """
    fence_char: str | None = None
    fence_len = 0
    for index, line in enumerate(body.splitlines()):
        fence = _fence_parts(line)
        if fence_char is None:
            if fence is not None:
                char, length, trailing = fence
                # Backtick info strings cannot themselves contain backticks.
                if char != "`" or "`" not in trailing:
                    fence_char, fence_len = char, length
                    continue
        else:
            if fence is not None:
                char, length, trailing = fence
                if char == fence_char and length >= fence_len \
                        and not trailing.strip():
                    fence_char, fence_len = None, 0
            continue

        heading = parse_atx_heading(line)
        if heading is not None:
            level, text = heading
            yield index, level, text


def section_text(body: str, start: int, level: int) -> str:
    """Return a heading section through the next equal/higher real heading."""
    lines = body.splitlines()
    stops = [index for index, heading_level, _ in iter_headings(body)
             if index > start and heading_level <= level]
    end = stops[0] if stops else len(lines)
    return "\n".join(lines[start:end]).rstrip() + "\n"
