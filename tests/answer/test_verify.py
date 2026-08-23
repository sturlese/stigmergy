"""The deterministic answer verifier — the pure half of the answering loop, judged without a model.

Pure and keyless: no service, no Postgres. These pin figure tracing, verbatim citation checking
and the corrective-retry prompt.
"""
import unicodedata

import pytest
from pydantic import ValidationError

from stigmergy.answer import synthesize
from stigmergy.answer.numbers import unverified_figures
from stigmergy.answer.synthesize import AnswerOutput, Citation
from stigmergy.answer.verify_answer import _FOLD, feedback, verify


def test_unverified_figures_matching_is_generous():
    ev = "ARR was 1,200,000 EUR in Q1 2026 (about 40 %)."
    assert unverified_figures("Revenue reached 1.2M, up 40%, in 2026.", ev) == []
    assert unverified_figures("Margin was 77%.", ev) == ["77%"]
    assert unverified_figures("the 3 initiatives", "no digits") == []      # bare digit skipped


def _pages(**pages):
    return lambda path: pages.get(path)


def test_verify_citations_and_figures():
    out = AnswerOutput(answer_markdown="Revenue was 1.3M.",
                       citations=[Citation(path="p.md", quote="Revenue was 1.3M")])
    get_page = _pages(**{"p.md": {"title": "T", "body": "Quarterly. Revenue   was 1.3M this year."}})
    v = verify(out, "tool said: Revenue was 1.3M", get_page, read_paths={"p.md"})
    assert v == {"verdict": "verified", "unverified_figures": [], "citation_problems": []}


def test_this_run_rule_a_corpus_only_figure_is_flagged():
    """The this-run rule: a figure that exists in the corpus but was NOT returned by this run's
    tools is flagged — a lucky corpus match cannot launder an invented number. The evidence_text
    here (what the tools returned) does not contain 9.9M, so it is unverified even though 'the
    page' would contain it elsewhere."""
    out = AnswerOutput(answer_markdown="Revenue was 9.9M.",
                       citations=[Citation(path="p.md", quote="x")])
    get_page = _pages(**{"p.md": {"title": "T", "body": "somewhere far away: 9.9M"}})
    v = verify(out, "tool returned: nothing numeric here", get_page, read_paths={"p.md"})
    assert v["unverified_figures"] == ["9.9M"]


def test_verify_flags_unsurfaced_page_and_missing_quote():
    out = AnswerOutput(answer_markdown="Fine.",
                       citations=[Citation(path="ghost.md", quote="x"),
                                  Citation(path="p.md", quote="never said this")])
    get_page = _pages(**{"p.md": {"title": "T", "body": "actual body"}})
    v = verify(out, "evidence", get_page, read_paths={"p.md"})
    assert v["verdict"] == "failed"
    assert any("never surfaced" in p for p in v["citation_problems"])
    assert any("quote not found" in p for p in v["citation_problems"])


def test_verify_requires_citations_for_substantive_answers():
    out = AnswerOutput(answer_markdown="Something substantive.", citations=[])
    v = verify(out, "evidence", _pages(), read_paths=set())
    assert v["citation_problems"] == ["answer carries no citations"]
    assert v["verdict"] == "partial"


def test_refusal_is_vacuously_verified():
    out = AnswerOutput(refused=True, reason="not in the brain")
    assert verify(out, "", _pages(), set())["verdict"] == "verified"


def test_feedback_carries_both_problem_classes():
    out = AnswerOutput(answer_markdown="Revenue 9.9M.", citations=[])
    v = {"unverified_figures": ["9.9M"], "citation_problems": ["answer carries no citations"]}
    fb = feedback("q?", out, v)
    assert "DETERMINISTIC VERIFIER" in fb and "9.9M" in fb and "citations" in fb


# ── a citation has to assert something ────────────────────────────────────────────────────────
def test_an_empty_quote_is_a_citation_problem_not_a_free_pass():
    """OLD BEHAVIOUR: `check_citations` guarded the verbatim check with `if c.quote and ...`, so an
    EMPTY quote skipped it entirely and contributed zero problems. `verdict: "verified"` — the
    strongest label this system issues — was reachable with a citation that asserts nothing about
    the page it points at, on any page the run happened to surface. An empty quote is a natural
    model output, so this needed no attacker."""
    out = AnswerOutput(answer_markdown="Revenue was 42.7M.",
                       citations=[Citation(path="p.md", quote="")])
    get_page = _pages(**{"p.md": {"title": "T", "body": "nothing about revenue here"}})

    v = verify(out, "tool said: Revenue was 42.7M", get_page, read_paths={"p.md"})

    assert v["citation_problems"] == ["citation with no quote: p.md"]
    assert v["verdict"] != "verified"


def test_a_whitespace_only_quote_is_the_same_hole():
    out = AnswerOutput(answer_markdown="Revenue was 42.7M.",
                       citations=[Citation(path="p.md", quote="   \n\t ")])
    get_page = _pages(**{"p.md": {"title": "T", "body": "nothing about revenue here"}})
    v = verify(out, "tool said: Revenue was 42.7M", get_page, read_paths={"p.md"})
    assert v["citation_problems"] == ["citation with no quote: p.md"]


def test_a_real_quote_still_verifies():
    """The benign twin: tightening the empty case must not touch the ordinary one."""
    out = AnswerOutput(answer_markdown="Revenue was 1.3M.",
                       citations=[Citation(path="p.md", quote="Revenue was 1.3M")])
    get_page = _pages(**{"p.md": {"title": "T", "body": "Revenue was 1.3M this year."}})
    v = verify(out, "tool said: Revenue was 1.3M", get_page, read_paths={"p.md"})
    assert v["verdict"] == "verified" and v["citation_problems"] == []


def test_the_documented_quote_cap_is_enforced_not_merely_described():
    """`Citation.quote`'s `<=200 chars` lived only inside the Field's description string — prose
    aimed at the model, not a constraint — while `answer/service.py` cited it as "`Citation.quote`'s
    own <=200 cap" when justifying its own query cap. One of the two had to become true."""
    with pytest.raises(ValidationError):
        Citation(path="p.md", quote="x" * 201)
    assert Citation(path="p.md", quote="x" * 200).quote      # the boundary itself is allowed


def test_the_citation_path_is_bounded_too_because_it_ships_into_the_audit_column():
    """OLD BEHAVIOUR: `path` carried no `max_length` at all — the twin of the cap above, left
    unfixed one field over.

    `answer.service.audit_summary` writes `[c["path"] for c in citations]` into `audit_log.result`,
    on the stated argument that "a path is the same identifying fact `verdict` and `surfaced`
    already carry — no new disclosure, and 'no question or answer text in this column' stays true
    by construction". That argument holds only while the value IS a path. `path` is model-authored
    free text like `quote` is, so unbounded it is a free channel into the one column whose whole
    contract is that it carries no transcript — and it renders to a reader as a citation link. The
    routine partial path needs no attacker: a citation whose path does not resolve is a citation
    problem, not a dropped citation, so it is logged either way."""
    with pytest.raises(ValidationError):
        Citation(path="wiki/" + "x" * 400, quote="q")
    # The benign twin, and it is the point of the number: 400 is the librarian's own
    # `agent.MAX_IDENTIFIER_LEN`, the ceiling past which it refuses to FILE a page at all — so no
    # legitimate corpus path can trip this, while `quote`'s 200 would have turned a real page into
    # a `ValidationError` out of `ask`, which catches `UsageLimitExceeded` only.
    assert Citation(path="x" * 400, quote="q").path
    assert Citation(path="wiki/customers/initech.md", quote="q").path


# ── a citation must not fail because a renderer's markers were dropped ─────────────────────────
# Observed on staging: the SAME question returned `verified` on one run and `partial` on the next.
# The citation was TRUE — the page says `- **Payments** — internal MVP ready; …` and the agent
# quoted it as a reader sees it, without the emphasis markers. `_normalize` folded whitespace and
# case only, so `**Payments**` vs `Payments` was a miss and a real answer was labelled defective.
#
# Why that is corrosive rather than cosmetic: `partial` is the signal meaning "something here
# could not be verified". Fired constantly for formatting, an operator learns to skip it — and a
# REAL verification failure hides inside the noise. A permanently-yellow verdict is this repo's
# permanently-green test wearing the other colour.
_MD_PAGE = {"title": "Globex June 2026 Investor Report",
            "body": ("## Product\n"
                     "- **Payments** — internal MVP ready; Globex is selecting clients to start "
                     "a private beta.\n"
                     "- _Lending_ is on hold until Q4.\n"
                     "- See [[Globex Monthly Reporting Review Cadence|the cadence decision]] and "
                     "the [public brief](https://example.com/brief).\n"
                     "- Margin was ~~12%~~ 14% after the revision.\n"
                     "- The `entity:` field is stamped by the server.\n")}


@pytest.mark.parametrize("quote", [
    "Payments — internal MVP ready; Globex is selecting clients to start a private be",
    "Lending is on hold until Q4",
    "See the cadence decision and the public brief",
    "The entity: field is stamped by the server",
])
def test_a_quote_a_reader_would_write_verifies_against_the_marked_up_page(quote):
    out = AnswerOutput(answer_markdown="Payments has an internal MVP.",
                       citations=[Citation(path="p.md", quote=quote)])
    v = verify(out, "tool said: Payments has an internal MVP", _pages(**{"p.md": _MD_PAGE}),
               read_paths={"p.md"})
    assert v["citation_problems"] == []
    assert v["verdict"] == "verified"


@pytest.mark.parametrize("quote", ["**Payments** — internal MVP ready",
                                   "Payments — internal MVP ready"])
def test_both_renderings_of_one_true_quote_reach_the_same_verdict(quote):
    """The real flakiness, stated as a property: the SAME true quote, written with the page's
    markers or as a reader sees it, must verify either way. (Asserting `verify(x) == verify(x)`
    would prove nothing — it is a pure function and was already deterministic; the variance lived
    in the model's rendering, which is what this parametrization covers.)"""
    out = AnswerOutput(answer_markdown="Payments has an internal MVP.",
                       citations=[Citation(path="p.md", quote=quote)])
    v = verify(out, "tool said: Payments has an internal MVP", _pages(**{"p.md": _MD_PAGE}),
               read_paths={"p.md"})
    assert v["verdict"] == "verified"


# ── the adversarial twins: this normalization is only safe if it cannot launder a fabrication ──
# The property preserved is "the quote exists in what the tools returned this run". It must not
# soften into "the quote resembles something in the page" — any normalization that could let a
# FABRICATED quote match is a regression of the whole mechanism, not a loosened check.
@pytest.mark.parametrize("quote,why", [
    ("Payments — internal MVP ready; Globex is selecting clients to start a public beta",
     "one word changed: private -> public"),
    ("Margin was 41% after the revision", "a changed FIGURE must never pass"),
    ("Payments internal MVP ready", "removing a word is not a rendering difference"),
    ("PaymentsinternalMVPready", "collapsing word boundaries would match almost anything"),
    ("Payments - internal MVP ready", "an ASCII hyphen is not the page's em dash"),
    ("Lending is on hold until Q3", "Q4 -> Q3"),
])
def test_a_quote_that_is_not_on_the_page_still_fails(quote, why):
    out = AnswerOutput(answer_markdown="Payments has an internal MVP.",
                       citations=[Citation(path="p.md", quote=quote)])
    v = verify(out, "tool said: Payments has an internal MVP", _pages(**{"p.md": _MD_PAGE}),
               read_paths={"p.md"})
    assert v["citation_problems"], why
    assert v["verdict"] != "verified"


@pytest.mark.parametrize("body,quote,why", [
    ("Margin was ~~12%~~ 14% after the revision.", "Margin was 12%",
     "a STRUCK figure is the page RETRACTING it — quotable as current would be worse than a "
     "formatting miss, and the figure check is no backstop (12 really is in the evidence)"),
    ("MAX_RETRIES = 3 in the config", "MAXRETRIES = 3",
     "a snake_case identifier is not emphasis; deleting `_` anywhere would launder it"),
    ("Revenue* was 40% (see the note below)", "Revenue was 40%",
     "a lone `*` is a FOOTNOTE marker — shedding it sheds the page's own caveat signal"),
    ("run 2*3 checks on *.md files", "run 23 checks on .md files",
     "arithmetic and a glob are not an emphasis pair"),
    ("see docs/q3_final_DRAFT.md for the numbers", "docs/q3finalDRAFT.md",
     "a path is not emphasis"),
])
def test_a_marker_that_is_not_a_matched_pair_is_left_alone(body, quote, why):
    """The boundary that keeps this normalization from laundering: delimiters are consumed only as
    MATCHED PAIRS, and `_`/lone-`*` only at word boundaries. Deleting the characters wherever they
    appear is the version that turns an honest model's paraphrase into a false `verified`."""
    out = AnswerOutput(answer_markdown="See the report.",
                       citations=[Citation(path="p.md", quote=quote)])
    v = verify(out, "tool said: see the report", _pages(**{"p.md": {"title": "T", "body": body}}),
               read_paths={"p.md"})
    assert v["citation_problems"], why


def test_a_bounded_derender_stays_fast_on_a_hostile_body():
    """Page bodies are attacker-influenced in size and `verify` runs synchronously inside
    `async def ask`, so a quadratic scan is the whole process's problem, not one caller's. The
    unbounded patterns measured 436 ms on 6 KB of `[`; bounded and newline-free, this is the
    property rather than the measurement."""
    import time
    page = {"title": "T", "body": "[" * 6000 + "\n" + "[[a" * 2000}
    out = AnswerOutput(answer_markdown="See it.",
                       citations=[Citation(path="p.md", quote="nothing to find here")])
    started = time.perf_counter()
    verify(out, "tool", _pages(**{"p.md": page}), read_paths={"p.md"})
    assert time.perf_counter() - started < 1.0


def test_punctuation_and_digits_are_never_stripped():
    """The boundary of the set, stated as a test: only what a RENDERER strips comes out — never
    punctuation, never digits, never word boundaries."""
    page = {"title": "T", "body": "Revenue was 1,200,000 EUR (about 40%) in Q1."}
    for quote in ("Revenue was 1200000 EUR", "Revenue was 1,200,000 EUR about 40% in Q1"):
        out = AnswerOutput(answer_markdown="Revenue was 1,200,000 EUR.",
                           citations=[Citation(path="p.md", quote=quote)])
        v = verify(out, "tool said: 1,200,000 EUR", _pages(**{"p.md": page}), read_paths={"p.md"})
        assert v["citation_problems"], f"{quote!r} is not what the page says"


def test_the_citation_list_is_bounded_like_the_quote_is():
    """Pinned for the same reason the 200-char quote cap is: every per-citation cost in the
    verifier multiplies by this number, and the list is model-controlled."""
    ok = [Citation(path=f"p{i}.md", quote="q") for i in range(synthesize.MAX_CITATIONS)]
    AnswerOutput(answer_markdown="a", citations=ok)
    with pytest.raises(ValidationError):
        AnswerOutput(answer_markdown="a", citations=[*ok, Citation(path="x.md", quote="q")])


# ── BATCH AUDIT F1: a quote may DROP the page's markers; it may not ADD markers of its own ─────
# The first version of this normalizer applied the whole derender to both sides. Every adversarial
# twin above tests a quote with markers REMOVED — the direction that reveals text. None tested a
# quote with markers ADDED, which is the direction that HIDES it: three of the constructs carry a
# payload, and consuming them deletes that payload from the claim before it is checked. The shipped
# citation carries the RAW quote, so a markdown-rendering client would show an attacker-chosen
# destination inside the one element this system calls a citation you can check.
@pytest.mark.parametrize("body,quote,why", [
    ("See the policy for details.",
     "See [the policy](https://attacker.example/collect?d=leak) for details.",
     "a link destination the page does not contain must not be consumed out of the claim"),
    ("See the policy for details.", "See [[../../secret|the policy]] for details.",
     "a wikilink TARGET is payload the label hides"),
    ("Margin was ~~12%~~ 14% after the revision.",
     "Margin was ~~and the CEO resigned over fraud~~ 14% after the revision.",
     "a struck span in the QUOTE would smuggle text the page never said"),
])
def test_a_quote_may_not_carry_markers_that_hide_text(body, quote, why):
    out = AnswerOutput(answer_markdown="See the report.",
                       citations=[Citation(path="p.md", quote=quote)])
    v = verify(out, "tool said: see the report", _pages(**{"p.md": {"title": "T", "body": body}}),
               read_paths={"p.md"})
    assert v["citation_problems"], why
    assert v["verdict"] != "verified"


@pytest.mark.parametrize("body,quote", [
    ("- **Payments** — internal MVP ready", "**Payments** — internal MVP ready"),
    ("The `entity:` field is stamped", "The `entity:` field is stamped"),
])
def test_emphasis_and_code_stay_symmetric_because_they_carry_no_payload(body, quote):
    """The benign twin of the rule above, and the line it draws: `**A**` asserts nothing `A` does
    not, so a quote written WITH those markers still verifies. Only the payload-bearing constructs
    (both link forms, strikethrough) are page-side only."""
    out = AnswerOutput(answer_markdown="Payments has an MVP.",
                       citations=[Citation(path="p.md", quote=quote)])
    v = verify(out, "tool said: Payments has an MVP",
               _pages(**{"p.md": {"title": "T", "body": body}}), read_paths={"p.md"})
    assert v["citation_problems"] == []
    assert v["verdict"] == "verified"


# ── the symmetric typographic fold (`_FOLD`) — see the long comment above it in verify_answer.py ──
# NFC plus curly-quote/ellipsis folding, applied identically to both sides. The twins that matter
# more than the happy path here are the SPECIFICITY ones below: this normalization sits right next
# to the citation-verbatim check that is this module's whole security property, so anything it
# quietly widens is a laundering channel, not a convenience.
def _verify_quote(body: str, quote: str) -> dict:
    out = AnswerOutput(answer_markdown="See the report.",
                       citations=[Citation(path="p.md", quote=quote)])
    return verify(out, "tool said: see the report", _pages(**{"p.md": {"title": "T", "body": body}}),
                  read_paths={"p.md"})


@pytest.mark.parametrize("body,quote", [
    ("The team said “no changes planned” this quarter.",
     'The team said "no changes planned" this quarter.'),                       # curly " -> straight
    ("It’s the client’s own decision, they said.",
     "It's the client's own decision, they said."),                             # curly ' -> straight
    ("Roughly 40%… give or take.", "Roughly 40%... give or take."),        # … -> ...
])
def test_a_true_quote_differing_only_in_typographic_punctuation_verifies(body, quote):
    """Sensitivity: a model reproducing prose reproduces curly quotes and ellipses inconsistently —
    `'` for `’` claims nothing `’` does not, so a quote differing from the page ONLY this
    way must not be flagged."""
    v = _verify_quote(body, quote)
    assert v["citation_problems"] == []
    assert v["verdict"] == "verified"


def test_a_decomposed_vs_composed_accent_verifies_either_direction():
    """Sensitivity, the NFC half: an accented letter reaching the matcher decomposed on one side
    (combining acute accent) and composed on the other (precomposed accented codepoint) is
    ordinary formatting drift, not an attack — Unicode itself defines the two spellings as the
    same text, and any corpus naming a European customer will carry both."""
    composed = "Café Zürich crédit review"                 # precomposed é, ü, é
    decomposed = unicodedata.normalize("NFD", composed)                    # base letter + combining accent
    assert composed != decomposed                                         # sanity: genuinely different bytes

    v_quote_decomposed = _verify_quote(composed, decomposed)
    assert v_quote_decomposed["citation_problems"] == []
    assert v_quote_decomposed["verdict"] == "verified"

    v_page_decomposed = _verify_quote(decomposed, composed)
    assert v_page_decomposed["citation_problems"] == []
    assert v_page_decomposed["verdict"] == "verified"


def test_the_typographic_fold_is_symmetric_by_construction():
    """The fold must apply IDENTICALLY to both sides — an asymmetric version (folded on only one
    side) would let a quote assert a character the page does not literally contain, the same class
    of hole the link-form/strikethrough asymmetry deliberately exists to prevent in the OTHER
    direction. Proven both ways: curly page + straight quote, and straight page + curly quote."""
    straight = 'The team said "no changes planned" this quarter.'
    curly = "The team said “no changes planned” this quarter."

    assert _verify_quote(curly, straight)["citation_problems"] == []
    assert _verify_quote(straight, curly)["citation_problems"] == []


# ── specificity: DASHES stay OUT of the fold, and it must be impossible to widen it silently ─────
def test_fold_table_never_gains_a_dash_mapping():
    """Pins the TABLE's contents directly, not only one behavioral twin — see the long comment
    above `_FOLD` in verify_answer.py: dashes are the one class here that is not plainly
    payload-free (`-` reads as a minus sign and a range separator too), so folding them would be a
    DECISION retiring the standing defense pinned below
    (`test_a_quote_that_is_not_on_the_page_still_fails`'s "an ASCII hyphen is not the page's em
    dash" case), not a widening of this table. A future edit adding any of these to `_FOLD` goes red
    here even before that end-to-end behavioral twin would catch it."""
    dash_like = "‐‑‒–—―−-"   # hyphen … em dash, horizontal bar, minus, ascii '-'
    for ch in dash_like:
        assert ord(ch) not in _FOLD, f"{ch!r} must not be folded — dashes are deliberately excluded"


def test_an_ascii_hyphen_still_does_not_match_the_pages_em_dash_after_the_fold():
    """The end-to-end twin for the table-content pin above, re-run explicitly for this change: the
    fold must not have quietly widened the existing adversarial case at the top of this module
    (`test_a_quote_that_is_not_on_the_page_still_fails`, 'an ASCII hyphen is not the page's em
    dash')."""
    v = _verify_quote("Payments — internal MVP ready", "Payments - internal MVP ready")
    assert v["citation_problems"]
    assert v["verdict"] != "verified"


# ── specificity: NFKC-style COMPATIBILITY folding must NOT happen ────────────────────────────────
# This is the laundering the long comment above `_FOLD` names by name: NFKC maps `①` onto `1` and
# `ﬁ` onto `fi`, so a quote folded that way could claim a digit or a letter sequence the page never
# wrote. `_fold` uses NFC (canonical equivalence only) — nothing here tested that boundary before.
@pytest.mark.parametrize("body,quote,why", [
    ("The report reached 1 million users.", "The report reached ① million users.",
     "NFKC would fold circled '①' onto '1' — a quote could then claim a digit the page never wrote"),
    ("This is the final report.", "This is the ﬁnal report.",
     "NFKC would fold the 'fi' ligature onto 'fi' — compatibility folding, deliberately not used"),
])
def test_nfkc_style_compatibility_folding_does_not_happen(body, quote, why):
    v = _verify_quote(body, quote)
    assert v["citation_problems"], why


# ── ORDER regression: `_fold` must run AFTER the derender, never before ──────────────────────────
# `…` -> `...` LENGTHENS a string by 2 characters, and every derender pattern above is bounded at
# `_SPAN` (200). Folding FIRST therefore pushes a marker span that is exactly `_SPAN` raw
# characters — a legal, in-bounds span — to `_SPAN + 2` once folded, past the bound, so the
# derender's own regex stops matching it. What that means differs by which construct it happens
# to: for `_STRIKE` (page-only, and the one member of this set where "drop it" is a SECURITY rule —
# the page RETRACTING a value) it is a LOOSENING: the retraction is no longer dropped, and a quote
# of the retracted text verifies. For the payload-free `_PAIRED` set (applied on BOTH sides) it is
# a TIGHTENING instead: the delimiter characters stay literally in the normalized text, breaking
# the contiguity a reader-form quote relies on, so a TRUE quote wrongly fails to verify — the
# "crying wolf" failure this whole derender layer exists to prevent (see the module-level comment
# above `_PAIRED`). Both are pinned below because they demonstrate the ORDER property in the two
# different directions it can break, over two independently-reachable constructs.
#
# `_WIKILINK`/`_MDLINK` are NOT given a third mirror here: both are page-only, and unlike a struck
# span their "drop" is reader-equivalence, not a security rule — when their regex fails to match,
# the raw markup (brackets, target, url) stays in the text, but the reader-visible LABEL remains a
# CONTIGUOUS run of characters inside it either way, so a bare-label quote still matches by
# substring regardless of order. The only way to observe a difference would be a quote spanning
# ACROSS the link into surrounding prose — the identical contiguity-breaking mechanism `_PAIRED`
# already demonstrates below, over a construct that is reachable on both the page AND the quote
# side. A dedicated mirror for the link forms would assert the same order property a third time
# through more setup, not a materially different one.
def test_a_struck_span_at_the_span_boundary_with_an_ellipsis_still_drops_the_retraction():
    """THE audit's defect, pinned directly. OLD (buggy) behaviour: `_fold` ran BEFORE the
    derender, so a struck span of exactly `_SPAN` (200) raw characters containing one `…` expanded
    to `_SPAN + 2` before `_STRIKE` ever saw it — past the bound, so the tildes were left
    un-stripped and the RETRACTED text stayed live in the normalized page; a short quote of it
    (well under `Citation.quote`'s own 200-char cap — the PAGE side has no such limit, only the
    quote does) VERIFIED. The fix (`_fold` now runs LAST) keeps `_STRIKE` operating on the raw,
    unfolded byte count, so the retraction is dropped exactly as it would be without the ellipsis
    at all."""
    unit = "the CEO resigned over fraud "
    content = (unit * 8)[:196] + "abc…"                      # exactly _SPAN (200) raw characters
    assert len(content) == 200 and content.count("…") == 1
    body = "Margin was ~~" + content + "~~ 14%."
    # a short TAIL of the retracted span — what a model would actually quote (<=200 chars), reader
    # form (plain dots): the ellipsis sits right where the bug's +2-character expansion happens.
    retracted_quote = content[-30:].replace("…", "...")
    assert len(retracted_quote) <= 200

    v = _verify_quote(body, retracted_quote)
    assert v["citation_problems"], "the retracted span must not be quotable as current"
    assert v["verdict"] != "verified"

    # Contrast, not a second order-dependent case: the BYTE-IDENTICAL span WITHOUT an ellipsis (a
    # 4-letter tail of the same length) never crosses the bound under either ordering, and was —
    # even under the old bug — always correctly dropped. This is what identifies the defect as an
    # ORDERING bug (folding interacting with a length bound), not a `_SPAN`-arithmetic one.
    content_no_ellipsis = (unit * 8)[:196] + "abcd"
    assert len(content_no_ellipsis) == 200
    body_no_ellipsis = "Margin was ~~" + content_no_ellipsis + "~~ 14%."
    v2 = _verify_quote(body_no_ellipsis, content_no_ellipsis[-30:])
    assert v2["citation_problems"]
    assert v2["verdict"] != "verified"


def test_bold_emphasis_at_the_span_boundary_with_an_ellipsis_still_strips_its_markers():
    """The order pin's OTHER direction, over `_PAIRED` (reachable on both the page and the quote
    side, unlike `_STRIKE`): a `**bold**` span of exactly `_SPAN` (200) raw characters containing
    one `…` must still have its `**` markers consumed. Folded-before-derender pushes this span past
    the bound too, but the observable failure is the OPPOSITE of the strikethrough case — a TRUE,
    SHORT, reader-form quote spanning from just before the marker into the content (no asterisks,
    the ellipsis already written as plain dots) wrongly fails to verify, because the un-stripped
    `**` breaks the contiguous run the substring check needs. A regression here would go red even
    if a future fix mistakenly special-cased `_STRIKE` alone instead of fixing the shared
    `_normalize_page`/`_normalize_quote` order."""
    unit = "quarterly revenue figures were reviewed "
    content = ("abc…" + unit * 8)[:200]                       # exactly _SPAN (200), ellipsis at the START
    assert len(content) == 200 and content.count("…") == 1
    body = "Summary: **" + content + "** end of note."
    # spans the marker boundary itself: "Summary: " (outside **) straight into the folded content
    # (inside **) — only contiguous in the normalized page if the ** were actually stripped.
    quote = "Summary: " + content[:20].replace("…", "...")
    assert len(quote) <= 200

    v = _verify_quote(body, quote)
    assert v["citation_problems"] == [], "a true reader-form quote must not be flagged"
    assert v["verdict"] == "verified"


def test_the_audit_column_gets_a_citation_COUNT_not_the_model_authored_paths():
    """OLD BEHAVIOUR: `audit_summary` wrote `[c["path"] for c in citations]`, so up to
    `MAX_CITATIONS` model-authored strings landed in `audit_log.result` — the column whose whole
    contract is that it carries no transcript.

    Bounding `Citation.path` narrows that channel; it does not close it. Nothing checks that the
    value is a path the run actually READ — an unresolvable citation is a citation *problem*, not a
    dropped citation, so it is logged either way — and a steered model needs no attacker's help to
    fill the field with prose. A count closes it and costs nothing that is read:
    `admin.measurements.answer_shape` tests `r.get("citations")` for TRUTH ("did this answer cite
    anything"), which `0`/`n` answers exactly as `[]`/`[…]` did. The same reduction
    `_verdict_shape` already makes one field over, for the same reason."""
    from stigmergy.answer.service import audit_summary
    smuggled = "SMUGGLED-TRANSCRIPT-4a91 the answer is that revenue was 512000"
    result = {"refused": False, "suppressed": False, "retried": False,
              "verdict": {"verdict": "verified"}, "first_verdict": None,
              "citations": [{"path": smuggled, "quote": "q"},
                            {"path": "wiki/customers/initech.md", "quote": "q"}]}

    summary = audit_summary(result)

    assert smuggled not in str(summary)
    assert summary["citations"] == 2
    # The benign twin, and the property `admin.measurements` actually depends on: the truthiness
    # that separates "answered with a citation" from "answered with none" is unchanged.
    assert bool(summary["citations"]) is True
    assert bool(audit_summary({**result, "citations": []})["citations"]) is False
