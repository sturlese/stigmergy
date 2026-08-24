"""The answer verifier — pure code judging the answering agent, before the answer leaves.

Every figure in the answer must trace to what the tools returned THIS run (not the whole corpus,
so a lucky match elsewhere cannot launder an invented number); every citation must name a page the
run surfaced and its quote must appear verbatim in it — tolerant of whitespace and typographic
punctuation, of nothing else. The strict verdict gate lives OUTSIDE this module, in
`stigmergy.answer.service`, so `verify()` stays a pure judgement.
"""
import re
import unicodedata

from stigmergy.answer.numbers import unverified_figures

# What a RENDERER strips, and nothing else: the agent quotes a page as a reader sees it, so
# markers the reader never saw must not fail the match — otherwise `partial` fires on formatting,
# non-deterministically, and the signal cries wolf.
#
# The set is CLOSED and every member strips only as a MATCHED PAIR: deleting delimiters wherever
# they appear launders (`MAX_RETRIES` would verify `MAXRETRIES`, and a page's own `Revenue*`
# footnote marker — its "there is a caveat" signal — would vanish). `_`/lone `*` are gated on word
# boundaries: CommonMark forbids intraword `_` emphasis, and `a__b__c` must not derender to `abc`.
#
# Strikethrough is not presentation: `~~12%~~ 14%` is the page RETRACTING a value, so the struck
# SPAN is dropped whole — a quote of retracted text finds nothing to match. The drop is bounded at
# `_SPAN`, a known residual: a longer retraction is not dropped and stays quotable as current;
# widening `_STRIKE`'s bound is a decision with its own cost argument, not a casual tightening.
#
# Digits are never touched, and the property preserved is "the quote exists in what the tools
# returned" — it must not soften into "the quote resembles something in the page". Every pattern
# is bounded and newline-free: bodies are attacker-influenced in size, and an unbounded
# `[^\]]+` restarting at every
# `[` is quadratic (6 KB of `[` measured 436 ms), and this runs synchronously inside `async def ask`.
_SPAN = 200          # a delimiter pair spanning more than this is not emphasis, it is a false pair
_WIKILINK = re.compile(rf"\[\[([^\]|\n]{{1,{_SPAN}}})(?:\|([^\]\n]{{1,{_SPAN}}}))?\]\]")
_MDLINK = re.compile(rf"\[([^\]\n]{{1,{_SPAN}}})\]\(([^)\s\n]{{0,500}})\)")
_STRIKE = re.compile(rf"~~(.{{1,{_SPAN}}}?)~~")
_PAIRED = tuple(re.compile(p) for p in (
    rf"\*\*\*(.{{1,{_SPAN}}}?)\*\*\*", rf"\*\*(.{{1,{_SPAN}}}?)\*\*",
    # Lone `*` gated on word boundaries too (CommonMark permits intraword `*`): in prose it is far
    # more often arithmetic, a glob or a footnote marker — erring toward NOT stripping costs a
    # rare over-refusal; the other way lets a page's own footnote marker disappear from the check.
    rf"(?<![A-Za-z0-9])\*(.{{1,{_SPAN}}}?)\*(?![A-Za-z0-9])",
    rf"`+(.{{1,{_SPAN}}}?)`+",
    rf"(?<![A-Za-z0-9])___(.{{1,{_SPAN}}}?)___(?![A-Za-z0-9])",
    rf"(?<![A-Za-z0-9])__(.{{1,{_SPAN}}}?)__(?![A-Za-z0-9])",
    rf"(?<![A-Za-z0-9])_(.{{1,{_SPAN}}}?)_(?![A-Za-z0-9])",
))


# ── the two sides are NOT symmetric, and that asymmetry is the security property ──────────────
# A PAGE's markers are corpus-given, so consuming them is reader-equivalence; a QUOTE's markers
# are ASSERTED BY THE MODEL, and the link forms and strikethrough carry a PAYLOAD (an
# attacker-chosen destination, a retraction) that consuming would delete from the claim before it
# is checked — the shipped citation carries the RAW quote. So emphasis/code pairs stay symmetric
# (`**A**` asserts nothing `A` does not); link forms and struck spans are consumed on the PAGE
# side only, and a quote written with them must match the page character for character.
def _derender_page(s: str) -> str:
    """`s` as a reader sees it: matched marker pairs consumed, their content kept — except a
    struck span, which is dropped whole because the page is retracting it."""
    s = _WIKILINK.sub(lambda m: m.group(2) or m.group(1), s)
    s = _MDLINK.sub(lambda m: m.group(1), s)
    s = _STRIKE.sub(" ", s)
    return _derender_pairs(s)


def _derender_pairs(s: str) -> str:
    """The payload-free half, applied to BOTH sides: emphasis, strong, and inline code. Adding
    `**` around a word claims nothing removing it would not."""
    for pattern in _PAIRED:
        s = pattern.sub(lambda m: m.group(1), s)
    return s


# ── typographic folding — SYMMETRIC, and outside the asymmetry above on purpose ───────────────
# This layer carries no payload in either direction (NFC and curly-quote/ellipsis substitution are
# spelling variants of the same text), so it applies to both sides; one-sided would let the quote
# assert a character the page lacks. DASHES ARE DELIBERATELY ABSENT: `-` reads as minus and as a
# range separator, so folding it changes what a quote asserts. NFKC is not used because
# compatibility folding maps `①`→`1` and `ﬁ`→`fi`,
# letting a quote claim a digit the page never wrote — canonical equivalence only. Whitespace needs
# no entry: `\s` is Unicode-aware, so the collapse in the normalizers below eats NBSP already.
_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...",
})


def _fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).translate(_FOLD)


# ORDER IS LOAD-BEARING: fold AFTER derender, never before. `…` → `...` lengthens the string and
# every derender pattern is bounded at `_SPAN`, so folding first pushes a 200-char struck span
# past `_STRIKE`'s bound and the page's own RETRACTION stops being dropped. Folding last leaves
# every bound measuring exactly the bytes it always measured.
def _normalize_page(s: str) -> str:
    return re.sub(r"\s+", " ", _fold(_derender_page(s or ""))).strip().lower()


def _normalize_quote(s: str) -> str:
    """The model's own claim, normalized ONLY for what carries no payload — see the block above."""
    return re.sub(r"\s+", " ", _fold(_derender_pairs(s or ""))).strip().lower()


def check_citations(citations, get_page, read_paths: set) -> list[str]:
    """Citation problems, human-readable. A citation is valid when its path was surfaced during
    the run and its quote appears in that page's body or title, normalized for whitespace and
    typographic punctuation."""
    problems = []
    # One normalization per PAGE, not per citation: the haystack is a pure function of the path.
    hay_by_path: dict[str, str] = {}
    for c in citations:
        if c.path not in read_paths:
            problems.append(f"citation to a page the run never surfaced: {c.path}")
            continue
        page = get_page(c.path)
        if not page:
            problems.append(f"citation to an unknown page: {c.path}")
            continue
        # An empty quote is a problem, not a free pass: a citation IS its quote — without one
        # there is no claim to check, and `verified` must not be reachable that way.
        quote = _normalize_quote(c.quote)
        if not quote:
            problems.append(f"citation with no quote: {c.path}")
            continue
        if c.path not in hay_by_path:
            hay_by_path[c.path] = _normalize_page(
                f"{page.get('title', '')} {page.get('body', '')}")
        if quote not in hay_by_path[c.path]:
            problems.append(f"citation quote not found in {c.path}: {c.quote[:80]!r}")
    return problems


def verify(out, evidence_text: str, get_page, read_paths: set) -> dict:
    """Deterministic verdict on one AnswerOutput. Refusals are vacuously verified —
    refusing with no evidence is the correct behavior, not a defect."""
    if out.refused:
        return {"verdict": "verified", "unverified_figures": [], "citation_problems": []}
    figures = unverified_figures(out.answer_markdown, evidence_text)
    citation_problems = check_citations(out.citations, get_page, read_paths)
    if not out.citations and out.answer_markdown.strip():
        citation_problems.append("answer carries no citations")
    problems = len(figures) + len(citation_problems)
    verdict = "verified" if problems == 0 else ("partial" if problems == 1 else "failed")
    return {"verdict": verdict, "unverified_figures": figures, "citation_problems": citation_problems}


def feedback(question: str, out, verdict: dict) -> str:
    """The corrective-retry prompt: the original question plus the verifier's findings."""
    parts = []
    if verdict["unverified_figures"]:
        parts.append("these figures do NOT appear in any tool result you gathered: "
                     + ", ".join(verdict["unverified_figures"]))
    if verdict["citation_problems"]:
        parts.append("citation problems: " + "; ".join(verdict["citation_problems"]))
    return (f"{question}\n\nA previous attempt answered:\n---\n{out.answer_markdown[:2000]}\n---\n"
            f"DETERMINISTIC VERIFIER: {'; '.join(parts)}. Re-answer using ONLY figures present in "
            "tool results and verbatim quotes from surfaced pages — or refuse if the evidence is "
            "insufficient.")
