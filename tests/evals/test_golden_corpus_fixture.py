"""`evals/corpus/` — the frozen reference corpus.

The two golden instruments need an API key and so run by hand; this file is the keyless half that
CI can run, and it protects the thing a baseline is only as good as: the instruments and the
corpus agreeing about which pages exist.

Resolution goes through `corpus.load_pages`, the same walk the index builder runs, so these tests
see page ids and `entity` lists exactly as `pages_index` will — not as a second, hand-rolled
parser that can drift from it. The known gap below is what that kind of drift costs.
"""
import json
import re
from pathlib import Path

import pytest

from stigmergy.index import corpus, golden

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "evals" / "corpus"

# `evals/retrieval_golden.json`'s `benchmark: foxglove prospect` lists a second expectation,
# `foxglove-health`, that matches no page id and no file stem — so that question's recall is capped
# at 0.5. It is the drift described above: an earlier preflight fell back to a SUBSTRING match
# against paths, and `foxglove-health` is a path SEGMENT of the pitch-deck page, so it reported
# resolved. Named here so it cannot silently grow a sibling.
KNOWN_UNRESOLVED = {"foxglove-health"}

CORPUS_PAGES = 38


@pytest.fixture(scope="module")
def pages():
    return corpus.load_pages(str(CORPUS))


@pytest.fixture(scope="module")
def retrieval_questions():
    return golden.load_golden(str(ROOT / "evals" / "retrieval_golden.json"))


def test_the_frozen_corpus_is_present_and_the_expected_size(pages):
    assert CORPUS.is_dir(), "evals/corpus/ is the committed reference corpus — do not delete it"
    assert len(pages) == CORPUS_PAGES


def test_every_zone_the_index_walks_is_populated(pages):
    """A corpus missing a whole zone would still 'run', and score zero for that zone's questions
    without ever saying so."""
    assert {p.zone for p in pages} == set(corpus.ZONES)


def test_provenance_records_the_page_count_and_a_source_sha():
    data = json.loads((CORPUS / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", data["source_sha"])
    assert data["pages"] == CORPUS_PAGES


def test_the_root_readme_and_provenance_are_not_indexed_as_pages(pages):
    """Both sit at the corpus root, outside every zone, on purpose — one more page would join the
    candidate pool of every question."""
    assert (CORPUS / "README.md").is_file()
    assert "README.md" not in {p.path for p in pages}


def test_every_qa_citation_resolves_to_a_real_page(pages):
    qa = json.loads((ROOT / "evals" / "qa_golden.json").read_text(encoding="utf-8"))
    paths = {p.path for p in pages}

    missing = []
    for case in qa["questions"]:
        chain = case.get("cites") or []
        if isinstance(chain, str):
            chain = [chain]
        missing += [f"{case['id']} -> {c}" for c in chain if c not in paths]
    assert not missing, f"qa_golden.json cites pages absent from the frozen corpus: {missing}"


def test_every_retrieval_expectation_resolves_apart_from_the_named_known_gap(
        pages, retrieval_questions):
    resolvable = {p.page_id for p in pages} | {Path(p.path).stem for p in pages}
    unresolved = {expected for q in retrieval_questions for expected in q["expect"]
                  if expected not in resolvable}
    assert unresolved == KNOWN_UNRESOLVED


def test_every_filtered_questions_expected_pages_really_carry_that_entity(
        pages, retrieval_questions):
    """The failure this catches is silent and total: a filter naming an entity its own expected
    page does not carry removes that page from BOTH arms, so the question scores 0 forever while
    reading as a retrieval regression. Checks all ten filtered questions, not a sample."""
    by_key = {}
    for p in pages:
        by_key[p.page_id] = p
        by_key.setdefault(Path(p.path).stem, p)

    filtered = [q for q in retrieval_questions if q["filters"]]
    assert len(filtered) == 10

    for q in filtered:
        entity = q["filters"]["entity"]
        for expected in q["expect"]:
            if expected in KNOWN_UNRESOLVED:
                continue
            page = by_key[expected]
            assert entity in page.entity, (
                f"{q['id']} filters on entity={entity!r} but its expected page {page.path} "
                f"declares {page.entity!r} — the filter would delete its own answer")
