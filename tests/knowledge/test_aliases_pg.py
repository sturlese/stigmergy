"""Aliases recorded at filing time from the `[[Entity|variant]]` links a plan adds (spec
retrieval-and-curation §3.5, BQ-14, BQ-15)."""
import datetime as dt
import subprocess
import time
from pathlib import Path

import pytest

from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.changes.store import list_changes
from stigmergy.kernel import llm
from stigmergy.knowledge import aliases, writer
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker

MORGAN = Actor(subject="morgan", display_name="Morgan")
ALICE = Actor(subject="alice", display_name="Alice")
TESSERA = "wiki/entities/Tessera.md"
LAYER_TESSERA = "layers/engineering/wiki/entities/Tessera.md"


def _decider(probability: float = 0.97, *, failure=None, seen: list | None = None, sleep: float = 0.0):
    def decide(options, state):
        if seen is not None:
            seen.append(state)
        time.sleep(sleep)
        if isinstance(failure, BaseException):
            raise failure
        if failure:
            raise llm.DecisionFailed(failure)
        return {aliases.ALIAS: probability, aliases.OTHER: 1 - probability}
    return decide


def _capture(conn, repo, store, *, actor, audience, key, mutations):
    receipt = CaptureService(conn, store).capture_text(
        actor=actor, audience=audience, adapter="mcp", text=f"Evidence for {key}.", idempotency_key=key)
    source = writer.source_path(schema.parse_capture(receipt["request"]))
    plan = FilingPlan(summary=key, mutations=tuple(
        m.model_copy(update={"body": m.body.replace("{source}", source)}) for m in mutations))
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    return worker.process_next(conn, WriterDeps(settings, store, ScriptedPlanner(plan), str(repo)))


def _show(repo, path: str) -> str:
    return subprocess.check_output(["git", "show", f"main:{path}"], cwd=repo, text=True)


def _tessera(action="create") -> PageMutation:
    return PageMutation(action=action, role="entity", title="Tessera", entity_type="organization",
                        reason="entity", path=None if action == "create" else TESSERA,
                        body="# Tessera\n\n## What / Who\nAn edtech company. (Source: `{source}`)\n\n"
                             "## Facts\n- 2026-03-03: presented its pilot. (Source: `{source}`)\n\n"
                             "## Connections\n- [[Tessera meeting]] - where it presented")


def _meeting(link: str, title: str = "Tessera meeting") -> PageMutation:
    return PageMutation(action="create", role="note", title=title, reason="note",
                        body=f"# {title}\n\nOn 3 March 2026 {link} presented its pilot. "
                             "(Source: `{source}`)\n\n## Entities\n- [[Tessera]] - presenting company")


def test_an_accepted_variant_becomes_an_alias_in_the_capture_commit(clean_queue, target_repo):
    seen: list = []
    with llm.decision_override(_decider(0.97, seen=seen)):
        item, outcome = _capture(clean_queue, target_repo, evidence.MemoryEvidenceStore(), actor=MORGAN,
                                 audience=None, key="tessera",
                                 mutations=(_tessera(), _meeting("[[Tessera|Tessera Learning]]")))
    assert outcome.status == "landed"
    assert parse_page(TESSERA, _show(target_repo, TESSERA)).aliases == ("Tessera Learning",)
    assert outcome.report["aliases"] == [
        {"entity": TESSERA, "alias": "Tessera Learning", "decision": "accepted", "probability": 0.97}]
    assert seen[0]["candidate"] == "Tessera Learning"
    assert seen[0]["sentence"].startswith("On 3 March 2026 Tessera Learning presented its pilot.")
    assert seen[0]["entity"]["title"] == "Tessera" and seen[0]["entity"]["type"] == "organization"
    commits = subprocess.check_output(["git", "rev-list", "--count", "main"], cwd=target_repo, text=True)
    assert int(commits) == 2                          # the seed commit and this capture's one commit


@pytest.mark.parametrize("decider,decision", [
    (_decider(0.6), "rejected"),
    (_decider(failure="timeout"), "skipped"),
])
def test_an_unsure_or_failed_decision_writes_nothing_and_the_capture_lands(clean_queue, target_repo,
                                                                            decider, decision):
    with llm.decision_override(decider):
        item, outcome = _capture(clean_queue, target_repo, evidence.MemoryEvidenceStore(), actor=MORGAN,
                                 audience=None, key="unsure",
                                 mutations=(_tessera(), _meeting("[[Tessera|Tessera Learning]]")))
    assert outcome.status == "landed"
    assert parse_page(TESSERA, _show(target_repo, TESSERA)).aliases == ()
    assert outcome.report["aliases"][0]["decision"] == decision


def test_a_restricted_capture_never_adds_an_alias_to_a_public_page(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    with llm.decision_override(_decider(0.99)):
        _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="public",
                 mutations=(_tessera(), _meeting("[[Tessera]]")))
        public_before = _show(target_repo, TESSERA)
        item, outcome = _capture(clean_queue, target_repo, store, actor=ALICE, audience=("engineering",),
                                 key="restricted", mutations=(_meeting("[[Tessera|Tessera Labs]]", "Review"),))
    assert outcome.status == "landed"
    assert _show(target_repo, TESSERA) == public_before
    assert outcome.report["aliases"] == []


def test_a_restricted_capture_records_aliases_on_the_entities_its_layer_stores(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    with llm.decision_override(_decider(0.99)):
        item, outcome = _capture(clean_queue, target_repo, store, actor=ALICE, audience=("engineering",),
                                 key="layer", mutations=(_tessera(), _meeting("[[Tessera|Tessera Labs]]")))
        assert outcome.report["aliases"][0]["decision"] == "accepted"
        assert parse_page(TESSERA, _show(target_repo, LAYER_TESSERA)).aliases == ("Tessera Labs",)
        assert TESSERA not in _tracked(target_repo)
        item, outcome = _capture(clean_queue, target_repo, store, actor=ALICE, audience=("engineering",),
                                 key="layer-again",
                                 mutations=(_meeting("[[Tessera|Tessera Labs Inc]]", "Second review"),))
    assert parse_page(TESSERA, _show(target_repo, LAYER_TESSERA)).aliases == ("Tessera Labs", "Tessera Labs Inc")


def _tracked(repo) -> set[str]:
    listing = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "main"], cwd=repo, text=True)
    return set(listing.splitlines())


def test_aliases_that_would_break_a_gate_are_all_undone(clean_queue, target_repo, monkeypatch):
    real = aliases.candidates

    def colliding(root, written, **kwargs):
        found, entities = real(root, written, **kwargs)
        return [aliases.Candidate(TESSERA, "Harbor Fund", "a name another entity claims")], entities

    harbor = PageMutation(action="create", role="entity", title="Harbor Fund", entity_type="organization",
                          reason="entity", body="# Harbor Fund\n\n## What / Who\nA fund. (Source: `{source}`)\n\n"
                                                "## Connections\n- [[Tessera meeting]] - attended")
    monkeypatch.setattr(aliases, "candidates", colliding)
    with llm.decision_override(_decider(0.99)):
        item, outcome = _capture(clean_queue, target_repo, evidence.MemoryEvidenceStore(), actor=MORGAN,
                                 audience=None, key="revert",
                                 mutations=(_tessera(), harbor, _meeting("[[Tessera]] with [[Harbor Fund]]")))
    assert outcome.status == "landed"
    assert outcome.report["aliases"] == [{"entity": TESSERA, "alias": "Harbor Fund",
                                          "decision": "reverted", "probability": 0.99}]
    assert parse_page(TESSERA, _show(target_repo, TESSERA)).aliases == ()


def _write(root: Path, path: str, *, title, aliases_=(), body="", role="entity"):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    text = render_page(path=path, role=role, title=title, body=body or f"# {title}\n\nText.", acl=None,
                       sources=(), created=dt.date(2026, 1, 1), updated=dt.date(2026, 1, 1),
                       entity_type="organization" if role == "entity" else None,
                       aliases=tuple(aliases_) if role == "entity" else ())
    file.write_text(text, encoding="utf-8")
    return text


def _fixture(root: Path) -> None:
    _write(root, "wiki/entities/Acme.md", title="Acme", aliases_=("Acme Corp",))
    _write(root, "wiki/entities/Acme Labs.md", title="Acme Labs", aliases_=("Labs",))
    _write(root, "wiki/entities/Globex.md", title="Globex")
    _write(root, "wiki/entities/Dana (Initech).md", title="Dana (Initech)")
    _write(root, "wiki/entities/Dana (Acme).md", title="Dana (Acme)")
    _write(root, "wiki/notes/Roadmap.md", title="Roadmap", role="note")


def _names(root: Path) -> dict[str, str | None]:
    return writer._knowledge_link_stems(str(root), (), writer.WriteContext(None, None, unrestricted=True))


def test_candidates_are_variants_the_plan_added_that_name_no_page_yet(tmp_path):
    _fixture(tmp_path)
    before = _write(tmp_path, "wiki/notes/Week.md", title="Week", role="note",
                    body="# Week\n\nEarlier [[Acme|Acme Holdings]] and [[Acme|Old Name]].")
    _write(tmp_path, "wiki/notes/Week.md", title="Week", role="note", body=(
        "# Week\n\nEarlier [[Acme|Acme Holdings]] and [[Acme|Old Name]].\n"
        "[[Acme|ACME corp]] and [[Acme|Labs]] and [[Acme|the customer]] and [[Acme|&]].\n"
        "[[Globex|the customer]] too, [[Acme|Roadmap]], and [[Acme|Acme Holdings]] again.\n"
        "[[Dana (Initech)|Dana]] met the team, and `[[Acme|Code Name]]` is code.\n"
        "Then [[Acme|Acme Group]] signed."))
    found, _entities = aliases.candidates(str(tmp_path), {"wiki/notes/Week.md": before},
                                          link_names=_names(tmp_path), eligible=lambda path: True)
    assert [(c.entity, c.text) for c in found] == [("wiki/entities/Acme.md", "Acme Holdings"),
                                                   ("wiki/entities/Acme.md", "Acme Group")]
    assert found[1].sentence == "Then Acme Group signed."
    none, _ = aliases.candidates(str(tmp_path), {"wiki/notes/Week.md": before},
                                 link_names=_names(tmp_path), eligible=lambda path: False)
    assert none == []


def _record(tmp_path, *, decider, window_s=10.0, path_of=None, variants=1, **kwargs):
    _fixture(tmp_path)
    links = " ".join(f"[[Acme|Acme Variant {index}]]" for index in range(variants))
    _write(tmp_path, "wiki/notes/Week.md", title="Week", role="note",
           body=f"# Week\n\n{links} and [[Globex|Globex Ltd]].")
    with llm.decision_override(decider):
        return aliases.record(str(tmp_path), {"wiki/notes/Week.md": None}, link_names=_names(tmp_path),
                              eligible=lambda path: True, at=dt.date(2026, 10, 2), window_s=window_s,
                              path_of=path_of or (lambda relative: tmp_path / relative), **kwargs)


def test_a_rotated_start_asks_the_candidates_past_the_cap_first(tmp_path):
    report, _before = _record(tmp_path, decider=_decider(0.9), variants=3, max_candidates=1, start=2)
    assert [(entry["alias"], entry["decision"]) for entry in report] == [
        ("Acme Variant 2", "accepted"), ("Globex Ltd", "skipped"), ("Acme Variant 0", "skipped"),
        ("Acme Variant 1", "skipped")]


def test_the_cap_and_the_window_are_reported_not_silent(tmp_path):
    report, before = _record(tmp_path, decider=_decider(0.9), variants=aliases.MAX_CANDIDATES + 1)
    assert [entry["reason"] for entry in report if entry["decision"] == "skipped"] == ["cap", "cap"]
    started = time.monotonic()
    report, _before = _record(tmp_path / "slow", decider=_decider(0.9, sleep=0.3), window_s=0.2, variants=2)
    assert time.monotonic() - started < 0.8
    assert [entry["decision"] for entry in report] == ["accepted", "skipped", "skipped"]
    assert {entry.get("reason") for entry in report[1:]} == {"deadline"}


def test_a_deadline_propagates_instead_of_being_reported(tmp_path):
    with pytest.raises(writer.WriterDeadline):
        _record(tmp_path, decider=_decider(failure=writer.WriterDeadline("capture budget exhausted")))


def test_either_every_accepted_alias_lands_or_none_does(tmp_path):
    originals = {}

    def path_of(relative):
        path = tmp_path / relative
        originals.setdefault(relative, path.read_text(encoding="utf-8") if path.exists() else None)
        if relative.endswith("Globex.md"):
            raise OSError("disk full")
        return path

    with pytest.raises(OSError):
        _record(tmp_path, decider=_decider(0.99), path_of=path_of)
    assert (tmp_path / "wiki/entities/Acme.md").read_text(encoding="utf-8") == originals["wiki/entities/Acme.md"]


def test_a_recompile_keeps_an_entitys_aliases_unless_another_entity_now_claims_them(tmp_path):
    previous = _write(tmp_path, "wiki/entities/Acme.md", title="Acme", aliases_=("Acme Holdings", "Shared"))
    _write(tmp_path, "wiki/entities/Acme.md", title="Acme", aliases_=("Acme Corp",))
    _write(tmp_path, "wiki/entities/Beta.md", title="Beta", aliases_=("Shared",))
    writer._restore_recompiled_page_identity(str(tmp_path), {"wiki/entities/Acme.md": previous})
    rebuilt = parse_page("wiki/entities/Acme.md", (tmp_path / "wiki/entities/Acme.md").read_text(encoding="utf-8"))
    assert rebuilt.aliases == ("Acme Corp", "Acme Holdings")


def _garden(conn, repo, store, key):
    queue.enqueue_garden(conn, schema.GardenRequest(
        idempotency_key=key, actor=schema.Actor(subject="system:garden", display_name="Stigmergy Gardener"),
        rationale="Autonomous corpus health run"))
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    return worker.process_next(conn, WriterDeps(settings, store, ScriptedPlanner(), str(repo)))


def test_the_garden_records_the_names_the_corpus_already_links_an_entity_by(clean_queue, target_repo):
    """Pages filed before aliases were recorded at filing carry `[[Entity|variant]]` links whose
    variant no page answers to; the daily garden records them as filing would have."""
    store = evidence.MemoryEvidenceStore()
    with llm.decision_override(_decider(failure="timeout")):
        _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="backlog",
                 mutations=(_tessera(), _meeting("[[Tessera|Tessera Learning]]")))
    assert parse_page(TESSERA, _show(target_repo, TESSERA)).aliases == ()

    with llm.decision_override(_decider(0.97)):
        item, outcome = _garden(clean_queue, target_repo, store, "garden-aliases")
        assert outcome.status == "landed" and outcome.report["aliases_recorded"] == 1
        assert parse_page(TESSERA, _show(target_repo, TESSERA)).aliases == ("Tessera Learning",)
        assert list_changes(clean_queue)[0].trigger == "garden"
        _item, again = _garden(clean_queue, target_repo, store, "garden-aliases-again")
    assert again.status == "landed" and again.report["aliases_recorded"] == 0
    assert again.report["commit_sha"] == ""


def test_the_garden_never_records_a_layers_variant_on_a_public_page(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    with llm.decision_override(_decider(0.99)):
        _capture(clean_queue, target_repo, store, actor=MORGAN, audience=None, key="public",
                 mutations=(_tessera(), _meeting("[[Tessera]]")))
    with llm.decision_override(_decider(failure="timeout")):
        _capture(clean_queue, target_repo, store, actor=ALICE, audience=("engineering",), key="restricted",
                 mutations=(_meeting("[[Tessera|Tessera Labs]]", "Review"),))
    public_before = _show(target_repo, TESSERA)
    with llm.decision_override(_decider(0.99)):
        _item, outcome = _garden(clean_queue, target_repo, store, "garden-layer")
    assert outcome.status == "landed" and outcome.report["aliases_recorded"] == 0
    assert _show(target_repo, TESSERA) == public_before
