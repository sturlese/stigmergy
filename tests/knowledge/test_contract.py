import datetime as dt
import json

import pytest

from stigmergy.capture import schema
from stigmergy.kernel.layers import layer_of_acl, physical_path
from stigmergy.knowledge import contradictions, writer
from stigmergy.knowledge.contradictions import Contradiction
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import (
    ENTITY_TYPES,
    PageContractError,
    page_path,
    parse_page,
    path_role,
    read_page,
    render_page,
)
from stigmergy.knowledge.plan import ContradictionClaim

AT = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)


def _write_source(
    tmp_path,
    source_id: str,
    *,
    acl=None,
    body="Evidence.",
    acquisition=None,
) -> str:
    relative = f"sources/2026/08/{source_id}.md"
    path = tmp_path.joinpath(*physical_path(layer_of_acl(acl), relative).split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": source_id,
        "type": "source",
        "submitted_by": "morgan",
        "acl": acl,
        "captured_at": "2026-08-24T00:00:00+00:00",
        "origin": "mcp",
        "participants": [],
        "artifacts": [
            {
                "sha256": "a" * 64,
                "bytes": 1,
                "media_type": "text/plain",
                "readable_sha256": "a" * 64,
                "extractor": "text",
                "extractor_version": "1",
                "ocr_pages": [],
            }
        ],
    }
    if acquisition is not None:
        metadata["acquisition"] = acquisition
    path.write_text(
        f"---\n{json.dumps(metadata, sort_keys=True)}\n---\n\n# Captured source\n\n{body}\n",
        encoding="utf-8",
    )
    return relative


def _write_page(tmp_path, role: str, title: str, body: str, *, acl=None, sources=(), **entity_fields) -> str:
    """Write a page into the folder of the layer its audience names; return its repository path."""
    path = page_path(role, title)
    stored = physical_path(layer_of_acl(acl), path)
    target = tmp_path.joinpath(*stored.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_page(
            path=path,
            role=role,
            title=title,
            body=body,
            acl=acl,
            sources=sources,
            created=AT.date(),
            updated=AT.date(),
            **entity_fields,
        ),
        encoding="utf-8",
    )
    return stored


def test_page_path_rejects_a_title_that_exceeds_the_filesystem_component_limit():
    with pytest.raises(PageContractError, match="oversized filename"):
        page_path("note", "a" * 253)


def test_page_path_accepts_the_maximum_portable_filename_component():
    assert page_path("note", "a" * 252) == f"wiki/notes/{'a' * 252}.md"


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        ("Robin Hale", "wiki/entities/Robin Hale.md"),
        ("Pedro García", "wiki/entities/Pedro García.md"),
        ("ACME & Co.", "wiki/entities/ACME & Co.md"),
        ("東京", "wiki/entities/東京.md"),
        ("Dana (Initech)", "wiki/entities/Dana (Initech).md"),
    ),
)
def test_entity_pages_follow_the_shared_title_path_contract(title, expected):
    assert page_path("entity", title) == expected
    assert path_role(expected) == "entity"


def test_path_role_recognizes_only_flat_knowledge_folders():
    assert path_role("wiki/notes/Plan.md") == "note"
    assert path_role("wiki/concepts/Plan.md") == "concept"
    assert path_role("wiki/entities/nested/Plan.md") is None
    assert path_role("wiki/entities/Plan.txt") is None
    assert path_role("sources/2026/08/x.md") is None
    assert path_role("ops/identities.json") is None


def test_entity_page_frontmatter_carries_type_and_aliases_and_round_trips(tmp_path):
    path = _write_page(
        tmp_path,
        "entity",
        "Acme Holdings",
        "# Acme Holdings\n\nAcme Holdings is a vendor.",
        entity_type="organization",
        aliases=("ACME", "Acme Holdings Ltd"),
    )

    page = read_page(str(tmp_path), path)

    assert page.role == "entity"
    assert page.entity_type == "organization"
    assert page.aliases == ("ACME", "Acme Holdings Ltd")
    assert page.name_keys() == ("acme holdings", "acme", "acme holdings ltd")
    text = tmp_path.joinpath(*path.split("/")).read_text(encoding="utf-8")
    assert "entity_type: organization" in text
    assert "- Acme Holdings Ltd" in text
    assert "entity:" not in text


@pytest.mark.parametrize(
    ("fields", "message"),
    (
        ({"entity_type": "company"}, "entity type is invalid"),
        ({"entity_type": None}, "entity type is invalid"),
        ({"entity_type": "person", "aliases": ("ACME",)}, "differ from the title"),
        ({"entity_type": "person", "aliases": ("Ada", "ADA")}, "aliases must be unique"),
        ({"entity_type": "person", "aliases": ("!!!",)}, "searchable text"),
    ),
)
def test_entity_page_contract_rejects_invalid_type_or_aliases(fields, message):
    with pytest.raises(PageContractError, match=message):
        render_page(
            path="wiki/entities/Acme.md",
            role="entity",
            title="Acme",
            body="# Acme\n\nA vendor.",
            acl=None,
            **fields,
        )


@pytest.mark.parametrize("role", ("note", "concept", "project"))
@pytest.mark.parametrize("fields", ({"entity_type": "person"}, {"aliases": ("Alias",)}))
def test_notes_and_concepts_reject_entity_fields(role, fields):
    with pytest.raises(PageContractError, match="only entity pages carry"):
        render_page(
            path=page_path(role, "Plan"),
            role=role,
            title="Plan",
            body="# Plan\n\nA plan.",
            acl=None,
            **fields,
        )
    text = render_page(path=page_path(role, "Plan"), role=role, title="Plan", body="# Plan\n\nA plan.", acl=None)
    forged = text.replace("sources: []", "sources: []\naliases:\n- Alias", 1)
    with pytest.raises(PageContractError, match="only entity pages carry"):
        parse_page(page_path(role, "Plan"), forged)


def test_a_project_is_its_own_page_role_and_no_longer_an_entity_type():
    text = render_page(path="wiki/projects/Initech seed round.md", role="project", title="Initech seed round",
                       body="# Initech seed round\n\n## Goal\n\nRaise the seed round.", acl=None)

    page = parse_page("wiki/projects/Initech seed round.md", text)
    assert (page.role, page.entity_type, path_role(page.path)) == ("project", None, "project")
    assert page_path("project", "Initech seed round") == "wiki/projects/Initech seed round.md"
    assert "project" not in ENTITY_TYPES
    with pytest.raises(PageContractError, match="entity type is invalid"):
        render_page(path="wiki/entities/Initech.md", role="entity", title="Initech",
                    body="# Initech\n\nA company.", acl=None, entity_type="project")


def test_parse_page_requires_a_known_entity_type_on_entity_pages():
    text = render_page(
        path="wiki/entities/Ada.md",
        role="entity",
        title="Ada",
        body="# Ada\n\nA person.",
        acl=None,
        entity_type="person",
    )

    assert parse_page("wiki/entities/Ada.md", text).entity_type == "person"
    with pytest.raises(PageContractError, match="entity type is invalid"):
        parse_page("wiki/entities/Ada.md", text.replace("entity_type: person", "entity_type: robot"))
    with pytest.raises(PageContractError, match="entity type is invalid"):
        parse_page("wiki/entities/Ada.md", text.replace("entity_type: person\n", ""))
    assert "person" in ENTITY_TYPES and "robot" not in ENTITY_TYPES


def test_entity_merge_folds_pages_and_rewrites_links_into_a_lint_clean_corpus(tmp_path):
    assertion = "Acme and Globex are duplicate company identities."
    source = _write_source(tmp_path, "11111111-1111-4111-8111-111111111111", body=assertion)
    survivor = _write_page(
        tmp_path,
        "entity",
        "Acme",
        f"# Acme\n\nAcme builds widgets. (Source: `{source}`)",
        sources=(source,),
        entity_type="organization",
    )
    absorbed = _write_page(
        tmp_path,
        "entity",
        "Globex",
        f"# Globex\n\nGlobex is the former trading name. (Source: `{source}`)",
        sources=(source,),
        entity_type="organization",
        aliases=("Globex Corp",),
    )
    note = _write_page(
        tmp_path,
        "note",
        "Acme review",
        f"# Acme review\n\nCurrent review of [[Globex]] and [[Acme]]. (Source: `{source}`)",
        acl=("finance",),
        sources=(source,),
    )

    canonical = writer._merge_in_layer(
        str(tmp_path),
        (survivor, absorbed),
        at=AT + dt.timedelta(days=1),
        evidence=schema.EntityMergeEvidence(
            source_assertions=(schema.SourceMergeAssertion(path=source, assertion=assertion),),
        ),
    )

    assert canonical == survivor
    assert not tmp_path.joinpath(*absorbed.split("/")).exists()
    page = read_page(str(tmp_path), survivor)
    assert page.aliases == ("Globex", "Globex Corp")
    assert "## Formerly Globex" in page.body
    assert "Globex is the former trading name." in page.body
    assert note == "layers/finance/wiki/notes/Acme review.md"
    assert "[[Acme|Globex]]" in read_page(str(tmp_path / "layers" / "finance"), "wiki/notes/Acme review.md").body
    assert check(str(tmp_path)) == ()


def test_contradiction_round_trips_as_visible_canonical_markdown():
    record = Contradiction(
        contradiction_id="con_11111111-1111-4111-8111-111111111111",
        explanation="Two signed sources disagree.",
        claims=(
            ContradictionClaim(
                text="The renewal is annual.",
                source="sources/2026/08/11111111-1111-4111-8111-111111111111.md",
                date="2026-08-01",
            ),
            ContradictionClaim(
                text="The renewal is monthly.",
                source="sources/2026/08/22222222-2222-4222-8222-222222222222.md",
                date="2026-08-02",
            ),
        ),
    )

    block = contradictions.render(record)
    parsed = contradictions.parse_all(block)

    assert parsed[0].record == record
    assert "Unresolved contradiction" in block
    assert "The renewal is annual" in block
    assert contradictions.remove(block, record.contradiction_id) == ("\n", True)


def test_linter_accepts_an_empty_target_without_any_registry_file(tmp_path):
    assert check(str(tmp_path)) == ()
    assert not (tmp_path / "ops" / "entity-registry.json").exists()


def test_linter_rejects_capability_data_in_source_provenance(tmp_path):
    source = _write_source(
        tmp_path,
        "11111111-1111-4111-8111-111111111111",
        acquisition={
            "original_url": "https://files.example/report?token=secret",
            "final_url": "https://files.example/report",
            "acquired_at": "2026-08-24T00:00:00+00:00",
        },
    )

    violations = check(str(tmp_path))

    assert [(item.path, item.code) for item in violations] == [(source, "page-contract")]


def test_linter_reports_alias_collisions_between_entity_pages(tmp_path):
    first = _write_page(
        tmp_path,
        "entity",
        "Acme",
        "# Acme\n\nA vendor.",
        entity_type="organization",
    )
    second = _write_page(
        tmp_path,
        "entity",
        "Acme Holdings",
        "# Acme Holdings\n\nThe same vendor.",
        entity_type="organization",
        aliases=("ACME",),
    )
    _write_page(tmp_path, "entity", "Globex", "# Globex\n\nAnother vendor.", entity_type="organization")

    violations = check(str(tmp_path))

    assert sorted((item.path, item.code) for item in violations) == sorted(
        [(first, "alias-collision"), (second, "alias-collision")]
    )
    assert all("'acme'" in item.message for item in violations)


def test_linter_reports_links_that_cross_entity_visibility(tmp_path):
    _write_page(
        tmp_path,
        "entity",
        "Secret Co",
        "# Secret Co\n\nA restricted vendor.",
        acl=("finance",),
        entity_type="organization",
    )
    note = _write_page(tmp_path, "note", "Public plan", "# Public plan\n\nWe met [[Secret Co]] today.")
    allowed = _write_page(
        tmp_path,
        "note",
        "Finance plan",
        "# Finance plan\n\nWe met [[Secret Co]] today.",
        acl=("finance",),
    )

    violations = check(str(tmp_path))

    # A public page resolves links among public pages only: the finance entity is not there.
    assert [(item.path, item.code) for item in violations] == [(note, "dead-link")]
    assert allowed not in {item.path for item in violations}


def test_linter_rejects_an_entity_page_with_an_invalid_contract(tmp_path):
    path = _write_page(tmp_path, "entity", "Ada", "# Ada\n\nA person.", entity_type="person")
    target = tmp_path.joinpath(*path.split("/"))
    target.write_text(
        target.read_text(encoding="utf-8").replace("entity_type: person", "entity_type: robot"),
        encoding="utf-8",
    )

    assert [(item.path, item.code) for item in check(str(tmp_path))] == [(path, "page-contract")]


def test_editorial_gate_rejects_a_heading_only_candidate_page(tmp_path):
    source = _write_source(tmp_path, "11111111-1111-4111-8111-111111111111")
    path = _write_page(tmp_path, "concept", "Decision trace", "# Decision trace", sources=(source,))

    violations = check(str(tmp_path), editorial_paths=frozenset({path}))

    assert {item.code for item in violations} == {
        "empty-knowledge-body",
        "missing-local-source-attribution",
    }


def test_editorial_gate_requires_declared_local_source_references(tmp_path):
    source = _write_source(tmp_path, "11111111-1111-4111-8111-111111111111")
    _write_page(
        tmp_path,
        "concept",
        "Agent harness",
        "# Agent harness\n\nAn agent harness is the operating system around a model.",
    )
    path = _write_page(
        tmp_path,
        "concept",
        "Harness engineering",
        (
            "# Harness engineering\n\n[[Agent harness]]\n\n"
            "This page cites an undeclared source: "
            "`sources/2026/08/ffffffff-ffff-4fff-8fff-ffffffffffff.md`."
        ),
        sources=(source,),
    )

    violations = check(str(tmp_path), editorial_paths=frozenset({path}))

    assert {item.code for item in violations} == {
        "invalid-local-source-reference",
        "missing-local-source-attribution",
    }


def test_editorial_gate_applies_to_entity_pages(tmp_path):
    source = _write_source(tmp_path, "11111111-1111-4111-8111-111111111111")
    path = _write_page(
        tmp_path,
        "entity",
        "Ada",
        "# Ada\n\nTBD",
        sources=(source,),
        entity_type="person",
    )

    violations = check(str(tmp_path), editorial_paths=frozenset({path}))

    assert {item.code for item in violations} == {
        "placeholder-knowledge-body",
        "missing-local-source-attribution",
    }
