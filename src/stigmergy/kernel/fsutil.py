"""Shared filesystem primitive: atomic text writes (tmp file + same-directory os.replace).

Every artifact written through it — a page, a view, a registry — is read by another stage or by a
human while the writer may be mid-write. Hand-rolling the idiom at each write site means the one
site that skips it is the one nobody notices; this is the single copy. Atomicity is a property of
the write, not of what is written, which is why it belongs at the bottom of the stack.
"""
import os


def write_text_atomic(path: str, text: str) -> None:
    """Write text so a concurrent reader sees the old file or the new one, never a partial."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
