"""`list_pages`: every visible page of a kind, a period, or a connection, fused by virtual path,
paged without loss, dated by the sources it cites, and answering identically whether or not content
exists in layers the reader cannot read (spec retrieval-and-curation §3.1, BQ-01..BQ-03)."""
import base64
import json
import os

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.service import LIST_PAGE_SIZE, BrainService
from stigmergy.server.settings import Settings
from tests.roster import seed_roster
from tests.server.conftest import connect_or_skip, write_page

ENG = "eng@example.com"
ADMIN = "admin@example.com"
IDENTITIES = {
    ENG: {"display_name": "Engineer", "groups": ["eng"], "default_audience": ["eng"]},
    ADMIN: {"display_name": "Admin", "groups": ["brain-admins"], "default_audience": None},
    "finance@example.com": {"display_name": "Finance", "groups": ["finance"],
                            "default_audience": ["finance"]},
}

S_MAR = "sources/2026/03/10000000-0000-4000-8000-000000000001.md"
S_JUN = "sources/2026/06/10000000-0000-4000-8000-000000000002.md"
S_SEP = "sources/2026/09/10000000-0000-4000-8000-000000000003.md"
S_ENG = "sources/2026/04/10000000-0000-4000-8000-000000000004.md"
S_FIN = "sources/2026/03/10000000-0000-4000-8000-000000000005.md"
ACME = "wiki/entities/Acme Corp.md"
DANA = "wiki/entities/Dana (Acme).md"
VAULT = "wiki/entities/Vault Corp.md"
KICKOFF = "wiki/notes/Acme Kickoff.md"
REVIEW = "wiki/notes/Acme Review.md"
ENG_NOTE = "wiki/notes/Acme Integration.md"
FIN_NOTE = "wiki/notes/Acme Finance.md"
PRICING = "wiki/concepts/Pricing.md"
STRAY = "wiki/notes/Stray citation.md"
FILLERS = [f"wiki/notes/Filler {index:02d}.md" for index in range(60)]


def _write_repo(repo: str, *, hidden: bool) -> None:
    write_page(repo, S_MAR, {"title": "March call", "occurred_at": "2026-03-10"}, "Kickoff call.")
    write_page(repo, S_JUN, {"title": "June memo"}, "June memo.")
    write_page(repo, S_SEP, {"title": "September review", "occurred_at": "2026-09-05T10:00:00+00:00"},
               "September review.")
    write_page(repo, S_ENG, {"title": "Integration notes", "acl": ["eng"], "occurred_at": "2026-04-02"},
               "Integration notes.")
    write_page(repo, ACME, {"title": "Acme Corp", "aliases": ["Acme"], "sources": [S_MAR]},
               "Acme Corp is a customer organization.")
    write_page(repo, DANA, {"title": "Dana (Acme)", "entity_type": "person", "sources": [S_SEP]},
               "Dana leads purchasing at [[Acme Corp]].")
    write_page(repo, KICKOFF, {"title": "Acme Kickoff", "updated": "2026-09-30", "sources": [S_MAR]},
               "The kickoff with [[Acme Corp]] agreed a pilot.")
    write_page(repo, REVIEW, {"title": "Acme Review", "sources": [S_JUN, S_SEP]},
               "The review with [[Acme Corp]] and [[Dana (Acme)]] extended the pilot.")
    write_page(repo, ENG_NOTE, {"title": "Acme Integration", "acl": ["eng"], "sources": [S_ENG]},
               "The integration for [[Acme Corp]] uses the public API.")
    write_page(repo, PRICING, {"title": "Pricing"}, "Pricing is per seat.")
    # Written outside the writer, which never lets a public page cite a restricted source: its
    # citation of the finance-only source must date nothing in either repository.
    write_page(repo, STRAY, {"title": "Stray citation", "sources": [S_FIN]}, "A stray citation.")
    for index, path in enumerate(FILLERS):
        write_page(repo, path, {"title": f"Filler {index:02d}", "sources": [S_JUN] if index % 2 else []},
                   f"Filler page number {index}.")
    if hidden:
        write_page(repo, S_FIN, {"title": "Finance ledger", "acl": ["finance"], "occurred_at": "2026-03-15"},
                   "Ledger.")
        write_page(repo, VAULT, {"title": "Vault Corp", "aliases": ["Vault"], "acl": ["finance"],
                                 "sources": [S_FIN]}, "Vault Corp is a counterparty.")
        write_page(repo, FIN_NOTE, {"title": "Acme Finance", "acl": ["finance"], "sources": [S_FIN]},
                   "The [[Acme Corp]] invoice and [[Vault Corp]] escrow.")
        # The finance layer's own part of the public entity page, with its own spelling and type:
        # fused for finance readers only.
        write_page(repo, ACME, {"title": "Acme Corporation", "aliases": ["Acme Holdings"], "acl": ["finance"],
                                "entity_type": "product", "sources": [S_FIN]},
                   "Acme Corp owes the 2026 invoice.")


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    root = tmp_path_factory.mktemp("list-pages")
    plain, hidden = os.path.join(root, "plain"), os.path.join(root, "hidden")
    _write_repo(plain, hidden=False)
    _write_repo(hidden, hidden=True)
    return plain, hidden


@pytest.fixture(scope="module")
def conn(repos):
    connection = connect_or_skip()
    seed_roster(connection, IDENTITIES)
    with connection.cursor() as cur:
        cur.execute("UPDATE roster_groups SET description = 'Engineering work.' WHERE name = 'eng'")
        cur.execute("UPDATE roster_groups SET description = 'Money matters.' WHERE name = 'finance'")
    build.rebuild(connection, repos[1], build_embedder("fake"))
    yield connection
    connection.close()


def _service(conn, subject):
    audiences = None if subject == ADMIN else {"eng"}
    return BrainService(Settings(identity=subject), conn, build_embedder("fake"), audiences,
                        identity=subject)


def _all(service, **filters) -> tuple[list[dict], list[dict]]:
    """Every row across the cursor pages, and each page response."""
    rows, responses, cursor = [], [], None
    while True:
        response = service.list_pages(**filters, cursor=cursor)
        responses.append(response)
        rows.extend(response["pages"])
        cursor = response["next"]
        if cursor is None:
            return rows, responses


def test_every_visible_page_is_listed_once_across_cursor_pages(conn):
    rows, responses = _all(_service(conn, ENG))
    paths = [row["path"] for row in rows]
    expected = {S_MAR, S_JUN, S_SEP, S_ENG, ACME, DANA, KICKOFF, REVIEW, ENG_NOTE, PRICING, STRAY,
                *FILLERS}
    assert len(paths) == len(set(paths))
    assert set(paths) == expected
    assert len(responses) == 2 and len(responses[0]["pages"]) == LIST_PAGE_SIZE
    assert all(response["total"] == len(expected) for response in responses)


def test_rows_carry_catalog_fields_and_fused_source_dates(conn):
    rows = {row["path"]: row for row in _all(_service(conn, ENG))[0]}
    review = rows[REVIEW]
    assert review == {
        "path": REVIEW, "title": "Acme Review", "type": "note", "layers": ["public"],
        "summary": "The review with Acme Corp and Dana (Acme) extended the pilot.",
        "first_source": "2026-06-01", "last_source": "2026-09-05", "sources": 2,
    }
    assert rows[DANA]["entity_type"] == "person" and rows[DANA]["aliases"] == []
    assert rows[S_SEP]["first_source"] == rows[S_SEP]["last_source"] == "2026-09-05"
    assert rows[S_SEP]["sources"] == 0
    assert rows[PRICING]["first_source"] == "" and rows[PRICING]["sources"] == 0
    assert rows[STRAY]["first_source"] == "" and rows[STRAY]["sources"] == 0
    assert "entity_type" not in rows[REVIEW]


def test_the_response_names_the_readers_layers_with_their_descriptions(conn):
    layers = _service(conn, ENG).list_pages()["layers"]
    assert layers == [
        {"name": "public", "description": "Organization-wide knowledge every identity reads."},
        {"name": "eng", "description": "Engineering work."},
    ]
    admin_layers = [item["name"] for item in _service(conn, ADMIN).list_pages()["layers"]]
    assert admin_layers == ["public", "eng", "finance"]


def test_a_period_selects_pages_by_the_dates_of_the_sources_they_cite(conn):
    service = _service(conn, ENG)
    march = {row["path"] for row in _all(service, since="2026-03-01", until="2026-03-31")[0]}
    assert march == {S_MAR, ACME, KICKOFF}
    september = {row["path"] for row in _all(service, since="2026-09-01", until="2026-09-30")[0]}
    assert september == {S_SEP, DANA, REVIEW}
    assert KICKOFF not in september        # edited in September, but its evidence is from March
    admin_march = {row["path"] for row in _all(_service(conn, ADMIN), since="2026-03-01",
                                                until="2026-03-31")[0]}
    assert admin_march == {S_MAR, S_FIN, ACME, KICKOFF, VAULT, FIN_NOTE}


def test_filters_apply_to_the_fused_page_as_its_reader_sees_it(conn):
    admin = _service(conn, ADMIN)
    assert ACME not in {row["path"] for row in _all(admin, entity_type="product")[0]}
    acme = next(row for row in _all(admin, entity_type="organization")[0] if row["path"] == ACME)
    assert acme["title"] == "Acme Corp" and acme["entity_type"] == "organization"
    assert acme["first_source"] == "2026-03-10" and acme["last_source"] == "2026-03-15"


def test_type_entity_type_layer_and_links_to_filters(conn):
    service = _service(conn, ENG)
    assert {row["path"] for row in _all(service, type="entity")[0]} == {ACME, DANA}
    assert [row["path"] for row in _all(service, entity_type="person")[0]] == [DANA]
    assert {row["path"] for row in _all(service, layer="eng")[0]} == {S_ENG, ENG_NOTE}
    linking = {row["path"] for row in _all(service, links_to="Acme")[0]}
    assert linking == {DANA, KICKOFF, REVIEW, ENG_NOTE}
    assert {row["path"] for row in _all(service, links_to=ACME)[0]} == linking
    assert {row["path"] for row in _all(service, links_to="Acme", type="note",
                                         since="2026-09-01")[0]} == {REVIEW}


def test_sorts_order_by_latest_source_oldest_source_and_title(conn):
    service = _service(conn, ENG)
    recent = _all(service)[0]
    lasts = [row["last_source"] for row in recent]
    assert lasts == sorted(lasts, reverse=True)
    oldest = _all(service, sort="oldest")[0]
    firsts = [row["first_source"] or "9999" for row in oldest]
    assert firsts == sorted(firsts)
    titles = [row["title"].lower() for row in _all(service, sort="title")[0]]
    assert titles == sorted(titles)


def test_invalid_arguments_are_refused_with_safe_messages(conn):
    service = _service(conn, ENG)
    for kwargs, message in (
        ({"type": "meeting"}, "type must be one of"),
        ({"entity_type": "robot"}, "entity_type must be one of"),
        ({"type": "note", "entity_type": "person"}, "entity_type applies to entity pages only"),
        ({"since": "September"}, "since must be an ISO date"),
        ({"since": "2026-09-02", "until": "2026-09-01"}, "since must not be after until"),
        ({"sort": "random"}, "sort must be one of"),
        ({"cursor": "not-a-cursor"}, "cursor is not valid"),
    ):
        with pytest.raises(ValueError, match=message):
            service.list_pages(**kwargs)
    cursor = service.list_pages()["next"]
    for changed in ({"sort": "title"}, {"type": "note"}, {"since": "2026-01-01"}):
        with pytest.raises(ValueError, match="cursor is not valid"):
            service.list_pages(**changed, cursor=cursor)
    nested = base64.urlsafe_b64encode(b"[" * 5000).decode()
    with pytest.raises(ValueError, match="cursor is not valid"):
        service.list_pages(cursor=nested)


def test_hidden_and_unknown_names_and_layers_read_alike(conn):
    service = _service(conn, ENG)
    assert service.list_pages(links_to="Vault") == service.list_pages(links_to="Nobody Ltd")
    assert service.list_pages(links_to=VAULT)["total"] == 0
    errors = []
    for layer in ("finance", "nowhere"):
        with pytest.raises(ValueError) as caught:
            service.list_pages(layer=layer)
        errors.append(str(caught.value))
    assert errors[0] == errors[1] == "layer must be one of the layers you may read: public, eng"


def _eng_responses(conn) -> list[str]:
    service = _service(conn, ENG)
    calls = [{}, {"sort": "oldest"}, {"sort": "title"}, {"type": "entity"},
             {"since": "2026-03-01", "until": "2026-03-31"}, {"links_to": "Acme"},
             {"links_to": "Vault"}, {"links_to": "Acme Holdings"}, {"links_to": VAULT},
             {"layer": "public"}]
    responses = []
    for call in calls:
        rows, pages = _all(service, **call)
        responses.append(json.dumps(pages, sort_keys=True))
    for query in ("Acme pilot", "Acme invoice escrow", "stray citation"):
        result = service.search(query, max_results=8)
        result.pop("built_at")
        responses.append(json.dumps(result, sort_keys=True))
    for layer in ("finance", "nowhere"):
        try:
            service.list_pages(layer=layer)
        except ValueError as error:
            responses.append(str(error))
    return responses


def test_responses_are_identical_with_and_without_hidden_layer_content(conn, repos):
    """BQ-02: rows, totals, cursors, resolution failures, and errors."""
    build.rebuild(conn, repos[0], build_embedder("fake"))
    without = _eng_responses(conn)
    build.rebuild(conn, repos[1], build_embedder("fake"))
    with_hidden = _eng_responses(conn)
    assert with_hidden == without


def test_search_states_how_many_pages_it_ranked(conn):
    result = _service(conn, ENG).search("Acme pilot", max_results=3)
    assert result["count"] == 3
    assert result["ranked"] >= result["count"]
    assert f"The 3 most similar of {result['ranked']} ranked pages" in result["note"]


def test_the_backoffice_vocabulary_lists_every_visible_entity_without_bodies(conn):
    vocabulary = _service(conn, ADMIN).entity_vocabulary()
    assert [entity["path"] for entity in vocabulary["entities"]] == [ACME, DANA, VAULT]
    acme = vocabulary["entities"][0]
    assert acme["aliases"] == ["Acme", "Acme Holdings"] and acme["layers"] == ["public", "finance"]
    eng = _service(conn, ENG).entity_vocabulary()
    assert [entity["path"] for entity in eng["entities"]] == [ACME, DANA]
    assert eng["entities"][0]["aliases"] == ["Acme"]


def test_the_answer_agent_reads_listings_and_search_counts_as_fenced_evidence(conn):
    from stigmergy.answer.brain import AnswerBrain

    brain = AnswerBrain(_service(conn, ENG))
    listing = brain.list_text(type="entity", sort="title")
    assert "total: 2 matching page(s); showing 2; no more pages" in listing
    assert f"- {DANA}\n  Dana (Acme) (entity, person · 2026-09-05 · 1 source(s))" in listing
    assert f"- {ACME}\n  Acme Corp (entity, organization · 2026-03-10 · 1 source(s))\n  aliases: Acme" in listing
    first = brain.list_text()
    assert "showing 50; next cursor: " in first
    search = brain.search_text("Acme pilot")
    assert "most similar of" in search and "search is not exhaustive, list_pages is" in search
