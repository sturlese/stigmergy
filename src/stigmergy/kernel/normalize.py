"""Entity-name normalization + slug. Deterministic, no LLM.

**Two keys, two questions, and the difference is a safety property.**

`resolution_key` answers *"which registered entity does this TEXT name?"* — the question asked at
FILING time, where a false positive anchors a page to the wrong entity silently and corrupts a
timeline nobody will re-read. It folds only what is not a judgment at all: accents, case and
punctuation. Whether `Cofers` and `Cofers Co` are one company is a judgment, and judgment belongs
to the agent with the corpus in front of it, fenced by code (the id it declares must exist in the
registry, and uncertainty parks).

`normalize` answers *"would this NEW name ever be confused with one we already have?"* — the
question asked at MINT time by `entities.birth._refuse_collisions`, where a false NEGATIVE lets a
duplicate identity through a governed gate. That failure falls closed onto a human, so folding
aggressively is correct there and only there: it strips the legal-suffix table below on top of the
resolution fold. The knowledge repo's own contract linter mirrors this match key as a declared
duplication across two repos with no shared import, so changing `normalize` is a two-repo decision.
"""
import re
import unicodedata

# Legal suffixes stripped for COLLISION detection only (longest/compound first). Covers common
# US/EU forms. Never consulted when resolving a capture — see the module docstring.
_SUFFIXES = [
    "s.a.p.i. de c.v.", "s. de r.l. de c.v.", "s.l.u.", "s.a.u.", "s.c.r.", "s.l.", "s.a.",
    "sociedad limitada", "sociedad anonima", "inc", "ltd", "llc",
    "gmbh", "b.v.", "s.r.l.", "limited", "corp", "co", "sl", "sa",
]
_SUFN = [re.sub(r"\s+", " ", re.sub(r"[.,]", " ", s)).strip() for s in _SUFFIXES]


def resolution_key(name: str) -> str:
    """The RESOLUTION key: no accents, lowercase, no punctuation — and nothing else folded.

    Two spellings share this key only when they are the same string modulo how a keyboard and a
    locale render it. Anything beyond that (a legal form, a qualifier, a former name, an
    abbreviation) is a claim about the world, not about text, and this function refuses to make it.

    `None` folds to `""`, which every caller reads as "names nothing": this is the fold the MCP
    server's alias resolution reaches for too, and there a missing name is an ordinary shape of the
    data rather than a programming error.
    """
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.,()\"'/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize(name: str) -> str:
    """The COLLISION key: `resolution_key` plus legal suffixes stripped.

    Strictly coarser than `resolution_key`, deliberately: at the mint gate a name that folds onto an
    existing one is refused to a human, so over-folding costs a question and under-folding mints a
    duplicate identity nothing will ever reconcile.
    """
    s = resolution_key(name)
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
