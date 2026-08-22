"""A model never reads what the page it is writing could not cite.

ADR 045 D3. The upward link — an open page carrying a restricted page's title — is not something a
human did: the librarian's agent searched the brain unrestricted, found the page and wrote its
title. So the correction is on the INPUT side. Every page a model sees while writing a page at
label L satisfies `flows_into(page.acl, L)`, and the wikilink vocabulary it is offered comes off
the same filtered rows.

Two rejected alternatives, both of which treat the symptom on the way out: narrowing the page
afterwards punishes the human's capture for the model's retrieval, and demoting the link to plain
text leaves the title, which IS the leak.

Real filesystem, real parse; no model and no Postgres — `gather` is a pure function of a checkout.
"""
import os
import subprocess

import pytest

from stigmergy.librarian import gather
from stigmergy.librarian.pydantic_backend import FilingToolbox

OPEN_PAGE = "wiki/notes/open-note.md"
LEADERSHIP_PAGE = "wiki/notes/board-terms.md"
FINANCE_PAGE = "wiki/notes/payroll-bands.md"


def _write(root: str, rel: str, acl: str | None, body: str) -> None:
    full = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    lines = ["---", "type: note", f'title: "{rel.rsplit("/", 1)[-1][:-3]}"', "tags: [note]",
             "status: developing", "as_of: 2026-08-22", "entity: []"]
    if acl is not None:
        lines.append(f"acl: {acl}")
    lines += ["---", "", f"# {rel.rsplit('/', 1)[-1][:-3]}", "", body, ""]
    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


@pytest.fixture()
def checkout(tmp_path):
    """Three pages, one per audience, all naming the same rare word so a single query ranks them
    together — the filter is what must separate them, never the scorer."""
    root = str(tmp_path / "repo")
    os.makedirs(root)
    # A real repo: `FilingToolbox` asks `gitcmd.tracked_paths` at construction, so that this run
    # cannot count a page it just wrote as one that already existed.
    subprocess.run(["git", "init", "--quiet", root], check=True)
    _write(root, OPEN_PAGE, None, "The quarterly zephyrine review is open knowledge.")
    _write(root, LEADERSHIP_PAGE, '["leadership"]', "Zephyrine board terms, restricted.")
    _write(root, FINANCE_PAGE, '["finance"]', "Zephyrine payroll bands, restricted.")
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "--quiet", "-m", "fixture"], check=True)
    return root


def _paths(rows) -> set[str]:
    return {row.path for row in rows}


# ── the corpus a model is given ───────────────────────────────────────────────────────────────
def test_writing_an_open_page_the_model_sees_only_open_pages(checkout):
    """The default, and the case that runs on almost every capture: open in, open out."""
    assert _paths(gather.load_corpus(checkout).rows) == {OPEN_PAGE}


def test_writing_at_a_group_the_model_sees_open_pages_and_that_group(checkout):
    corpus = gather.load_corpus(checkout, acl=["leadership"])
    assert _paths(corpus.rows) == {OPEN_PAGE, LEADERSHIP_PAGE}


def test_one_group_never_sees_another(checkout):
    """`flows_into` is containment, not intersection: sharing no label means seeing nothing of
    it, and sharing SOME label would still not be enough."""
    assert FINANCE_PAGE not in _paths(gather.load_corpus(checkout, acl=["leadership"]).rows)


def test_the_default_is_the_NARROW_one(checkout):
    """A caller that forgets to pass the capture's audience starves the model rather than widening
    it. Asserted against a LABELLED audience rather than against `acl=None`, which is the
    default's own value and would make this true whatever the code did: the omitted-argument
    corpus must be a strict subset of what a labelled capture sees.

    Note which direction "wider" runs. `flows_into` is containment, so a capture at
    `["leadership", "finance"]` sees LESS than one at `["leadership"]`: a page readable by both
    groups may only carry material both may read. The narrowest audience is the one naming the
    most groups, and the widest single audience in this fixture is one group."""
    omitted = _paths(gather.load_corpus(checkout).rows)
    labelled = _paths(gather.load_corpus(checkout, acl=["leadership"]).rows)
    assert omitted < labelled, (omitted, labelled)
    assert omitted == {OPEN_PAGE}


def test_naming_MORE_groups_sees_LESS_not_more(checkout):
    """The direction `flows_into` runs, pinned where a reader would guess wrong. A capture for
    both `leadership` and `finance` may cite only material both audiences may read — so it sees
    the open page and neither labelled one, while a capture for `leadership` alone sees two."""
    both = _paths(gather.load_corpus(checkout, acl=["leadership", "finance"]).rows)
    one = _paths(gather.load_corpus(checkout, acl=["leadership"]).rows)
    assert both == {OPEN_PAGE}
    assert both < one


# ── what the model is told it may link to ─────────────────────────────────────────────────────
def test_the_wikilink_vocabulary_is_scoped_too(checkout):
    """The names are the other half of the leak: a model that cannot READ a page but is handed its
    NAME can still write `[[board-terms]]` into an open note, and the title is what leaks."""
    assert gather.load_corpus(checkout).link_names == ("open-note",)
    assert set(gather.load_corpus(checkout, acl=["leadership"]).link_names) == {
        "open-note", "board-terms"}


def test_the_gathered_seed_offers_only_scoped_names(checkout):
    """`gather.gather` is the block a tool-less backend is handed as everything it gets."""
    gathered = gather.gather(checkout, None, "zephyrine", top_k=10, excerpt_lines=2)
    assert set(gathered.link_names) == {"open-note"}
    assert {c.path for c in gathered.candidates} <= {OPEN_PAGE}


# ── the tools, which reach past the seed ──────────────────────────────────────────────────────
def test_search_pages_cannot_surface_a_page_out_of_scope(checkout):
    box = FilingToolbox(checkout, top_k=10, excerpt_lines=2)
    found = box.search_pages("zephyrine")
    assert [m["path"] for m in found["matches"]] == [OPEN_PAGE]


def test_read_page_refuses_a_path_the_model_names_directly(checkout):
    """The tool reaches the FILESYSTEM, not the parsed rows, so a path the model guesses — or
    reads off a wikilink in the material it was given — has to meet the same scope its searches
    do."""
    box = FilingToolbox(checkout, top_k=10, excerpt_lines=2)
    assert "refused" in box.read_page(LEADERSHIP_PAGE)


def test_the_refusal_is_the_SAME_sentence_as_a_page_that_does_not_exist(checkout):
    """Or the tool is an existence oracle for a model that will happily report what it found."""
    box = FilingToolbox(checkout, top_k=10, excerpt_lines=2)
    assert (box.read_page(LEADERSHIP_PAGE)["refused"]
            == box.read_page("wiki/notes/no-such-page.md")["refused"])


def test_the_benign_twin_a_page_IN_scope_still_reads_in_full(checkout):
    """The specificity half. Every rule above bounces something; this is the one that must not."""
    box = FilingToolbox(checkout, top_k=10, excerpt_lines=2, acl=["leadership"])
    payload = box.read_page(LEADERSHIP_PAGE)
    assert "refused" not in payload
    assert "board terms" in str(payload).lower()


def test_a_template_is_readable_whatever_the_audience(checkout):
    """A template is not a corpus page and carries no audience — the run writes a page's own
    container, so scoping it would break every capture rather than protect anything."""
    os.makedirs(os.path.join(checkout, "ops", "templates"), exist_ok=True)
    with open(os.path.join(checkout, "ops", "templates", "note.md"), "w", encoding="utf-8") as f:
        f.write("---\ntype: note\n---\n\n# <Title>\n")
    box = FilingToolbox(checkout, top_k=10, excerpt_lines=2)
    assert "refused" not in box.read_page("ops/templates/note.md")


def test_list_page_names_offers_only_what_this_run_may_cite(checkout):
    box = FilingToolbox(checkout, top_k=10, excerpt_lines=2, acl=["finance"])
    assert set(box.list_page_names()["names"]) == {"open-note", "payroll-bands"}
