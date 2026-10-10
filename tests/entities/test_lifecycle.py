import datetime as dt
from pathlib import Path

import pytest

from stigmergy.capture import schema
from stigmergy.entities.service import (
    EntityOperationError,
    entity_pages,
    find_entity,
    merge_entities,
    name_index,
    rename_entity,
    rewrite_links,
)
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import page_path, read_page, render_page

NOW = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)
LATER = NOW + dt.timedelta(days=1)
SOURCE = "sources/2026/08/00000000-0000-4000-8000-000000000001.md"
SECOND_SOURCE = "sources/2026/08/00000000-0000-4000-8000-000000000002.md"


def _entity(
    root: Path,
    title: str,
    *,
    entity_type: str = "organization",
    aliases: tuple[str, ...] = (),
    acl: tuple[str, ...] | None = None,
    body: str | None = None,
    sources: tuple[str, ...] = (SOURCE,),
) -> str:
    path = page_path("entity", title)
    target = root.joinpath(*path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_page(
            path=path,
            role="entity",
            title=title,
            body=body or f"# {title}\n\n{title} is an identity in the wiki. (Source: `{SOURCE}`)",
            acl=acl,
            sources=sources,
            created=NOW.date(),
            updated=NOW.date(),
            entity_type=entity_type,
            aliases=aliases,
        ),
        encoding="utf-8",
    )
    return path


def _note(root: Path, body: str, *, title: str = "Relationship", acl: tuple[str, ...] | None = None) -> str:
    path = page_path("note", title)
    target = root.joinpath(*path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_page(
            path=path,
            role="note",
            title=title,
            body=f"# {title}\n\n{body}",
            acl=acl,
            sources=(SOURCE,),
            status="mature",
            created=NOW.date(),
            updated=NOW.date(),
        ),
        encoding="utf-8",
    )
    return path


def _source(root: Path, assertion: str, relative: str = SOURCE) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {path.stem}\n"
        "type: source\n"
        "submitted_by: alice\n"
        "acl: null\n"
        "captured_at: 2026-08-24T12:00:00+00:00\n"
        "origin: mcp\n"
        "participants: []\n"
        "artifacts:\n"
        f"  - sha256: {'a' * 64}\n"
        "    bytes: 1\n"
        "    media_type: text/plain\n"
        f"    readable_sha256: {'a' * 64}\n"
        "    extractor: utf8\n"
        "    extractor_version: '1'\n"
        "    ocr_pages: []\n"
        "---\n\n"
        f"# Captured source\n\n{assertion}\n",
        encoding="utf-8",
    )


def _evidence(assertion: str, path: str = SOURCE) -> schema.EntityMergeEvidence:
    return schema.EntityMergeEvidence(
        source_assertions=(schema.SourceMergeAssertion(path=path, assertion=assertion),),
    )


def _snapshot(root: Path) -> dict:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_entity_pages_and_name_index_cover_every_title_and_alias(tmp_path):
    assert entity_pages(str(tmp_path)) == {}
    acme = _entity(tmp_path, "Acme Holdings", aliases=("AH", "Acme"))
    globex = _entity(tmp_path, "Globex", entity_type="product")

    pages = entity_pages(str(tmp_path))

    assert list(pages) == [acme, globex]
    assert pages[acme].aliases == ("AH", "Acme")
    assert pages[globex].entity_type == "product"
    assert name_index(pages) == {
        "acme holdings": (acme,),
        "ah": (acme,),
        "acme": (acme,),
        "globex": (globex,),
    }


def test_find_entity_resolves_a_path_title_or_alias_and_refuses_ambiguity(tmp_path):
    acme = _entity(tmp_path, "Acme Holdings", aliases=("AH",))
    pages = entity_pages(str(tmp_path))

    assert find_entity(pages, acme).path == acme
    assert find_entity(pages, "acme holdings").path == acme
    assert find_entity(pages, "ÁCME  Holdings.").path == acme
    assert find_entity(pages, "ah").path == acme
    assert find_entity(pages, "Globex") is None

    rival = _entity(tmp_path, "Acme Holdings Ltd", aliases=("AH",))
    pages = entity_pages(str(tmp_path))

    assert find_entity(pages, "AH") is None
    assert find_entity(pages, rival).path == rival
    assert set(name_index(pages)["ah"]) == {acme, rival}


def test_merge_folds_absorbed_pages_into_the_first_listed_and_rewrites_links(tmp_path):
    assertion = "Acme One and Acme Two are duplicate company identities."
    _source(tmp_path, assertion)
    _source(tmp_path, "Acme Two ships the second product.", SECOND_SOURCE)
    survivor = _entity(tmp_path, "Acme One", aliases=("A1",))
    absorbed = _entity(
        tmp_path,
        "Acme Two",
        aliases=("A2", "Acme"),
        sources=(SOURCE, SECOND_SOURCE),
        body=f"# Acme Two\n\nAcme Two ships the second product. (Source: `{SECOND_SOURCE}`)",
    )
    note = _note(
        tmp_path,
        f"[[Acme Two]] works with [[Acme One]] and [[Acme Two|the second Acme]]. (Source: `{SOURCE}`)",
    )
    original = read_page(str(tmp_path), survivor)

    canonical = merge_entities(str(tmp_path), (survivor, absorbed), at=LATER, evidence=_evidence(assertion))

    assert canonical == survivor
    assert not tmp_path.joinpath(*absorbed.split("/")).exists()
    assert list(entity_pages(str(tmp_path))) == [survivor]
    page = read_page(str(tmp_path), survivor)
    assert page.page_id == original.page_id
    assert (page.created, page.updated) == (NOW.date(), LATER.date())
    assert page.aliases == ("A1", "Acme Two", "A2", "Acme")
    assert page.sources == (SOURCE, SECOND_SOURCE)
    assert page.body.startswith(original.body)
    assert "## Formerly Acme Two\n\nAcme Two ships the second product." in page.body
    assert "\n# Acme Two" not in page.body
    assert read_page(str(tmp_path), note).body == (
        "# Relationship\n\n[[Acme One|Acme Two]] works with [[Acme One]] and "
        f"[[Acme One|the second Acme]]. (Source: `{SOURCE}`)"
    )
    assert check(str(tmp_path)) == ()


def test_merge_survivor_is_the_first_listed_page(tmp_path):
    assertion = "Acme One and Acme Two are aliases for the same entity."
    _source(tmp_path, assertion)
    first = _entity(tmp_path, "Acme One")
    second = _entity(tmp_path, "Acme Two")

    canonical = merge_entities(str(tmp_path), (second, first), at=LATER, evidence=_evidence(assertion))

    assert canonical == second
    assert list(entity_pages(str(tmp_path))) == [second]
    assert read_page(str(tmp_path), second).aliases == ("Acme One",)


@pytest.mark.parametrize(
    "assertion",
    [
        "AH and Globex are the same organization.",
        "Globex is another name for AH.",
        "Acme Holdings and Globex refer to the same company.",
    ],
)
def test_merge_evidence_binds_pages_through_titles_and_aliases(tmp_path, assertion):
    _source(tmp_path, assertion)
    acme = _entity(tmp_path, "Acme Holdings", aliases=("AH",))
    globex = _entity(tmp_path, "Globex")

    assert merge_entities(str(tmp_path), (acme, globex), at=LATER, evidence=_evidence(assertion)) == acme
    assert read_page(str(tmp_path), acme).aliases == ("AH", "Globex")


@pytest.mark.parametrize(
    ("first_name", "second_name", "assertion"),
    (
        ("Eva", "Other", "Deva and Other are aliases for the same entity."),
        ("Acme", "Globex", "Acme and Globex used aliases during migration."),
        ("Acme", "Acme Inc", "Acme and Acme Inc are duplicate company identities."),
    ),
)
def test_merge_rejects_assertions_that_do_not_bind_every_page_without_mutation(
    tmp_path, first_name, second_name, assertion
):
    _source(tmp_path, assertion)
    first = _entity(tmp_path, first_name)
    second = _entity(tmp_path, second_name)
    _note(tmp_path, f"[[{first_name}]] and [[{second_name}]]. (Source: `{SOURCE}`)")
    before = _snapshot(tmp_path)

    with pytest.raises(EntityOperationError, match="unambiguously equate"):
        merge_entities(str(tmp_path), (first, second), at=LATER, evidence=_evidence(assertion))

    assert _snapshot(tmp_path) == before


def test_merge_rejects_evidence_missing_from_the_cited_source_without_mutation(tmp_path):
    _source(tmp_path, "Acme and Globex signed a contract.")
    first = _entity(tmp_path, "Acme")
    second = _entity(tmp_path, "Globex")
    before = _snapshot(tmp_path)
    assertion = "Acme and Globex are the same organization."

    with pytest.raises(EntityOperationError, match="not present in the cited source"):
        merge_entities(str(tmp_path), (first, second), at=LATER, evidence=_evidence(assertion))
    with pytest.raises(EntityOperationError, match="source does not exist"):
        merge_entities(str(tmp_path), (first, second), at=LATER, evidence=_evidence(assertion, SECOND_SOURCE))

    assert _snapshot(tmp_path) == before


def test_merge_refuses_different_types_or_visibility_and_unknown_pages(tmp_path):
    assertion = "Acme and Ada are the same entity."
    _source(tmp_path, assertion)
    acme = _entity(tmp_path, "Acme")
    ada = _entity(tmp_path, "Ada", entity_type="person")
    secret = _entity(tmp_path, "Secret Co", acl=("finance",))
    before = _snapshot(tmp_path)

    with pytest.raises(EntityOperationError, match="different types"):
        merge_entities(str(tmp_path), (acme, ada), at=LATER, evidence=_evidence(assertion))
    with pytest.raises(EntityOperationError, match="different visibility"):
        merge_entities(str(tmp_path), (acme, secret), at=LATER, evidence=_evidence(assertion))
    with pytest.raises(EntityOperationError, match="distinct existing entity pages"):
        merge_entities(str(tmp_path), (acme, acme), at=LATER, evidence=_evidence(assertion))
    with pytest.raises(EntityOperationError, match="distinct existing entity pages"):
        merge_entities(str(tmp_path), (acme, "wiki/entities/Missing.md"), at=LATER, evidence=_evidence(assertion))

    assert _snapshot(tmp_path) == before


def test_rename_moves_the_page_keeps_the_old_title_as_alias_and_rewrites_links(tmp_path):
    _source(tmp_path, "Acme partners with Globex.")
    acme = _entity(tmp_path, "Acme", aliases=("ACME Inc",))
    _entity(tmp_path, "Globex")
    note = _note(tmp_path, f"[[Acme]] partners with [[Globex]] and [[Acme|the vendor]]. (Source: `{SOURCE}`)")
    original = read_page(str(tmp_path), acme)

    renamed = rename_entity(str(tmp_path), acme, "Acme  Holdings", at=LATER)

    assert renamed == "wiki/entities/Acme Holdings.md"
    assert not tmp_path.joinpath(*acme.split("/")).exists()
    page = read_page(str(tmp_path), renamed)
    assert page.title == "Acme Holdings"
    assert page.page_id == original.page_id
    assert page.aliases == ("ACME Inc", "Acme")
    assert page.entity_type == "organization"
    assert "Acme is an identity in the wiki." in page.body
    assert (page.created, page.updated) == (NOW.date(), LATER.date())
    assert read_page(str(tmp_path), note).body == (
        "# Relationship\n\n[[Acme Holdings|Acme]] partners with [[Globex]] and "
        f"[[Acme Holdings|the vendor]]. (Source: `{SOURCE}`)"
    )
    assert find_entity(entity_pages(str(tmp_path)), "acme").path == renamed
    assert check(str(tmp_path)) == ()


def test_rename_refuses_claimed_names_unknown_pages_and_unusable_titles(tmp_path):
    acme = _entity(tmp_path, "Acme")
    _entity(tmp_path, "Globex", aliases=("Acme Holdings",))
    _entity(tmp_path, "Initech")
    before = _snapshot(tmp_path)

    with pytest.raises(EntityOperationError, match="already uses that title"):
        rename_entity(str(tmp_path), acme, "Initech", at=LATER)
    with pytest.raises(EntityOperationError, match="already claims that name"):
        rename_entity(str(tmp_path), acme, "acme holdings", at=LATER)
    with pytest.raises(EntityOperationError, match="does not exist"):
        rename_entity(str(tmp_path), "wiki/entities/Missing.md", "Anything", at=LATER)
    with pytest.raises(EntityOperationError, match="cannot form a filename"):
        rename_entity(str(tmp_path), acme, "///", at=LATER)

    assert _snapshot(tmp_path) == before


def test_rewrite_links_keeps_shown_text_anchors_and_embeds(tmp_path):
    peer = _entity(tmp_path, "Peer", body=f"# Peer\n\nPeer works with [[Old]]. (Source: `{SOURCE}`)")
    note = _note(tmp_path, "[[Old]] [[Old|Label]] [[Old#Facts]] ![[Old]] [[old.md]] [[Older]] [[Other|Old]]")
    concept = page_path("concept", "Untouched")
    tmp_path.joinpath(*concept.split("/")).parent.mkdir(parents=True)
    tmp_path.joinpath(*concept.split("/")).write_text(
        render_page(path=concept, role="concept", title="Untouched", body="# Untouched\n\nNo links here.", acl=None),
        encoding="utf-8",
    )
    before = _snapshot(tmp_path)

    assert rewrite_links(str(tmp_path), {}) == ()
    assert _snapshot(tmp_path) == before
    assert rewrite_links(str(tmp_path), {"old": "New"}) == (note, peer)

    page = read_page(str(tmp_path), note)
    assert page.title == "Relationship"
    assert page.body == (
        "# Relationship\n\n[[New|Old]] [[New|Label]] [[New#Facts|Old]] ![[New|Old]] [[New|old]] [[Older]] [[Other|Old]]"
    )
    assert "[[New|Old]]" in read_page(str(tmp_path), peer).body
    assert _snapshot(tmp_path)[Path(concept)] == before[Path(concept)]
