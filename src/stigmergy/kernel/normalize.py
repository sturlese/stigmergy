"""Entity-name normalization + slug. Deterministic, no LLM."""
import re
import unicodedata

# Legal suffixes stripped for canonicalization (longest/compound first). Covers common US/EU forms.
_SUFFIXES = [
    "s.a.p.i. de c.v.", "s. de r.l. de c.v.", "s.l.u.", "s.a.u.", "s.c.r.", "s.l.", "s.a.",
    "sociedad limitada", "sociedad anonima", "inc", "ltd", "llc",
    "gmbh", "b.v.", "s.r.l.", "limited", "corp", "co", "sl", "sa",
]
_SUFN = [re.sub(r"\s+", " ", re.sub(r"[.,]", " ", s)).strip() for s in _SUFFIXES]


def normalize(name: str) -> str:
    """Canonical key: no accents, lowercase, no punctuation or legal suffixes."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.,()\"'/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    changed = True
    while changed:
        changed = False
        for suf in _SUFN:
            if s.endswith(" " + suf):
                s = s[: -len(suf)].strip()
                changed = True
    return s.strip()


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "x"
