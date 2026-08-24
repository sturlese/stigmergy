"""Deterministic entity-name resolution."""
import re
import unicodedata


def resolution_key(name: str) -> str:
    """Fold accents, case, punctuation, and whitespace without inferring identity."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.,()\"'/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()
