"""Unified diff parser and deterministic in-memory patch applier."""

from __future__ import annotations

import re
from typing import NamedTuple
from .model import FilePatch

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ParsedHunk(NamedTuple):
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str]


def parse_unified_diff(diff_text: str) -> list[FilePatch]:
    """Parse a unified diff string into a list of FilePatch objects."""
    lines = diff_text.splitlines()
    patches: list[FilePatch] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        # Look for --- and +++ headers
        if line.startswith("--- "):
            src_path = line[4:].strip()
            # Next line must be +++
            if i + 1 < n and lines[i + 1].startswith("+++ "):
                tgt_path = lines[i + 1][4:].strip()
                i += 2
                # Determine normalized relative path
                norm = _normalize_patch_path(tgt_path if tgt_path != "/dev/null" else src_path)
                is_creation = (src_path == "/dev/null")
                is_deletion = (tgt_path == "/dev/null")

                added: list[str] = []
                removed: list[str] = []
                hunks: list[str] = []
                current_hunk: list[str] = []

                # Inside a hunk every line is content, whatever it starts with:
                # an added line "++x" is "+++x" and a removed "-- x" is "--- x".
                # The header's line counts, not prefixes, decide where it ends.
                old_left = new_left = 0
                while i < n:
                    l = lines[i]
                    if old_left > 0 or new_left > 0:
                        if l.startswith("\\"):
                            pass
                        elif l.startswith("+"):
                            added.append(l[1:])
                            new_left -= 1
                        elif l.startswith("-"):
                            removed.append(l[1:])
                            old_left -= 1
                        else:
                            old_left -= 1
                            new_left -= 1
                        current_hunk.append(l)
                        i += 1
                        continue
                    if l.startswith("--- ") or l.startswith("diff --git "):
                        break
                    if l.startswith("@@"):
                        if current_hunk:
                            hunks.append("\n".join(current_hunk))
                            current_hunk = []
                        match = _HUNK_HEADER.match(l)
                        if match:
                            old_left = int(match.group(2)) if match.group(2) is not None else 1
                            new_left = int(match.group(4)) if match.group(4) is not None else 1
                    elif l.startswith("+"):
                        added.append(l[1:])
                    elif l.startswith("-"):
                        removed.append(l[1:])
                    current_hunk.append(l)
                    i += 1
                if current_hunk:
                    hunks.append("\n".join(current_hunk))

                patches.append(FilePatch(
                    source_path=src_path,
                    target_path=tgt_path,
                    normalized_path=norm,
                    added_lines=tuple(added),
                    removed_lines=tuple(removed),
                    hunks=tuple(hunks),
                    is_creation=is_creation,
                    is_deletion=is_deletion,
                ))
                continue
        i += 1

    return patches


def _normalize_patch_path(path: str) -> str:
    cleaned = path.strip().strip('"').strip("'")
    # Strip a/ or b/ prefixes standard in git diffs
    if cleaned.startswith("a/") or cleaned.startswith("b/"):
        cleaned = cleaned[2:]
    return cleaned.replace("\\", "/")


def apply_patch_to_text(original_text: str, hunks_text: tuple[str, ...] | list[str]) -> str:
    """Apply unified diff hunks deterministically to original file text.

    Raises ValueError if a hunk cannot be applied due to mismatch.
    """
    orig_lines = original_text.splitlines()
    result_lines = list(orig_lines)
    offset = 0

    for hunk_str in hunks_text:
        hunk_lines = hunk_str.splitlines()
        if not hunk_lines or not hunk_lines[0].startswith("@@"):
            continue

        match = _HUNK_HEADER.match(hunk_lines[0])
        if not match:
            raise ValueError(f"malformed hunk header: {hunk_lines[0]}")

        old_start = int(match.group(1))
        old_len = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_len = int(match.group(4)) if match.group(4) is not None else 1

        # Calculate position in result_lines
        # 1-indexed in hunk, 0-indexed in list
        pos = (old_start - 1) + offset

        hunk_body = hunk_lines[1:]
        # Extract expected old lines and replacement lines from hunk
        expected_old: list[str] = []
        replacement: list[list[str]] = []
        new_block: list[str] = []

        for hl in hunk_body:
            if hl.startswith(" "):
                expected_old.append(hl[1:])
                new_block.append(hl[1:])
            elif hl.startswith("-"):
                expected_old.append(hl[1:])
            elif hl.startswith("+"):
                new_block.append(hl[1:])

        # Verify old lines match at pos
        if expected_old:
            actual_old = result_lines[pos:pos + len(expected_old)]
            if actual_old != expected_old:
                # Try finding exact match near pos (search window +/- 5)
                found_pos = None
                window = 10
                start_search = max(0, pos - window)
                end_search = min(len(result_lines) - len(expected_old) + 1, pos + window)
                for candidate in range(start_search, end_search):
                    if result_lines[candidate:candidate + len(expected_old)] == expected_old:
                        found_pos = candidate
                        break
                if found_pos is None:
                    raise ValueError(
                        f"Hunk mismatch at line {old_start}: expected {expected_old[:3]!r}, "
                        f"found {actual_old[:3]!r}"
                    )
                pos = found_pos

        # Replace slice
        slice_end = pos + len(expected_old)
        result_lines[pos:slice_end] = new_block
        offset += len(new_block) - len(expected_old)

    trailing_newline = original_text.endswith("\n") if original_text else True
    res = "\n".join(result_lines)
    if trailing_newline and res and not res.endswith("\n"):
        res += "\n"
    return res
