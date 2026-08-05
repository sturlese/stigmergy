"""The answer verifier — pure code judging the answering agent, before the answer leaves.

A generator-judge loop applied at query time:
- every figure in the answer must trace back to what the tools actually returned this run
  (the agent's visible evidence — not the whole corpus, so a lucky match elsewhere can't
  launder an invented number);
- every citation must point at a page the run actually surfaced, and its quote must appear
  verbatim in that page — tolerant of whitespace and of typographic punctuation, of nothing else.

The verdict ships with the answer (`verified` / `partial` / `failed`), and a first attempt with
problems earns exactly one corrective retry with the findings as feedback. The strict verdict gate
(any remaining unverified figure suppresses the answer) lives OUTSIDE this module, in
`stigmergy.answer.service`, so `verify()` stays a pure judgement and the shipping decision stays one
layer up.
"""
import re
import unicodedata

from stigmergy.answer.numbers import unverified_figures

# What a RENDERER strips, and nothing else. The agent quotes a page as a reader sees it — models
# normalize prose by nature — while the verifier compares against the raw bytes, so a quote
# spanning `**bold**`, a `[[wikilink]]` or an inline `code` span missed by a marker the reader
# never saw. That made `partial` fire on formatting rather than on substance, repeatedly and
# non-deterministically (the same question verified on one run and degraded on the next), which is
# the corrosive kind of wrong: `partial` means "something could not be verified", and a signal that
# cries wolf is one an operator learns to skip.
#
# The set is CLOSED, and every member is stripped only as a MATCHED PAIR. Deleting the delimiter
# characters wherever they appear is the version that launders: a page saying `MAX_RETRIES = 3`
# would verify a quote saying `MAXRETRIES = 3`, `__init__` would match `init`, and — the dangerous
# one — a page's `Revenue* was 40%` footnote marker would vanish, so the agent could shed the
# page's own "there is a caveat here" signal. Snake_case identifiers, dunders, globs and paths are
# ordinary content in an engineering corpus, so that class is reachable by an honest model, not
# only by a crafted page. CommonMark also forbids intraword `_` emphasis, which is why `_` is
# gated on word boundaries: `a__b__c` renders literally and must not derender to `abc`.
#
# **Strikethrough is not presentation.** `~~12%~~ 14%` is the page RETRACTING a value, so removing
# the markers and keeping the content would make a superseded figure quotable as current — with
# `unverified_figures` no backstop, since the struck number really is in the evidence text. The
# struck SPAN is therefore dropped whole: a quote of retracted text finds nothing to match. It is
# the one member of this set where "what a reader sees" and "what the page asserts" diverge.
#
# **That drop is BOUNDED, and the bound is a known residual — not a guarantee.** `_STRIKE` is
# `_SPAN`-limited like every pattern here (the quadratic-cost reason below), so a retraction longer
# than `_SPAN` is not dropped at all and its text stays quotable as current. `Citation.quote` caps
# a quote at 200 characters, which is exactly a `_SPAN`-sized window onto a longer retraction, so
# the gap is reachable rather than theoretical. Closing it means giving `_STRIKE` its own larger
# bound with its own cost argument — a decision, not a tightening to slip into an unrelated change.
# The claim to hold onto is the bounded one: a struck span UP TO `_SPAN` is dropped whole.
#
# Digits are never touched; the only punctuation removed is a matched delimiter; a word boundary is
# only collapsed where a renderer collapses it too (`**A**B` really does render `AB`). The property
# preserved is "the quote exists in what the tools returned this run", and it must not soften into
# "the quote resembles something in the page". The adversarial twins in
# `tests/answer/test_verify.py` are what hold that line.
#
# The alternative — instruct the agent to quote raw bytes including the markers — was rejected:
# models normalize prose by nature, so that moves the flakiness into the prompt instead of removing
# it, and leaves the verdict crying wolf.
#
# Every pattern is BOUNDED and newline-free. Page bodies are attacker-influenced in size, and an
# unbounded `[^\]]+` restarting at every `[` is quadratic: 6 KB of `[` measured 436 ms, and this
# runs synchronously inside `async def ask`, so the cost is the whole process's, not one caller's.
_SPAN = 200          # a delimiter pair spanning more than this is not emphasis, it is a false pair
_WIKILINK = re.compile(rf"\[\[([^\]|\n]{{1,{_SPAN}}})(?:\|([^\]\n]{{1,{_SPAN}}}))?\]\]")
_MDLINK = re.compile(rf"\[([^\]\n]{{1,{_SPAN}}})\]\(([^)\s\n]{{0,500}})\)")
_STRIKE = re.compile(rf"~~(.{{1,{_SPAN}}}?)~~")
_PAIRED = tuple(re.compile(p) for p in (
    rf"\*\*\*(.{{1,{_SPAN}}}?)\*\*\*", rf"\*\*(.{{1,{_SPAN}}}?)\*\*",
    # Single `*` is gated on word boundaries too, though CommonMark permits intraword `*`
    # emphasis: a lone `*` inside prose is far more often arithmetic (`2*3`), a glob (`*.md`) or a
    # footnote marker than emphasis, and CommonMark's real flanking rules would not pair those
    # either. Erring toward NOT stripping costs a rare over-refusal; erring the other way lets a
    # page's own footnote marker disappear from the check.
    rf"(?<![A-Za-z0-9])\*(.{{1,{_SPAN}}}?)\*(?![A-Za-z0-9])",
    rf"`+(.{{1,{_SPAN}}}?)`+",
    rf"(?<![A-Za-z0-9])___(.{{1,{_SPAN}}}?)___(?![A-Za-z0-9])",
    rf"(?<![A-Za-z0-9])__(.{{1,{_SPAN}}}?)__(?![A-Za-z0-9])",
    rf"(?<![A-Za-z0-9])_(.{{1,{_SPAN}}}?)_(?![A-Za-z0-9])",
))


# ── the two sides are NOT symmetric, and that asymmetry is the security property ──────────────
# A PAGE's markers are given: the corpus contains them and a reader never sees them, so consuming
# them is reader-equivalence. A QUOTE's markers are ASSERTED BY THE MODEL, and three of these
# constructs carry a PAYLOAD that consuming them deletes from the claim before it is checked:
#
#   page:  "See the policy for details."
#   quote: "See [the policy](https://attacker.example/collect?d=…) for details."   -> verified
#   quote: "Margin was ~~and the CEO resigned over fraud~~ 14%."                   -> verified
#
# The shipped citation carries the RAW quote, so a markdown-rendering client displays a clickable
# attacker-chosen destination inside the one element this system calls "a citation you can check".
# The first version of this normalizer applied the whole set to both sides — its adversarial twins
# all tested quotes with markers REMOVED and none with markers ADDED, which is the direction that
# hides text rather than the one that reveals it.
#
# So: emphasis and code pairs stay symmetric (they carry no payload — `**A**` asserts nothing
# `A` does not), while the link forms and the struck span are consumed on the PAGE side only. A
# quote written with a link, a wikilink or a strikethrough must match a page that really contains
# it, character for character.
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
# This layer carries no payload in either direction, so unlike the link forms and the struck span
# it applies to the quote exactly as it applies to the page. Applying it to one side only would be
# the loosening version: the quote could then assert a character the page does not contain.
#
# Two failures it removes, both of them formatting drift a reader cannot see:
#   - NFC: accented letters are ordinary in this corpus, so one reaching the
#     matcher decomposed on one side and composed on the other is ordinary, not an attack. Unicode
#     itself defines the two spellings as the same text.
#   - the quote/ellipsis map: every writing tool substitutes typographic quote marks and ellipses,
#     and a model reproducing prose reproduces them inconsistently. `'` for `’` claims nothing `’`
#     does not.
#
# DASHES ARE DELIBERATELY ABSENT from the table. `tests/answer/test_verify.py` pins "an ASCII
# hyphen is not the page's em dash" as one of the adversarial twins, so folding that class would
# be a DECISION retiring a standing defense, not a widening of this table. It is also the one
# class here that is not plainly payload-free: `-` reads as a minus sign and as a range separator,
# so a folded quote can differ from the page in what it ASSERTS and not only in how it looks.
#
# NFKC is deliberately NOT used either, and the difference is the whole security argument: NFKC is
# COMPATIBILITY folding, so it maps `①` onto `1` and `ﬁ` onto `fi` — a quote could then claim a
# digit the page never wrote, which is precisely the laundering the closed delimiter set above
# exists to prevent. Canonical equivalence only.
#
# Whitespace needs no entry here: `\s` is Unicode-aware for `str` patterns, so the collapse in the
# two normalizers below already eats NBSP and its relatives.
_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...",
})


def _fold(s: str) -> str:
    return unicodedata.normalize("NFC", s).translate(_FOLD)


# ORDER IS LOAD-BEARING: the fold runs AFTER the derender, never before. `…` → `...` makes the
# string LONGER, and every derender pattern is bounded at `_SPAN` — so folding first pushed a
# 200-character struck span to 202, past `_STRIKE`'s bound, and the page's own RETRACTION stopped
# being dropped: a quote of retracted text verified, while the byte-identical span without an
# ellipsis was still correctly refused. The mapping is symmetric; its composition with a bounded,
# page-only pattern was not. Folding last leaves every bound measuring exactly the bytes it
# measured before this layer existed, which is the only version of "this cannot loosen the gate"
# that is actually true.
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
    # One normalization per PAGE, not per citation: N citations to one page used to pay N full
    # passes over a body capped at 6000 characters, and the derender above multiplied that
    # constant. The haystack is a pure function of the path, so it is cached for this call.
    hay_by_path: dict[str, str] = {}
    for c in citations:
        if c.path not in read_paths:
            problems.append(f"citation to a page the run never surfaced: {c.path}")
            continue
        page = get_page(c.path)
        if not page:
            problems.append(f"citation to an unknown page: {c.path}")
            continue
        # An empty quote used to short-circuit the check below and count as no problem at all,
        # which made `verified` reachable with a citation that asserts nothing about the page it
        # names. A citation IS its quote — without one there is no claim to check.
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
