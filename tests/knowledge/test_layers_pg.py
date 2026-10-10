"""Audience layers end to end: one capture files in one layer, the librarian sees a tree shaped like
today's, and readers see the union of their layers with shared pages fused."""
import subprocess

from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.capture.source import render_source, source_path
from stigmergy.index import build, corpus
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.knowledge import writer
from stigmergy.knowledge.context import actor_scope, filing_context, render_context
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import PlanRun, ScriptedPlanner, _filing_prompt
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings as ServerSettings

MORGAN = Actor(subject="morgan", display_name="Morgan")
BOB = Actor(subject="bob", display_name="Bob")
ALICE = Actor(subject="alice", display_name="Alice")


class RecordingPlanner(ScriptedPlanner):
    """Keeps every argument the writer handed to the model."""

    calls: list

    def plan(self, **kwargs) -> PlanRun:
        self.calls = [*getattr(self, "calls", []), kwargs]
        return super().plan(**kwargs)


class ExtendingPlanner(RecordingPlanner):
    """Updates one existing page the way the librarian does: its whole shown body, plus a unit."""

    def __init__(self, path: str, addition: str, *, section: str | None = None):
        super().__init__(FilingPlan(summary="Extended an existing page"))
        self.path, self.addition, self.section = path, addition, section

    def plan(self, **kwargs) -> PlanRun:
        import json

        context = json.loads(kwargs["context"])
        body = next(item["body"] for item in context["candidates"] if item["path"] == self.path)
        addition = self.addition.replace("{source}", f"(Source: `{kwargs['source_path']}`)")
        if self.section and self.section in body:
            head, tail = body.split(self.section, 1)
            lines = tail.split("\n\n", 1)
            body = head + self.section + lines[0].rstrip("\n") + "\n" + addition + (
                "\n\n" + lines[1] if len(lines) > 1 else "")
        else:
            body = f"{body.rstrip()}\n\n{addition}"
        self.result = FilingPlan(summary="Extended an existing page", mutations=(PageMutation(
            action="update", path=self.path, body=body, reason="The source adds to this page"),))
        return super().plan(**kwargs)


def _capture(conn, repo, store, *, actor, audience, key, text, plan, planner=None):
    """Queue and file one capture; `{source}` in any planned body becomes its source path."""
    receipt = CaptureService(conn, store).capture_text(
        actor=actor, audience=audience, adapter="mcp", text=text, idempotency_key=key)
    planner = planner or ScriptedPlanner(plan)
    planner.result = _cited(planner.result, _source(receipt))
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(conn, WriterDeps(settings, store, planner, str(repo)))
    assert outcome.status == schema.LANDED, item
    assert item["report"]["plan_rejected"] is False, item["report"]
    return receipt, item


def _cited(plan: FilingPlan, source: str) -> FilingPlan:
    mutations = tuple(
        mutation.model_copy(update={"body": mutation.body.replace("{source}", f"(Source: `{source}`)")})
        if mutation.body else mutation
        for mutation in plan.mutations
    )
    return plan.model_copy(update={"mutations": mutations})


def _source(receipt) -> str:
    return source_path(schema.parse_capture(receipt["request"]))


def _entity(title, body, **extra):
    return PageMutation(action="create", role="entity", title=title, entity_type="organization",
                        body=body, reason="The source names it", **extra)


def _tree(repo) -> list[str]:
    return subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "main"], cwd=repo,
                                   text=True).splitlines()


def _changed(repo, commit) -> list[str]:
    return subprocess.check_output(["git", "show", "--format=", "--name-only", commit], cwd=repo,
                                   text=True).splitlines()


def _reader(conn, audiences):
    return BrainService(ServerSettings(llm="fake"), conn, build_embedder("fake"), audiences)


def test_a_layer_capture_changes_only_its_layer_folder(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=BOB, audience=("finance",), adapter="mcp", text="Stealth Holdings is a counterparty.",
        idempotency_key="layer-scope")
    source = _source(receipt)
    plan = FilingPlan(summary="Recorded the counterparty", mutations=(
        _entity("Stealth Holdings", f"# Stealth Holdings\n\nA counterparty. (Source: `{source}`)"),))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(
        clean_queue, WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo)))

    assert outcome.status == schema.LANDED
    assert item["source_path"] == f"layers/finance/{source}"
    assert sorted(_changed(target_repo, item["commit_sha"])) == [
        f"layers/finance/{source}", "layers/finance/wiki/entities/Stealth Holdings.md"]
    assert check(str(target_repo)) == ()


def test_the_same_title_lives_in_several_layers_in_either_order(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    first, _item = _capture(
        clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="acme-finance",
        text="Acme asked finance for a discount.", plan=None, planner=RecordingPlanner(FilingPlan(
            summary="Recorded Acme for finance", mutations=(
                _entity("Acme", "# Acme\n\nAcme asked for a discount. {source}"),))))
    public_planner = RecordingPlanner(FilingPlan(summary="Recorded Acme", mutations=(
        _entity("Acme", "# Acme\n\nAcme builds robots. {source}"),)))
    _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="acme-public",
             text="Acme builds robots.", plan=None, planner=public_planner)

    tree = _tree(target_repo)
    assert "layers/finance/wiki/entities/Acme.md" in tree
    assert "wiki/entities/Acme.md" in tree
    assert "discount" not in public_planner.calls[0]["context"]
    assert check(str(target_repo)) == ()

    later = ExtendingPlanner("wiki/entities/Acme.md", "Acme pays yearly. {source}")
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="acme-terms",
             text="Acme pays yearly.", plan=None, planner=later)
    candidate = next(item for item in _context(later)["candidates"] if item["path"] == "wiki/entities/Acme.md")
    assert "Acme builds robots." in candidate["body"] and "discount" in candidate["body"]
    finance_part = subprocess.check_output(
        ["git", "show", "main:layers/finance/wiki/entities/Acme.md"], cwd=target_repo, text=True)
    assert "Acme pays yearly." in finance_part
    assert "Acme builds robots." not in finance_part
    assert first["id"]


def _context(planner) -> dict:
    import json

    return json.loads(planner.calls[-1]["context"])


def test_the_librarian_never_sees_a_layer(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="seed-layer",
             text="Orbit is a supplier.", plan=FilingPlan(summary="Recorded Orbit", mutations=(
                 _entity("Orbit", "# Orbit\n\nOrbit is a supplier. {source}"),)))
    planner = RecordingPlanner(FilingPlan(summary="Nothing durable"))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="second-layer",
             text="Orbit renewed its contract.", plan=None, planner=planner)

    call = planner.calls[0]
    prompt = _filing_prompt(envelope=call["envelope"], source_path=call["source_path"],
                            source_text=call["source_text"], context=call["context"])
    assert "finance" not in prompt
    assert "layers/" not in prompt
    assert call["envelope"].audience is None
    assert call["source_path"].startswith("sources/")


def test_a_public_only_prompt_is_the_prompt_the_tree_itself_gives(clean_queue, target_repo, tmp_path):
    store = evidence.MemoryEvidenceStore()
    seed, _item = _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="plain-seed",
                           text="Northstar runs the payroll pilot.", plan=FilingPlan(
                               summary="Recorded Northstar", mutations=(
                                   _entity("Northstar", "# Northstar\n\nNorthstar runs the payroll pilot. {source}"),)))
    base = subprocess.check_output(["git", "rev-parse", "main"], cwd=target_repo, text=True).strip()
    planner = RecordingPlanner(FilingPlan(summary="Nothing durable"))
    receipt, _item = _capture(clean_queue, target_repo, store, actor=ALICE, audience=None, key="plain-next",
                              text="Northstar extended the payroll pilot.", plan=None, planner=planner)

    checkout = tmp_path / "checkout"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(checkout), base], cwd=target_repo, check=True)
    envelope = schema.parse_capture(receipt["request"])
    extracted = writer.extract_capture(store, envelope, ocr_model=config.Settings(
        repo=str(target_repo), branch="main", backend="scripted").ocr_model)
    text = render_source(envelope, extracted)
    relative = source_path(envelope)
    (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
    (checkout / relative).write_text(text, encoding="utf-8")
    groups, _unrestricted = actor_scope(clean_queue, ALICE.subject)
    direct = render_context(filing_context(str(checkout), source_text=text, capture_acl=envelope.audience,
                                           actor_groups=groups))

    call = planner.calls[0]
    assert _filing_prompt(envelope=call["envelope"], source_path=call["source_path"],
                          source_text=call["source_text"], context=call["context"]) == _filing_prompt(
        envelope=envelope, source_path=relative, source_text=text, context=direct)
    assert seed["id"]


def test_readers_see_the_union_of_their_layers_fused(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="fuse-public",
             text="Acme builds robots.", plan=FilingPlan(summary="Recorded Acme", mutations=(
                 _entity("Acme", "# Acme\n\n## Facts\n- 2026-09-02: Acme builds robots. {source}"),)))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="fuse-finance",
             text="Acme negotiated a discount.", plan=None, planner=ExtendingPlanner(
                 "wiki/entities/Acme.md", "- 2026-09-01: Acme negotiated a discount. {source}",
                 section="## Facts\n"))
    build.rebuild(clean_queue, str(target_repo), build_embedder("fake"))

    finance = _reader(clean_queue, {"finance"}).read_page("wiki/entities/Acme.md")
    open_reader = _reader(clean_queue, {"engineering"}).read_page("wiki/entities/Acme.md")
    assert finance["layers"] == ["public", "finance"]
    assert "- [layer:finance] 2026-09-01: Acme negotiated a discount." in finance["body"]
    assert finance["body"].index("2026-09-01") < finance["body"].index("2026-09-02")
    assert open_reader["layers"] == ["public"]
    assert "discount" not in open_reader["body"]
    entities = _reader(clean_queue, {"engineering"}).entity_vocabulary()["entities"]
    assert [(item["path"], item["layers"]) for item in entities] == [("wiki/entities/Acme.md", ["public"])]


def test_deleting_a_source_removes_the_units_that_cite_it(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    first = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Acme builds robots.", idempotency_key="cite-a")
    first_source = _source(first)
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    worker.process_next(clean_queue, WriterDeps(settings, store, ScriptedPlanner(FilingPlan(
        summary="Recorded Acme", mutations=(_entity(
            "Acme", f"# Acme\n\n## Facts\n- 2026-09-01: Acme builds robots. (Source: `{first_source}`)"),))),
        str(target_repo)))
    second = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Acme opened Berlin.", idempotency_key="cite-b")
    second_source = _source(second)
    worker.process_next(clean_queue, WriterDeps(settings, store, ScriptedPlanner(FilingPlan(
        summary="Recorded Berlin", mutations=(PageMutation(
            action="update", path="wiki/entities/Acme.md",
            body=(f"# Acme\n\n## Facts\n- 2026-09-01: Acme builds robots. (Source: `{first_source}`)\n"
                  f"- 2026-09-03: Acme opened Berlin. (Source: `{second_source}`)"),
            reason="A new office"),))), str(target_repo)))

    queue.enqueue_delete(clean_queue, schema.DeleteRequest(
        idempotency_key="delete-second", actor=MORGAN, paths=(second_source,), rationale="Retracted"))
    _item, outcome = worker.process_next(clean_queue, WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)))

    assert outcome.status == schema.LANDED
    page = subprocess.check_output(["git", "show", "main:wiki/entities/Acme.md"], cwd=target_repo, text=True)
    assert "Acme builds robots." in page
    assert "Berlin" not in page and second_source not in page
    assert check(str(target_repo)) == ()


class SourcePlanner(RecordingPlanner):
    """One plan per source: public sources recreate the entity, layer sources extend it."""

    def __init__(self, public_source: str):
        super().__init__(FilingPlan(summary="Recompiled"))
        self.public_source = public_source

    def plan(self, **kwargs) -> PlanRun:
        import json

        source = kwargs["source_path"]
        if source == self.public_source:
            self.result = FilingPlan(summary="Recompiled Acme", mutations=(_entity(
                "Acme", f"# Acme\n\n## Facts\n- 2026-09-02: Acme builds robots. (Source: `{source}`)"),))
        else:
            context = json.loads(kwargs["context"])
            body = next(item["body"] for item in context["candidates"] if item["path"] == "wiki/entities/Acme.md")
            self.result = FilingPlan(summary="Recompiled the discount", mutations=(PageMutation(
                action="update", path="wiki/entities/Acme.md",
                body=f"{body.rstrip()}\n- 2026-09-01: Acme negotiated a discount. (Source: `{source}`)",
                reason="Finance knows the terms"),))
        return RecordingPlanner.plan(self, **kwargs)


def test_recompile_rebuilds_public_then_each_layer_on_top(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    public_receipt, _item = _capture(
        clean_queue, target_repo, store, actor=MORGAN, audience=None, key="recompile-public",
        text="Acme builds robots.", plan=FilingPlan(summary="Recorded Acme", mutations=(
            _entity("Acme", "# Acme\n\n## Facts\n- 2026-09-02: Acme builds robots. {source}"),)))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="recompile-finance",
             text="Acme negotiated a discount.", plan=None, planner=ExtendingPlanner(
                 "wiki/entities/Acme.md", "- 2026-09-01: Acme negotiated a discount. {source}",
                 section="## Facts\n"))
    before = subprocess.check_output(["git", "show", "main:layers/finance/wiki/entities/Acme.md"],
                                     cwd=target_repo, text=True)
    queue.enqueue_garden(clean_queue, schema.GardenRequest(
        idempotency_key="recompile-layers", actor=MORGAN, rationale="Recompile every layer", mode="recompile"))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    planner = SourcePlanner(_source(public_receipt))

    item, outcome = worker.process_next(clean_queue, WriterDeps(settings, store, planner, str(target_repo)))

    assert outcome.status == schema.LANDED, item
    assert [call["source_path"].startswith("sources/") for call in planner.calls] == [True, True]
    assert all("finance" not in call["context"] for call in planner.calls)
    public_page = subprocess.check_output(["git", "show", "main:wiki/entities/Acme.md"], cwd=target_repo, text=True)
    finance_page = subprocess.check_output(["git", "show", "main:layers/finance/wiki/entities/Acme.md"],
                                           cwd=target_repo, text=True)
    assert "discount" not in public_page
    assert "Acme negotiated a discount." in finance_page and "robots" not in finance_page
    assert before.split("---", 2)[1] != ""
    assert check(str(target_repo)) == ()


def test_renaming_an_entity_renames_every_layers_page_and_links(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="rename-public",
             text="Acme builds robots.", plan=FilingPlan(summary="Recorded Acme", mutations=(
                 _entity("Acme", "# Acme\n\nAcme builds robots. {source}"),)))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="rename-finance",
             text="Acme negotiated a discount.", plan=FilingPlan(summary="Recorded the discount", mutations=(
                 PageMutation(action="create", role="note", title="Discount",
                              body="# Discount\n\n[[Acme]] negotiated a discount. {source}",
                              reason="Finance terms"),)))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="rename-finance-part",
             text="Acme pays yearly.", plan=None, planner=ExtendingPlanner(
                 "wiki/entities/Acme.md", "Acme pays yearly. {source}"))
    queue.enqueue_entity_operation(clean_queue, schema.EntityOperationRequest(
        idempotency_key="rename-acme", actor=MORGAN, action="rename", entities=("wiki/entities/Acme.md",),
        new_title="Acme Robotics", rationale="The company renamed itself"))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    item, outcome = worker.process_next(clean_queue, WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)))

    assert outcome.status == schema.LANDED, item
    tree = _tree(target_repo)
    assert "wiki/entities/Acme Robotics.md" in tree
    assert "layers/finance/wiki/entities/Acme Robotics.md" in tree
    assert not any(path.endswith("entities/Acme.md") for path in tree)
    note = subprocess.check_output(["git", "show", "main:layers/finance/wiki/notes/Discount.md"],
                                   cwd=target_repo, text=True)
    assert "[[Acme Robotics|Acme]]" in note
    assert check(str(target_repo)) == ()


def test_deleting_one_layers_entity_page_leaves_the_others(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="delete-public",
             text="Acme builds robots.", plan=FilingPlan(summary="Recorded Acme", mutations=(
                 _entity("Acme", "# Acme\n\nAcme builds robots. {source}"),)))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="delete-finance",
             text="Acme pays yearly.", plan=None, planner=ExtendingPlanner(
                 "wiki/entities/Acme.md", "Acme pays yearly. {source}"))
    queue.enqueue_entity_operation(clean_queue, schema.EntityOperationRequest(
        idempotency_key="delete-acme-finance", actor=MORGAN, action="delete",
        entities=("layers/finance/wiki/entities/Acme.md",), rationale="Finance withdrew its notes"))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    item, outcome = worker.process_next(clean_queue, WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)))

    assert outcome.status == schema.LANDED, item
    tree = _tree(target_repo)
    assert "wiki/entities/Acme.md" in tree
    assert "layers/finance/wiki/entities/Acme.md" not in tree
    assert check(str(target_repo)) == ()


def test_a_public_name_a_layer_already_uses_elsewhere_files_and_degrades_its_links(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="memo",
             text="The Acme pricing memo.", plan=FilingPlan(summary="Recorded the memo", mutations=(
                 PageMutation(action="create", role="concept", title="Acme",
                              body="# Acme\n\nA pricing approach. {source}", reason="Finance term"),
                 PageMutation(action="create", role="note", title="Pricing memo",
                              body="# Pricing memo\n\nFinance prices with [[Acme]]. {source}",
                              reason="Finance memo"))))

    _receipt, item = _capture(
        clean_queue, target_repo, store, actor=MORGAN, audience=None, key="acme-org",
        text="Acme builds robots.", plan=FilingPlan(summary="Recorded Acme", mutations=(
            _entity("Acme", "# Acme\n\nAcme builds robots. {source}"),)))

    assert "wiki/entities/Acme.md" in _changed(target_repo, item["commit_sha"])
    assert not any(path.startswith("layers/") for path in _changed(target_repo, item["commit_sha"]))
    rows = {row.path: row for row in corpus.load_page_texts([
        (path, subprocess.check_output(["git", "show", f"main:{path}"], cwd=target_repo, text=True))
        for path in _tree(target_repo) if corpus.is_indexable_page(path)])}
    assert rows["layers/finance/wiki/notes/Pricing memo.md"].links == []
    assert rows["wiki/entities/Acme.md"].inlinks == 0


class FailingPlanner(ScriptedPlanner):
    def plan(self, **kwargs) -> PlanRun:
        raise RuntimeError("the model did not answer")


def test_a_failed_layer_filing_leaves_no_view_behind(clean_queue, target_repo, tmp_path_factory):
    scratch = tmp_path_factory.mktemp("scratch")
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_text(
        actor=BOB, audience=("finance",), adapter="mcp", text="Acme asked for a discount.",
        idempotency_key="failing")
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted",
                               worktree_root=str(scratch))
    worker.process_next(clean_queue, WriterDeps(settings, store, FailingPlanner(), str(target_repo)))

    assert list(scratch.iterdir()) == []


def test_a_capture_whose_author_left_its_layer_is_not_filed(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_text(
        actor=BOB, audience=("finance",), adapter="mcp", text="Acme asked for a discount.",
        idempotency_key="left-the-layer")
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE roster_principals SET groups = '{}', default_audience = '{public}' "
                    "WHERE subject = 'bob'")
    planner = RecordingPlanner(FilingPlan(summary="Nothing"))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    head = subprocess.check_output(["git", "rev-parse", "main"], cwd=target_repo, text=True)

    _item, outcome = worker.process_next(clean_queue, WriterDeps(settings, store, planner, str(target_repo)))

    assert outcome.status == schema.FAILED
    assert getattr(planner, "calls", []) == []
    assert subprocess.check_output(["git", "rev-parse", "main"], cwd=target_repo, text=True) == head


def test_a_delete_names_one_layers_page_like_an_entity_delete(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="page-public",
             text="Acme builds robots.", plan=FilingPlan(summary="Recorded Acme", mutations=(
                 _entity("Acme", "# Acme\n\nAcme builds robots. {source}"),)))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="page-finance",
             text="Acme pays yearly.", plan=None, planner=ExtendingPlanner(
                 "wiki/entities/Acme.md", "Acme pays yearly. {source}"))
    queue.enqueue_delete(clean_queue, schema.DeleteRequest(
        idempotency_key="delete-public-acme", actor=MORGAN, paths=("wiki/entities/Acme.md",),
        rationale="The public page was wrong"))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    item, outcome = worker.process_next(clean_queue, WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)))

    assert outcome.status == schema.LANDED, item
    tree = _tree(target_repo)
    assert "wiki/entities/Acme.md" not in tree
    assert "layers/finance/wiki/entities/Acme.md" in tree


def test_a_public_filing_reports_only_its_own_layers_changes(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="old-acme",
             text="Acme builds robots.", plan=FilingPlan(summary="Recorded Acme", mutations=(
                 _entity("Acme", "# Acme\n\nAcme builds robots. {source}"),)))
    _capture(clean_queue, target_repo, store, actor=BOB, audience=("finance",), key="deal",
             text="The Acme deal memo.", plan=FilingPlan(summary="Recorded the deal", mutations=(
                 PageMutation(action="create", role="note", title="Deal memo",
                              body="# Deal memo\n\nFinance priced [[Acme]]. {source}", reason="Deal"),)))

    _receipt, item = _capture(
        clean_queue, target_repo, store, actor=MORGAN, audience=None, key="retire-acme",
        text="Acme was a duplicate of Acme Robotics.", plan=FilingPlan(summary="Retired Acme", mutations=(
            PageMutation(action="delete", path="wiki/entities/Acme.md", reason="Duplicate"),
            _entity("Acme Robotics", "# Acme Robotics\n\nAcme Robotics builds robots. {source}"))))

    changed = _changed(target_repo, item["commit_sha"])
    assert "layers/finance/wiki/notes/Deal memo.md" in changed
    assert item["report"]["wiki_changes"] == 2
