from pathlib import Path

import pytest

from stigmergy.index import corpus
from stigmergy.index.errors import StigmergyIndexError

FIXTURE = str(Path(__file__).parent / "fixtures" / "repo")


_FOLDERS = {"note": "notes", "concept": "concepts", "entity": "entities", "project": "projects"}


def _wiki_text(*, role="note", title="Example", status="developing", body="Useful body",
               entity_type="organization", aliases=()):
    path = f"wiki/{_FOLDERS[role]}/{title}.md"
    entity_fields = ""
    if role == "entity":
        alias_block = "".join(f"- {alias}\n" for alias in aliases) if aliases else ""
        entity_fields = f"entity_type: {entity_type}\naliases:{' []' if not aliases else ''}\n{alias_block}"
    text = (
        "---\n"
        "id: page_00000000-0000-4000-8000-000000000001\n"
        f"type: {role}\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "created: '2026-01-01'\n"
        "updated: '2026-01-02'\n"
        "acl: null\n"
        "sources: []\n"
        f"{entity_fields}"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )
    return path, text


def test_loads_only_canonical_searchable_roles():
    rows = corpus.load_pages(FIXTURE)
    assert len(rows) == 12
    assert {row.type for row in rows} == {"note", "concept", "entity", "source"}
    assert {row.path for row in rows if row.type == "entity"} == {
        "wiki/entities/Globex.md",
        "wiki/entities/Initech.md",
    }
    assert not any(row.path.startswith(("ops/", "meta/", "datasets/")) for row in rows)


def test_page_row_preserves_canonical_metadata():
    path, text = _wiki_text(body="Body with [[Support refunds]].")
    row = corpus.page_row(path, "wiki", text)
    assert row.page_id == "page_00000000-0000-4000-8000-000000000001"
    assert row.type == "note"
    assert row.status == "developing"
    assert row.updated == "2026-01-02"
    assert row.acl is None
    assert row.links == ["support refunds"]
    assert row.entity_type == "" and row.aliases == []
    assert row.content_hash.startswith("sha256:")


def test_entity_pages_carry_their_type_and_deduplicated_aliases():
    path, text = _wiki_text(
        role="entity", title="Globex", aliases=("Globex Corporation", " GLBX ", "Globex Corporation")
    )
    row = corpus.page_row(path, "wiki", text)
    assert row.type == "entity"
    assert row.entity_type == "organization"
    assert row.aliases == ["Globex Corporation", "GLBX"]


def test_a_projects_folder_page_is_indexed_as_a_project():
    path, text = _wiki_text(role="project", title="Initech seed round", body="## Goal\n\nRaise the seed round.")
    row = corpus.page_row(path, "wiki", text)
    assert (row.type, row.entity_type, row.aliases) == ("project", "", [])


def test_entity_fields_are_ignored_outside_entity_pages():
    path, text = _wiki_text()
    text = text.replace("sources: []\n", "sources: []\nentity_type: organization\naliases:\n- Alias\n")
    row = corpus.page_row(path, "wiki", text)
    assert row.entity_type == "" and row.aliases == []


def test_an_entities_folder_page_must_declare_the_entity_type():
    path, text = _wiki_text(role="entity", title="Globex")
    with pytest.raises(corpus.CorpusContractError, match="must have type entity"):
        corpus.page_row(path, "wiki", text.replace("type: entity", "type: note"))


def test_a_layer_folder_carries_its_audience_and_its_virtual_path():
    path, text = _wiki_text()
    text = text.replace("acl: null", "acl:\n- finance")
    row = corpus.page_row(f"layers/finance/{path}", "wiki", text)
    assert (row.acl, row.vpath, row.path) == (["finance"], path, f"layers/finance/{path}")


def test_a_page_whose_audience_disagrees_with_its_folder_fails_closed():
    path, text = _wiki_text()
    with pytest.raises(corpus.CorpusContractError, match="layer's audience"):
        corpus.page_row(path, "wiki", text.replace("acl: null", "acl:\n- finance"))
    with pytest.raises(corpus.CorpusContractError, match="layer's audience"):
        corpus.page_row(f"layers/finance/{path}", "wiki", text)
    with pytest.raises(corpus.CorpusContractError, match="layer's audience"):
        corpus.page_row(f"layers/legal/{path}", "wiki", text.replace("acl: null", "acl:\n- finance"))


@pytest.mark.parametrize("acl", ["[]", "finance", "[finance, 2]", "false"])
def test_invalid_acl_fails_closed(acl):
    path, text = _wiki_text()
    text = text.replace("acl: null", f"acl: {acl}")
    with pytest.raises(corpus.CorpusContractError, match="acl"):
        corpus.page_row(path, "wiki", text)


def test_corpus_contract_error_is_an_index_domain_error_not_a_generic_value_error():
    path, text = _wiki_text()
    text = text.replace("acl: null", "acl: []")

    with pytest.raises(StigmergyIndexError) as exc_info:
        corpus.page_row(path, "wiki", text)

    assert isinstance(exc_info.value, corpus.CorpusContractError)
    assert not isinstance(exc_info.value, ValueError)


@pytest.mark.parametrize("role", ["meeting", "document", "page", "view", "raw"])
def test_removed_roles_are_rejected(role):
    path, text = _wiki_text()
    text = text.replace("type: note", f"type: {role}")
    with pytest.raises(corpus.CorpusContractError, match="must have type note"):
        corpus.page_row(path, "wiki", text)


def test_noncanonical_paths_are_not_indexable():
    assert corpus.is_indexable_page("wiki/entities/Globex.md")
    assert not corpus.is_indexable_page("wiki/entities/nested/Globex.md")
    assert not corpus.is_indexable_page("wiki/playbooks/Support.md")
    assert not corpus.is_indexable_page("sources/general/input.md")
    assert corpus.is_indexable_page("wiki/notes/Support.md")
    assert corpus.is_indexable_page(
        "sources/2026/08/00000000-0000-4000-8000-000000000001.md"
    )


def test_link_graph_resolves_paths_and_counts_inlinks():
    rows = {row.path: row for row in corpus.load_pages(FIXTURE)}
    refund = rows["wiki/notes/Refund policy.md"]
    support = rows["wiki/concepts/Support refunds.md"]
    assert refund.links == ["wiki/concepts/Support refunds.md"]
    assert support.inlinks == 1
    renewal = rows["layers/finance/wiki/notes/Globex and Initech renewal.md"]
    assert renewal.vpath == "wiki/notes/Globex and Initech renewal.md"
    assert renewal.links == ["wiki/entities/Globex.md", "wiki/entities/Initech.md"]
    assert rows["wiki/entities/Globex.md"].inlinks == 1
    assert rows["wiki/entities/Globex.md"].links == []


def test_links_inside_code_are_not_edges():
    assert corpus.link_targets("`[[inline]]`\n```md\n[[fenced]]\n```\n[[real]]") == ["real"]


def test_dotted_link_names_are_not_truncated():
    assert corpus.link_targets("[[Booking.com]] [[Guide.md|guide]]") == ["booking.com", "guide"]


def test_frontmatter_parser_accepts_bom_and_crlf():
    metadata, body, malformed = corpus.split_frontmatter_checked(
        "\ufeff---\r\ntitle: Example\r\n---\r\nbody\r\n"
    )
    assert metadata == {"title": "Example"}
    assert body == "body\r\n"
    assert malformed is False


def test_unreadable_acl_frontmatter_is_marked_malformed():
    metadata, _body, malformed = corpus.split_frontmatter_checked(
        "---\nacl: [finance\n\n# Body\n"
    )
    assert metadata == {}
    assert malformed is True


def test_plain_markdown_is_not_misclassified_as_malformed_frontmatter():
    metadata, body, malformed = corpus.split_frontmatter_checked("# Body\n\nacl: is prose\n")
    assert metadata == {}
    assert body.startswith("# Body")
    assert malformed is False


def test_content_hash_changes_with_searchable_content():
    path, first = _wiki_text(body="First")
    _, second = _wiki_text(body="Second")
    assert corpus.page_row(path, "wiki", first).content_hash != corpus.page_row(
        path, "wiki", second
    ).content_hash


def test_iso_day_reads_dates_and_timestamps_and_nothing_else():
    import datetime as dt

    from stigmergy.index.corpus import iso_day

    assert iso_day("2026-09-21T08:18:01.843760+00:00") == "2026-09-21"
    assert iso_day("2026-03-01") == "2026-03-01"
    assert iso_day(dt.date(2026, 3, 1)) == "2026-03-01"
    assert iso_day(dt.datetime(2026, 3, 1, 23, 59)) == "2026-03-01"
    for value in (None, "", "yesterday", "2026-13-01", "2026-3-1", 20260301):
        assert iso_day(value) == ""


def test_attach_source_dates_uses_the_batch_first_then_the_known_dates():
    from stigmergy.index.corpus import PageRow, attach_source_dates

    source = PageRow(path="sources/a.md", vpath="sources/a.md", zone="sources", page_id="a", title="A",
                     body="b", type="source", source_date="2026-05-02", source_dates=["2026-05-02"])
    page = PageRow(path="wiki/notes/N.md", vpath="wiki/notes/N.md", zone="wiki", page_id="n", title="N",
                   body="b", type="note",
                   sources=["sources/a.md", "sources/b.md", "sources/missing.md", "sources/a.md"])
    attach_source_dates([source, page], {("public", "sources/a.md"): "1999-01-01",
                                         ("public", "sources/b.md"): "2026-01-09"})
    assert page.source_dates == ["2026-01-09", "2026-05-02"]
    assert source.source_dates == ["2026-05-02"]


def test_a_page_never_takes_the_date_of_a_source_outside_its_own_layer_and_public():
    from stigmergy.index.corpus import PageRow, attach_source_dates

    hidden = PageRow(path="layers/finance/sources/a.md", vpath="sources/a.md", zone="sources",
                     page_id="a", title="A", body="b", type="source", source_date="2026-05-02")
    public = PageRow(path="wiki/notes/N.md", vpath="wiki/notes/N.md", zone="wiki", page_id="n",
                     title="N", body="b", type="note", sources=["sources/a.md", "sources/b.md"])
    finance = PageRow(path="layers/finance/wiki/notes/F.md", vpath="wiki/notes/F.md", zone="wiki",
                      page_id="f", title="F", body="b", type="note", sources=["sources/a.md", "sources/b.md"])
    attach_source_dates([hidden, public, finance], {("finance", "sources/b.md"): "2026-06-01"})
    assert public.source_dates == []
    assert finance.source_dates == ["2026-05-02", "2026-06-01"]
