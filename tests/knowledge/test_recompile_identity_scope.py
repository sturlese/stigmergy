import datetime as dt

import pytest

from stigmergy.knowledge import contradictions, writer
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import ContradictionClaim, FilingPlan, PageMutation
from stigmergy.knowledge.write_guard import WriteContext


def _prior_page(
    *,
    title="Prior knowledge",
    sources=("sources/2026/09/source.md",),
    acl=None,
    body=None,
):
    path = f"wiki/notes/{title}.md"
    text = render_page(
        path=path,
        role="note",
        title=title,
        body=body or f"# {title}\n\nDurable source-backed knowledge.",
        acl=acl,
        sources=sources,
        created=dt.date(2026, 9, 1),
        updated=dt.date(2026, 9, 1),
    )
    return path, parse_page(path, text)


def test_recompile_treats_entity_pages_as_derived_knowledge(tmp_path):
    """Entity pages are cleared and rebuilt like notes, and their prior context keeps their names."""
    source = "sources/2026/09/source.md"
    note_path, _note = _prior_page(sources=(source,))
    entity_path = "wiki/entities/Acme.md"
    entity_text = render_page(
        path=entity_path,
        role="entity",
        title="Acme",
        body="# Acme\n\nAn organization backed by the source.",
        acl=None,
        sources=(source,),
        entity_type="organization",
        aliases=("ACME Inc",),
        created=dt.date(2026, 9, 1),
        updated=dt.date(2026, 9, 1),
    )
    for path, text in ((note_path, render_page(
        path=note_path,
        role="note",
        title="Prior knowledge",
        body="# Prior knowledge\n\nDurable source-backed knowledge.",
        acl=None,
        sources=(source,),
    )), (entity_path, entity_text)):
        target = tmp_path.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    assert writer._derived_paths(str(tmp_path)) == {note_path, entity_path}
    context = writer._recompile_prior_page_context(parse_page(entity_path, entity_text))
    assert (context["type"], context["entity_type"], context["aliases"]) == ("entity", "organization", ["ACME Inc"])

    writer._clear_derived_pages(str(tmp_path))

    assert writer._derived_paths(str(tmp_path)) == set()


def test_recompile_prior_context_fails_closed_across_source_scopes():
    source = "sources/2026/09/source.md"
    path, page = _prior_page(sources=(source,), acl=("finance",))

    with pytest.raises(writer.GateRefused, match="outside its backing source audience"):
        writer._recompile_prior_pages(
            {path: page},
            source=source,
            context=WriteContext(None, ("engineering",), unrestricted=True),
        )


def test_recompile_rejects_unknown_or_conflicting_tombstones():
    path, page = _prior_page()
    unknown = FilingPlan(
        summary="Tried to remove an unknown page",
        mutations=(
            PageMutation(
                action="delete",
                path="wiki/notes/Unknown.md",
                reason="It appears obsolete",
            ),
        ),
    )
    with pytest.raises(writer.GateRefused, match="not backed by this source"):
        writer._prepare_recompile_plan(unknown, prior_pages={path: page})

    conflicting = FilingPlan(
        summary="Tried conflicting dispositions",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Prior knowledge",
                body="# Prior knowledge\n\nRecreated durable knowledge.",
                reason="The knowledge remains durable",
            ),
            PageMutation(
                action="delete",
                path=path,
                reason="The same page appears obsolete",
            ),
        ),
    )
    with pytest.raises(writer.GateRefused, match="tombstone and recreate"):
        writer._prepare_recompile_plan(conflicting, prior_pages={path: page})


def test_recompile_rejects_tombstone_for_page_with_open_contradiction():
    source = "sources/2026/09/source.md"
    record = contradictions.Contradiction(
        contradiction_id="con_00000000-0000-4000-8000-000000000001",
        explanation="The sources disagree.",
        claims=(
            ContradictionClaim(text="The term is annual.", source=source),
            ContradictionClaim(text="The term is monthly.", source=source),
        ),
    )
    body = f"# Prior knowledge\n\nDurable knowledge.\n\n{contradictions.render(record)}"
    path, page = _prior_page(sources=(source,), body=body)
    plan = FilingPlan(
        summary="Tried to retire disputed knowledge",
        mutations=(
            PageMutation(
                action="delete",
                path=path,
                reason="The disputed page appears obsolete",
            ),
        ),
    )

    with pytest.raises(writer.GateRefused, match="unresolved contradictions"):
        writer._prepare_recompile_plan(plan, prior_pages={path: page})


def test_recompile_requires_every_backing_source_to_authorize_disappearance():
    first = "sources/2026/09/first.md"
    second = "sources/2026/09/second.md"
    path, page = _prior_page(sources=(first, second))

    with pytest.raises(writer.GateRefused, match="every backing source"):
        writer._gate_recompile_disappearance(
            {path: page},
            final_paths=set(),
            tombstone_authorizations={path: {first}},
        )

    assert writer._gate_recompile_disappearance(
        {path: page},
        final_paths=set(),
        tombstone_authorizations={path: {first, second}},
    ) == 1
