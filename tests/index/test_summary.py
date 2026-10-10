"""`rank.summary_of`: one catalog line per page, read from the page's own first prose sentence."""
import pytest

from stigmergy.index.rank import SUMMARY_WIDTH, summary_of


def test_the_first_sentence_after_the_title_is_the_summary():
    body = "# Harbor Fund\n\nHarbor Fund is a seed investor. It led the first round.\n"
    assert summary_of(body) == "Harbor Fund is a seed investor."


def test_prose_under_a_first_section_heading_is_used():
    body = "# Tessera\n\n## What / Who\nTessera is a learning platform for nurses.\n\n## Facts\n- x\n"
    assert summary_of(body) == "Tessera is a learning platform for nurses."


def test_leading_lists_and_tables_are_skipped_for_the_first_prose_paragraph():
    body = ("# Renewal\n\n- a bullet first\n- another\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
            "The renewal closed on 9 April.\n")
    assert summary_of(body) == "The renewal closed on 9 April."


def test_wikilinks_render_as_their_shown_text_and_citations_are_removed():
    body = ("# Kickoff\n\nOn 8 January [[Dana (Initech)|Dana]] met [[Quillstack]] about pricing. "
            "(Source: `sources/2026/01/00000000-0000-4000-8000-000000000001.md`)\n")
    assert summary_of(body) == "On 8 January Dana met Quillstack about pricing."


def test_a_title_abbreviation_does_not_end_the_sentence():
    body = "# Pilot\n\nDr. Mara Lind approved the pilot for 40 vans. The fee was small.\n"
    assert summary_of(body) == "Dr. Mara Lind approved the pilot for 40 vans."


def test_a_long_sentence_is_cut_at_a_word_boundary():
    words = " ".join(["cold-chain"] * 60)
    summary = summary_of(f"# Long\n\n{words}.\n")
    assert len(summary) <= SUMMARY_WIDTH + 1 and summary.endswith("…")
    assert "cold-chai…" not in summary


def test_a_page_with_only_a_list_summarizes_its_first_item():
    body = "# Veltrix\n\n## Facts\n- 2026-03-01: signed the pilot. (Source: `sources/x.md`)\n"
    assert summary_of(body) == "2026-03-01: signed the pilot."


@pytest.mark.parametrize("body", ["", "# Only a title\n", "```\ncode only\n```\n"])
def test_a_page_without_prose_has_an_empty_summary(body):
    assert summary_of(body) == ""


def test_code_fences_and_quotes_are_not_prose():
    body = "# Tool\n\n```\nrun --fast\n```\n\n> quoted aside\n\nThe tool rebuilds the index.\n"
    assert summary_of(body) == "The tool rebuilds the index."


def test_accented_text_and_emphasis_survive_cleanly():
    body = "# Iván Robles\n\n**Iván Robles** leads *operations* at Tessera.\n"
    assert summary_of(body) == "Iván Robles leads operations at Tessera."


@pytest.mark.parametrize("body", [
    "# T\n\n" + "vs. A " * 350_000,
    "# T\n\n" + "`" * 2_000_000,
    "# T\n\n" + "Inc. Co. " * 200_000,
    "# T\n\n" + "[[" * 1_000_000,
    "# T\n\n" + ("word " * 50 + "\n") * 40_000,
])
def test_a_huge_or_hostile_body_is_summarized_in_bounded_time(body):
    import time

    def cost(text):
        started = time.perf_counter()
        summary_of(text)
        return time.perf_counter() - started

    small = min(cost(body[:8_200]) for _ in range(3))
    large = min(cost(body) for _ in range(3))
    assert large < 0.25, "a hostile page must not stall a search"
    assert large < 5 * small + 0.002, "the cost must not grow with the body"


def test_spanish_openers_and_courtesy_titles_split_sentences_correctly():
    body = "# Reunión\n\nLa Sra. Marín aprobó el piloto. ¿Quién paga el mapeo?\n"
    assert summary_of(body) == "La Sra. Marín aprobó el piloto."
