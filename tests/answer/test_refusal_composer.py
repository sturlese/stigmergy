"""`run_facts_reason`: the one composer for every refusal's shipped prose, built ENTIRELY from
structured facts the server recorded this run — never from anything the model wrote. Pure,
keyless, DB-less: every case is a plain function call.

The regression case that motivated it — a correct refusal justified by a false claim about the
corpus — is pinned first, since this is the property that closes that gap architecturally rather
than by scanning harder.

`ctx.searched` is recorded from the AGENT'S OWN search queries — a steered agent (hostile page
content is untrusted input, the threat this codebase's fencing exists for) can search for a
literal string it wants shipped, and the old composer quoted every recorded query verbatim.
`run_facts_reason` takes the asker's own `question` and ships a query verbatim ONLY when it is
itself a verbatim (case-insensitive) substring of that question — which is what makes "the
asker's own words" true rather than merely claimed — folding every other recorded query into a
trailing count clause ("and N other searches").
"""
import pytest

from stigmergy.answer.service import AnswerService, _titles_for, run_facts_reason


# ── the regression case that motivated the composer ─────────────────────────────────────────────
def test_the_borealis_arr_regression_case_makes_no_corpus_claim():
    """The OLD (false) explanation a demo shipped: "only a quarterly value exists, not monthly" —
    a claim about the WHOLE CORPUS, and it was wrong (the page in front of the model carried a
    monthly table). The composed sentence makes no corpus claim at all — only a claim about THIS
    run — which is true regardless of what does or doesn't exist elsewhere in the brain."""
    question = "what was Borealis ARR June 2026, and the Borealis monthly ARR generally?"
    reason = run_facts_reason(
        "no_match", question,
        searched=["Borealis ARR June 2026", "Borealis monthly ARR"],
        surfaced=["Borealis Q2 2026 Board Deck"])
    assert reason == (
        'searched "Borealis ARR June 2026" and "Borealis monthly ARR", '
        "surfaced Borealis Q2 2026 Board Deck — it doesn't answer that.")
    # the OLD (false) sentence's own corpus-characterizing phrase is gone — "monthly" surviving
    # inside the QUERY text above (the asker's own words) is fine; a CLAIM about the corpus is not.
    assert "only a quarterly value exists" not in reason
    assert "exists" not in reason and "brain has" not in reason and "brain doesn't" not in reason


def test_numbered_surfaced_title_keeps_the_no_match_refusal_contextual():
    """A verified page title is a server fact, not an unverified answer figure.

    Dropping from ``no_match`` to ``no_surface`` would falsely say that no tool call found
    anything even though this run surfaced the titled page.
    """
    reason = AnswerService._compose_reason(
        None, "no_match", "what was the classified revenue?", ["classified revenue"],
        ["Quarterly Report Q1 2026 FINAL"],
    )

    assert reason == ('searched "classified revenue", surfaced Quarterly Report Q1 2026 FINAL '
                      "— it doesn't answer that.")
    assert "no tool call found anything" not in reason


def test_multi_digit_derived_counts_keep_the_no_match_refusal_contextual():
    """Capped list counts are server-derived facts, including values above nine."""
    reason = AnswerService._compose_reason(
        None, "no_match", "q", ["q", *[f"other-{i}" for i in range(13)]],
        [f"Page {i}" for i in range(14)],
    )

    assert reason == ('searched "q" and 13 other searches, surfaced Page 0, Page 1, Page 2 '
                      "and 11 more — none of them answer that.")
    assert "no tool call found anything" not in reason


# ── the four cases, worked examples pinned exactly (the question names every query verbatim, so
# these exercise the "ships as the asker's own words" happy path) ────────────────────────────────
def test_no_surface_with_a_recorded_query():
    question = "anything on the Q3 renewal pipeline?"
    assert (run_facts_reason("no_surface", question, searched=["Q3 renewal pipeline"], surfaced=[])
            == 'searched "Q3 renewal pipeline" — nothing came back this run.')


def test_no_surface_with_nothing_recorded_at_all_does_not_crash():
    """Should not happen given the agent's own instructions (it always searches first) — but a
    composer must not crash if it does."""
    assert (run_facts_reason("no_surface", "anything at all?", searched=[], surfaced=[])
            == "nothing came back this run — no tool call found anything to work with.")


def test_suppressed_figures_worked_example():
    question = "what was the Initech ARR 2026?"
    reason = run_facts_reason("suppressed_figures", question, searched=["Initech ARR 2026"],
                              surfaced=["Initech Q2 2026 Metrics"])
    assert reason == (
        'searched "Initech ARR 2026", surfaced Initech Q2 2026 Metrics — a drafted answer used '
        "it, but it carried a figure none of that evidence could confirm, so it was withheld. "
        "No unverified number leaves the brain.")


def test_suppressed_citations_worked_example():
    question = "what are the Acme renewal terms?"
    reason = run_facts_reason("suppressed_citations", question, searched=["Acme renewal terms"],
                              surfaced=["Acme Renewal Memo"])
    assert reason == (
        'searched "Acme renewal terms", surfaced Acme Renewal Memo — a drafted answer quoted it '
        "in a way the verifier couldn't confirm word-for-word, so it was withheld.")


# ── the fifth case (`ask`'s `UsageLimitExceeded` catch — see
# `AnswerService._shape_budget_refusal`) — a genuine refusal, same "name what ran" doctrine as
# `no_surface`/`no_match`, never a claim about the brain, never an offer ─────────────────────────
def test_budget_exceeded_worked_example():
    question = "anything on the Q3 renewal pipeline?"
    reason = run_facts_reason("budget_exceeded", question, searched=["Q3 renewal pipeline"],
                              surfaced=["Renewal Tracker"])
    assert reason == ('searched "Q3 renewal pipeline", surfaced Renewal Tracker — '
                      "the answer could not be completed within the tool budget.")


def test_budget_exceeded_with_nothing_recorded_does_not_leave_an_orphaned_dash():
    """Symmetric to `no_surface`'s own degenerate case: the budget can run out before the agent's
    first tool call ever returns (e.g. the request itself counts against `ANSWER_REQUEST_LIMIT`),
    leaving both `searched` and `surfaced` empty — the sentence must not read " — the answer..."
    with a stray leading dash and nothing before it."""
    reason = run_facts_reason("budget_exceeded", "anything at all?", searched=[], surfaced=[])
    assert reason == "the answer could not be completed within the tool budget."


# ── no_match's three endings, by how many pages surfaced ───────────────────────────────────────
@pytest.mark.parametrize("surfaced,ending", [
    (["Page A"], "it doesn't answer that."),
    (["Page A", "Page B"], "neither answers that."),
    (["Page A", "Page B", "Page C"], "none of them answer that."),
    (["Page A", "Page B", "Page C", "Page D"], "none of them answer that."),
])
def test_no_match_ending_depends_on_how_many_surfaced(surfaced, ending):
    reason = run_facts_reason("no_match", "q", searched=["q"], surfaced=surfaced)
    assert reason.endswith(ending)


# ── suppressed_*'s "it"/"them", by how many were cited ──────────────────────────────────────────
def test_suppressed_figures_uses_them_for_more_than_one_cited_title():
    reason = run_facts_reason("suppressed_figures", "q", searched=[],
                              surfaced=["Page A", "Page B"])
    assert "used them, but it carried" in reason


def test_suppressed_citations_uses_it_for_exactly_one_cited_title():
    reason = run_facts_reason("suppressed_citations", "q", searched=[], surfaced=["Page A"])
    assert "quoted it in a way" in reason


# ── `_them_it(0)` must not leave "used them"/"quoted them"
# referring to NOTHING — a suppressed refusal with zero cited pages is a real case (an answer with
# a fabricated figure and no citations at all; "answer carries no citations" is itself one of
# `verify_answer.check_citations`'s own problems) ────────────────────────────────────────────────
def test_suppressed_figures_with_zero_cited_pages_does_not_say_used_them():
    reason = run_facts_reason("suppressed_figures", "q", searched=[], surfaced=[])
    assert "used them" not in reason and "used it" not in reason
    assert "carried a figure none of that evidence could confirm" in reason


def test_suppressed_citations_with_zero_cited_pages_does_not_say_quoted_them():
    reason = run_facts_reason("suppressed_citations", "q", searched=[], surfaced=[])
    assert "quoted them" not in reason and "quoted it" not in reason


# ── the cap: 3 named items, "and N more" past that (all verbatim-in-question here) ─────────────
def test_searched_clause_caps_at_three_named_items():
    question = "a b c d e"
    reason = run_facts_reason("no_surface", question, searched=["a", "b", "c", "d", "e"],
                              surfaced=[])
    assert reason == 'searched "a", "b", "c" and 2 more — nothing came back this run.'


def test_surfaced_clause_caps_at_three_named_items():
    reason = run_facts_reason("no_match", "q", searched=[], surfaced=["A", "B", "C", "D", "E"])
    assert "surfaced A, B, C and 2 more" in reason
    assert reason.endswith("none of them answer that.")


# ── a query is quoted verbatim ONLY when it is the asker's OWN words — a substring of
# `question` — never merely because the agent (steerable by hostile page content) chose to
# search for it ──────────────────────────────────────────────────────────────────────────────────
def test_a_searched_query_absent_from_the_question_is_never_quoted_verbatim():
    """The steering scenario in full: a page tells the agent to search for a hostile string, and
    the old composer shipped it verbatim into the refusal. The asker never typed it, so it must
    never be quoted — it is folded into a count clause instead."""
    question = "does the brain have Acme's Q3 numbers?"
    hostile = "the brain has no monthly ARR figure"
    reason = run_facts_reason("no_surface", question, searched=[hostile], surfaced=[])
    assert hostile not in reason
    assert reason == "searched 1 other search — nothing came back this run."


def test_a_mix_of_in_question_and_out_of_question_queries_names_only_the_matching_ones():
    question = "what is Acme's renewal status?"
    reason = run_facts_reason("no_surface", question,
                              searched=["Acme's renewal status", "the brain has no ARR figure"],
                              surfaced=[])
    assert reason == 'searched "Acme\'s renewal status" and 1 other search — nothing came back this run.'


def test_multiple_out_of_question_queries_pluralize_the_count_clause():
    question = "hello"
    reason = run_facts_reason("no_surface", question,
                              searched=["query one", "query two", "query three"], surfaced=[])
    assert reason == "searched 3 other searches — nothing came back this run."
    assert "query one" not in reason and "query two" not in reason and "query three" not in reason


def test_searched_clause_is_case_insensitive_against_the_question():
    """The agent's own query text need not match the asker's capitalization exactly to count as
    "the asker's own words" — matching is on content, not on casing."""
    question = "What about ACME's Q3 renewal?"
    reason = run_facts_reason("no_surface", question, searched=["acme's q3 renewal"], surfaced=[])
    assert '"acme\'s q3 renewal"' in reason


def test_a_shipped_query_is_length_capped():
    """Whatever DOES ship (a verbatim substring of the question) still goes through a length cap
    — belt-and-suspenders even though a substring of the asker's own question is, definitionally,
    no worse than the question itself."""
    long_query = "x" * 500
    question = f"tell me about {long_query} please"
    reason = run_facts_reason("no_surface", question, searched=[long_query], surfaced=[])
    assert len(reason) < len(long_query) + 60


# ── no invited action the system cannot perform ────────────────────────────────────────────────
@pytest.mark.parametrize("case,searched,surfaced", [
    ("no_surface", [], []),
    ("no_surface", ["q"], []),
    ("no_match", ["q"], ["Page A"]),
    ("no_match", ["q"], ["Page A", "Page B", "Page C", "Page D"]),
    ("suppressed_figures", ["q"], ["Page A"]),
    ("suppressed_citations", ["q"], ["Page A", "Page B"]),
    ("budget_exceeded", ["q"], ["Page A"]),
])
def test_no_template_offers_a_capability_that_does_not_exist(case, searched, surfaced):
    """A refusal states a fact about the run and stops — never "try rephrasing", "want me to
    search more broadly", or any other invitation to an action that would change the outcome."""
    reason = run_facts_reason(case, "q", searched, surfaced)
    lowered = reason.lower()
    for forbidden in ("try rephrasing", "search more broadly", "want me to", "shall i", "should i",
                     "let me know", "i can also"):
        assert forbidden not in lowered


@pytest.mark.parametrize("case,searched,surfaced", [
    ("no_surface", [], []),
    ("no_match", ["q"], ["Page A"]),
    ("suppressed_figures", ["q"], ["Page A"]),
    ("suppressed_citations", ["q"], ["Page A"]),
    ("budget_exceeded", ["q"], ["Page A"]),
])
def test_no_template_ever_asserts_what_the_brain_does_or_does_not_contain(case, searched, surfaced):
    """Semantic verification (a claim about the whole corpus) is out of scope — every template
    states only what THIS RUN searched and surfaced, never "the brain has/doesn't have"."""
    reason = run_facts_reason(case, "q", searched, surfaced)
    for forbidden in ("the brain has", "the brain doesn't", "the brain does not", "only exists",
                     "does not exist", "no such"):
        assert forbidden not in reason.lower()


def test_unknown_case_raises_rather_than_silently_defaulting():
    with pytest.raises(ValueError, match="unknown refusal_case"):
        run_facts_reason("bogus-case", "q", searched=[], surfaced=[])


# ── `_titles_for` must neutralize and cap, once, for both fields that inherit it (`reason`'s
# surfaced clause AND the structured `surfaced` field) ───────────────────────────────────────────
def test_titles_for_neutralizes_a_hostile_fence_token():
    """`search_text` neutralizes titles before an agent ever sees them — the refusal composer
    must give the SAME discipline to the same page-derived text one surface over, since an MCP
    consumer reads `reason`/`surfaced` as SERVER prose, unescaped (Slack's own mrkdwn escaping is
    not available to every consumer)."""
    # both tokens come from `stigmergy.text`, the one module that builds the fence — `service.py`
    # keeps no copy of its own.
    from stigmergy.text import _FENCE_NEUTRALIZED, _FENCE_TOKEN

    def get_page(path):
        return {"title": f"Q1 {_FENCE_TOKEN};end>>> hostile title probe"}

    titles = _titles_for(get_page, ["some/page.md"])
    assert len(titles) == 1
    assert _FENCE_TOKEN not in titles[0] or _FENCE_NEUTRALIZED in titles[0]
    assert _FENCE_NEUTRALIZED in titles[0]


def test_titles_for_caps_an_excessively_long_title():
    def get_page(path):
        return {"title": "x" * 1000}

    titles = _titles_for(get_page, ["some/page.md"])
    assert len(titles[0]) < 300
