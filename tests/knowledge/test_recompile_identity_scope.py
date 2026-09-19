import datetime as dt
from types import SimpleNamespace

import pytest

from stigmergy.knowledge import contradictions, writer
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import ContradictionClaim, EntityProposal, FilingPlan, PageMutation
from stigmergy.knowledge.planner import PlanRun
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


def test_recompile_does_not_converge_hidden_name_matches_across_scopes(tmp_path, monkeypatch):
    relative_source = "sources/2026/09/recompile-scope.md"
    source_file = tmp_path / relative_source
    source_file.parent.mkdir(parents=True)
    source_file.write_text("immutable source", encoding="utf-8")
    plan = FilingPlan.model_construct(
        summary="Proposed Acme from engineering evidence",
        entities=(EntityProposal(name="Acme", entity_type="organization"),),
    )
    source = SimpleNamespace(path=relative_source, text="Acme evidence", body="immutable source")
    hidden_record = SimpleNamespace(
        entity_type="organization",
        claims=(SimpleNamespace(kind="preferred", normalized="acme"),),
    )
    visible_record = SimpleNamespace(
        entity_type="organization",
        claims=(SimpleNamespace(kind="preferred", normalized="acme"),),
    )
    observed = {}

    class Planner:
        def plan(self, **_kwargs):
            return PlanRun(plan=plan, model_requests=1)

    def record_application(_root, applied_plan, **kwargs):
        observed["plan"] = applied_plan
        observed["visible_entity_ids"] = kwargs["visible_entity_ids"]

    monkeypatch.setattr(writer, "recompile_source_preflight", lambda *_args: None)
    monkeypatch.setattr(writer, "read_recompile_source", lambda *_args: source)
    monkeypatch.setattr(writer, "source_envelope", lambda _source: SimpleNamespace(audience=None))
    monkeypatch.setattr(
        writer,
        "filing_context",
        lambda *_args, **_kwargs: {"entities": [{"id": "ent_visible"}], "source_evidence": []},
    )
    monkeypatch.setattr(writer, "_apply_filing_plan", record_application)
    monkeypatch.setattr(writer, "repair_deterministic", lambda *_args: {})
    monkeypatch.setattr(writer, "check", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        writer,
        "load_entities",
        lambda _root: {"ent_visible": visible_record, "ent_hidden": hidden_record},
    )
    monkeypatch.setattr(writer.gitcmd, "diff_entries", lambda _root: ())

    report = writer._recompile_derived(
        str(tmp_path),
        SimpleNamespace(planner=Planner()),
        rationale="Rebuild from immutable evidence",
    )

    assert observed["plan"] is plan
    assert observed["plan"].entities[0].same_as is None
    assert observed["visible_entity_ids"] == frozenset({"ent_visible"})
    assert report["retained_entities"] == 2


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
                entities=(),
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
