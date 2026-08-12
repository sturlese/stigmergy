"""Atomic text writes (tmp file + same-directory os.replace) — the single copy of the idiom."""
import os


def write_text_atomic(path: str, text: str) -> None:
    """Write text so a concurrent reader sees the old file or the new one, never a partial."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
