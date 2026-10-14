"""Text classification and conservative line-level merging."""

from __future__ import annotations

import difflib

TEXT_LIMIT = 1024 * 1024
LINE_LIMIT = 20000


def _text(data: bytes) -> str | None:
    if b"\0" in data or len(data) > TEXT_LIMIT:
        return None
    try:
        result = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return result if len(result.splitlines()) <= LINE_LIMIT else None


def _edits(base: list[str], other: list[str]) -> list[tuple]:
    return [(i, j, other[a:b]) for tag, i, j, a, b in
            difflib.SequenceMatcher(None, base, other, autojunk=False).get_opcodes()
            if tag != "equal"]


def merge_text(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
    """Combine disjoint line edits; refuse overlapping or non-text edits."""
    texts = [_text(data) for data in (base, ours, theirs)]
    if any(text is None for text in texts):
        return None
    original, left, right = [text.splitlines(keepends=True) for text in texts]
    ours_edits, theirs_edits = _edits(original, left), _edits(original, right)
    for a, b, replacement in ours_edits:
        for c, d, other in theirs_edits:
            if (a, b, replacement) == (c, d, other):
                continue
            overlap = max(a, c) < min(b, d)
            same_insertion = a == b == c == d
            inside = (a == b and c < a < d) or (c == d and a < c < b)
            if overlap or same_insertion or inside:
                return None
    edits = list(ours_edits)
    edits.extend(edit for edit in theirs_edits if edit not in edits)
    result, cursor = [], 0
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1])):
        result.extend(original[cursor:start])
        result.extend(replacement)
        cursor = end
    result.extend(original[cursor:])
    return "".join(result).encode("utf-8")
