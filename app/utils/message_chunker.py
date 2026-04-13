"""Message chunker to split long responses for sending.

We prefer natural boundaries (newlines, then sentence ends, then whitespace) and we
preserve newlines in the returned chunks.
"""

from __future__ import annotations

from typing import List


def _trim_boundary(text: str) -> str:
    return (text or "").strip()


def _find_break_index(text: str, max_length: int) -> int:
    """
    Pick a natural break index <= max_length.

    Preference order:
      1) Newline boundaries
      2) Sentence boundaries (., !, …)
      3) Clause boundaries (;, :, ,)
      4) Whitespace
      5) Hard cut
    """
    if max_length <= 0:
        return 0
    if len(text) <= max_length:
        return len(text)

    min_idx = int(max_length * 0.55)
    min_idx = max(0, min(min_idx, max_length - 1))

    def _search_window(start: int, end: int) -> int:
        # 1) Newlines (break *before* the newline)
        for sep in ("\n\n", "\n"):
            idx = text.rfind(sep, start, end)
            if idx != -1:
                return idx

        # 2) Sentence ends (include punctuation in the left chunk)
        for sep in (".\n", "!\n", "…\n", ". ", "! ", "… "):
            idx = text.rfind(sep, start, end)
            if idx != -1:
                return idx + 1

        # 3) Clause boundaries
        for sep in (";\n", ":\n", ",\n", "; ", ": ", ", "):
            idx = text.rfind(sep, start, end)
            if idx != -1:
                return idx + 1

        # 4) Whitespace
        idx = text.rfind(" ", start, end)
        if idx != -1:
            return idx

        return -1

    idx = _search_window(min_idx, max_length + 1)
    if idx != -1:
        return idx

    idx = _search_window(0, max_length + 1)
    if idx != -1:
        return idx

    return max_length


def _truncate(text: str, max_length: int, *, suffix: str = "…") -> str:
    if max_length <= 0:
        return ""
    s = _trim_boundary(text)
    if len(s) <= max_length:
        return s
    if max_length <= len(suffix):
        return suffix[:max_length]

    cut_limit = max_length - len(suffix)
    idx = _find_break_index(s, cut_limit)
    if idx <= 0:
        idx = cut_limit
    left = s[:idx].rstrip()
    return (left + suffix).strip()


def chunk_message(message: str, max_length: int = 280, *, max_chunks: int | None = None) -> List[str]:
    """
    Split a message into chunks no longer than max_length.

    If max_chunks is set, returns at most that many chunks (best-effort).
    """
    text = _trim_boundary(message)
    if not text:
        return []

    if max_length <= 0:
        return [text]

    if len(text) <= max_length:
        return [text]

    if max_chunks is not None and max_chunks <= 1:
        return [_truncate(text, max_length)]

    if max_chunks == 2:
        idx = _find_break_index(text, max_length)
        left = _trim_boundary(text[:idx])
        right = _trim_boundary(text[idx:])
        if not left:
            left = _trim_boundary(text[:max_length])
            right = _trim_boundary(text[max_length:])
        if not right:
            return [left]
        if len(right) > max_length:
            right = _truncate(right, max_length)
        return [left, right]

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        idx = _find_break_index(remaining, max_length)
        piece = _trim_boundary(remaining[:idx])
        remaining = _trim_boundary(remaining[idx:])
        if not piece:
            piece = _trim_boundary(remaining[:max_length])
            remaining = _trim_boundary(remaining[max_length:])
        chunks.append(piece)

        if max_chunks is not None and len(chunks) >= max_chunks:
            if remaining:
                chunks[-1] = _truncate(chunks[-1] + " " + remaining, max_length)
            break

    return [c for c in chunks if c]
