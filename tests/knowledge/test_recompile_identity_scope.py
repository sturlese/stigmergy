from types import SimpleNamespace

from stigmergy.knowledge import writer
from stigmergy.knowledge.plan import EntityProposal, FilingPlan


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
            return SimpleNamespace(plan=plan, model_requests=1)

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
