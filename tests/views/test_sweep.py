"""The convergence pass: the UNION population, its single corpus parse, the per-run ceiling and
the cost property.

The property under test is not "a trigger fires" but "`views/` matches the corpus, whatever wrote
it". Every case here therefore mutates the CORPUS through a real commit — the way an ordinary
capture, an applied repair or a hand edit leaves the repo — and then asks the sweep to converge,
never calling a per-door hook. Real git throughout, like the rest of this suite.
"""
import asyncio
import os

import pytest

from stigmergy.kernel.registry import Registry
from stigmergy.views import regenerate, staleness, synthesis
from tests.views.conftest import (
    FakeConn,
    build_repo,
    decision_page,
    git,
    registry_of,
    remote_files,
    remote_log,
)

_COMMIT_ENV = {"GIT_AUTHOR_NAME": "Test Steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
               "GIT_COMMITTER_NAME": "Test Steward", "GIT_COMMITTER_EMAIL": "steward@example.com"}


def commit_all(clone: str, message: str) -> None:
    """Whatever is on disk, committed and pushed — the state a door leaves behind, with no view
    hook of any kind involved."""
    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", message, cwd=clone, env=_COMMIT_ENV)
    git("push", "--quiet", cwd=clone)


def add_decision(clone: str, name: str, *, entity_id: str = "acme-corp", as_of: str = "2026-08-01",
                 acl: list | None = None, mentions_entity_page: str = "Acme Corp") -> str:
    """`mentions_entity_page` is which entity page the new decision WIKILINKS, which since #85 is
    a second thing this fixture decides: a page linking `[[Acme Corp]]` is a backlink of
    `acme-corp`'s view whatever it is anchored to, so a test that means "exactly one entity
    changed" has to point the link at that entity's own page too."""
    path = os.path.join(clone, "wiki", "decisions", f"{name}.md")
    with open(path, "w") as f:
        f.write(decision_page(name, entity_id, as_of=as_of,
                              mentions_entity_page=mentions_entity_page, acl=acl))
    return path


def add_entity_page(clone: str, name: str, entity_id: str) -> None:
    from tests.views.conftest import entity_page
    with open(os.path.join(clone, "wiki", "entities", f"{name}.md"), "w") as f:
        f.write(entity_page(name, entity_id))


def sweep(clone: str, conn, registry, **kw) -> regenerate.RunResult:
    return asyncio.run(regenerate.sweep(clone, conn, registry=registry, **kw))


# ── D2: the population is a UNION, and neither existing target is a superset ────────────────────
def test_stale_alone_would_never_create_a_missing_view(repo):
    """The `Ferrovial Nexus` case, and the reason `--sweep` is not spelled `--stale`: an entity
    minted with one anchored page and no view is invisible to `list_stale_entities`, which
    iterates the views on DISK. A sweep built on the gardener's own `stale-view` population — the
    obvious reuse — would leave that entity with `"view": null` forever."""
    remote, clone = repo
    assert staleness.list_stale_entities(clone) == []          # RED: --stale cannot see it
    assert staleness.list_all_anchored_entities(clone) == ["acme-corp"]
    assert staleness.list_sweep_entities(clone) == ["acme-corp"]

    conn = FakeConn()
    result = sweep(clone, conn, registry_of())

    assert result.stats["written"] == 1
    assert "views/acme-corp.md" in remote_files(remote)


def test_all_alone_would_never_remove_an_orphaned_view(tmp_path):
    """The other half: once the last anchored page is gone the entity has no anchored pages left,
    so `list_all_anchored_entities` cannot name it — the view would stay committed, indexed and
    searchable with no members behind it."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))

    os.remove(os.path.join(clone, "wiki", "decisions", "decision-1.md"))
    os.remove(os.path.join(clone, "wiki", "entities", "Acme Corp.md"))
    commit_all(clone, "chore: the last anchored page goes (an applied `delete` repair)")

    assert staleness.list_all_anchored_entities(clone) == []   # RED: --all cannot see it
    assert staleness.list_stale_entities(clone) == ["acme-corp"]
    assert staleness.list_sweep_entities(clone) == ["acme-corp"]

    result = sweep(clone, FakeConn(), registry)

    assert result.stats["removed"] == 1
    assert "views/acme-corp.md" not in remote_files(remote)


def test_a_deregistered_entitys_orphaned_view_is_removed(repo):
    """De-registration is the case NEITHER population's own name suggests: the pages still anchor
    the entity, so its member hash still MATCHES and `--stale` does not name it either. `--all`
    does, and `regenerate_entity` turns the missing registry row into a removal."""
    remote, clone = repo
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry_of()))
    assert "views/acme-corp.md" in remote_files(remote)

    deregistered = Registry()
    assert staleness.list_stale_entities(clone) == []          # the member hash still matches
    assert "acme-corp" in staleness.list_sweep_entities(clone)

    result = sweep(clone, FakeConn(), deregistered)

    assert result.stats["removed"] == 1
    assert "views/acme-corp.md" not in remote_files(remote)


def test_the_union_is_exactly_both_halves_and_is_deduplicated(tmp_path):
    """One entity in each half at once, so the union is proven to be a union and not either half
    with an extra case bolted on — and an entity in BOTH halves appears once."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    # `ghost-co` gets a view and then loses every member: --stale's half.
    add_entity_page(clone, "Ghost Co", "ghost-co")
    commit_all(clone, "chore: add ghost-co")
    registry.entities["ghost-co"] = {"name": "Ghost Co", "type": "organization", "aliases": []}
    asyncio.run(regenerate.regenerate_entity(clone, "ghost-co", registry=registry))
    os.remove(os.path.join(clone, "wiki", "entities", "Ghost Co.md"))
    commit_all(clone, "chore: ghost-co loses its only page")

    assert staleness.list_all_anchored_entities(clone) == ["acme-corp"]   # --all's half only
    assert staleness.list_stale_entities(clone) == ["ghost-co"]           # --stale's half only
    assert staleness.list_sweep_entities(clone) == ["acme-corp", "ghost-co"]

    # acme-corp is now in BOTH halves (anchored, and its view is stale) — still listed once.
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    add_decision(clone, "decision-2")
    commit_all(clone, "chore: a second page anchors acme-corp")
    assert staleness.list_sweep_entities(clone) == ["acme-corp", "ghost-co"]


# ── the property: a view is never stale, whatever wrote the page ───────────────────────────────
def test_an_ordinary_capture_refreshes_the_view_without_any_hook(repo):
    """An ordinary `brain_submit` (or Slack drop, or Drive drop) files a page and calls NOTHING —
    the sweep still converges, because it reads the corpus rather than remembering a call site."""
    remote, clone = repo
    registry = registry_of()
    sweep(clone, FakeConn(), registry)
    before = remote_log(remote)

    add_decision(clone, "decision-3", as_of="2026-08-05")
    commit_all(clone, "feat: a page an ordinary capture filed")

    result = sweep(clone, FakeConn(), registry)

    assert result.stats["written"] == 1
    assert remote_log(remote) != before
    page = open(os.path.join(clone, "views", "acme-corp.md")).read()
    assert "decision-3" in page


def test_an_applied_repair_that_deletes_a_page_refreshes_the_view(tmp_path):
    """The `delete` repair kind removes pages from the corpus in the HTTP server process, which
    must not regenerate anything. The sweep is what closes it — and this is the one door that can
    take a member set from N to N-1 without anybody writing a page."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2)
    registry = registry_of()
    sweep(clone, FakeConn(), registry)
    assert "decision-2" in open(os.path.join(clone, "views", "acme-corp.md")).read()

    os.remove(os.path.join(clone, "wiki", "decisions", "decision-2.md"))
    commit_all(clone, "chore(repair): apply an approved deletion")

    result = sweep(clone, FakeConn(), registry)

    assert result.stats["written"] == 1
    assert "decision-2" not in open(os.path.join(clone, "views", "acme-corp.md")).read()


# ── the benign twin, and it is the COST property ───────────────────────────────────────────────
def test_a_converged_corpus_produces_no_commit_and_no_model_call(repo, monkeypatch):
    """"It regenerated everything every night" is how this feature turns into a bill, so BOTH
    halves are asserted: not one commit, and not one synthesis call. `unchanged` is decided from
    the member hash before any agent is built, which is the whole reason a periodic pass is
    affordable at all."""
    remote, clone = repo
    registry = registry_of()
    sweep(clone, FakeConn(), registry)
    log_before = remote_log(remote)

    calls = []
    real = synthesis.write_synthesis

    async def counting(*a, **kw):
        calls.append(a[1] if len(a) > 1 else None)
        return await real(*a, **kw)

    monkeypatch.setattr(regenerate.synthesis, "write_synthesis", counting)

    result = sweep(clone, FakeConn(), registry)

    assert result.stats["unchanged"] == 1
    assert result.stats["written"] == 0
    assert calls == [], "a converged corpus must cost zero model calls"
    assert remote_log(remote) == log_before, "a converged corpus must cost zero commits"


def _count_parses(monkeypatch) -> list:
    from stigmergy.index import corpus
    parses = []
    real = corpus.load_pages

    def counting(repo_path, *a, **kw):
        parses.append(repo_path)
        return real(repo_path, *a, **kw)

    monkeypatch.setattr(corpus, "load_pages", counting)
    return parses


def test_a_converged_sweep_parses_the_corpus_exactly_once(tmp_path, monkeypatch):
    """The population scan and every entity's member set come off ONE parse — the case that
    decides whether a periodic pass is affordable, since a converged corpus is what almost every
    pass finds. `views/` is deliberately excluded from `skeleton.MEMBER_ZONES`, so nothing this
    loop commits can change a member set and the shared parse cannot go stale under it."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    for name in ("Globex", "Initech"):
        add_entity_page(clone, name, name.lower())
        registry.entities[name.lower()] = {"name": name, "type": "organization", "aliases": []}
    commit_all(clone, "chore: three anchored entities")
    sweep(clone, FakeConn(), registry)                    # everything converged

    parses = _count_parses(monkeypatch)
    result = sweep(clone, FakeConn(), registry)

    assert result.stats["unchanged"] == 3
    assert len(parses) == 1, f"expected ONE corpus parse for a converged sweep, got {len(parses)}"


def test_only_an_entity_actually_being_rewritten_pays_a_second_parse(tmp_path, monkeypatch):
    """The shared parse stops at the member set on purpose: `skeleton.backlinks_of` scans every
    indexed zone INCLUDING `views/`, so a view written earlier in the same pass is a legitimate
    backlink source and a shared parse would silently drop it. That fresh parse is therefore paid
    per REGENERATED entity — never per entity checked, which is what would make the pass
    unaffordable."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    for name in ("Globex", "Initech"):
        add_entity_page(clone, name, name.lower())
        registry.entities[name.lower()] = {"name": name, "type": "organization", "aliases": []}
    commit_all(clone, "chore: three anchored entities")
    sweep(clone, FakeConn(), registry)

    add_decision(clone, "decision-9", entity_id="globex", as_of="2026-08-09",
                 mentions_entity_page="Globex")
    commit_all(clone, "feat: exactly one entity changed")

    parses = _count_parses(monkeypatch)
    result = sweep(clone, FakeConn(), registry)

    assert result.stats["written"] == 1 and result.stats["unchanged"] == 2
    assert len(parses) == 2, "one shared parse for the population, one backlinks parse for the "\
                             f"single rewritten entity — got {len(parses)}"


# ── D8: the per-run ceiling, and what it says it deferred ──────────────────────────────────────
def test_the_ceiling_stops_the_pass_and_records_what_it_deferred(tmp_path):
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    add_entity_page(clone, "Globex", "globex")
    registry.entities["globex"] = {"name": "Globex", "type": "organization", "aliases": []}
    commit_all(clone, "chore: a second entity")

    result = sweep(clone, FakeConn(), registry, max_changes=1)

    assert result.stats["written"] == 1
    assert result.stats["population"] == 2
    assert result.stats["checked"] == 1
    assert result.stats["deferred"] == 1
    assert len(result.skip_reasons) == 1
    # The repair loop's own wording pattern, so an operator learns ONE spelling of this fact.
    assert result.skip_reasons[0].startswith("run-ceiling-reached(1):")
    assert "the next run will see them" in result.skip_reasons[0]
    assert remote_files(remote).count("views/globex.md") == 0


def test_the_next_pass_picks_up_what_the_ceiling_deferred(tmp_path):
    """The surplus is not lost, and the mechanism is the population being recomputed from STATE:
    whatever the ceiling deferred is still divergent next time, while what was done is now free."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    add_entity_page(clone, "Globex", "globex")
    registry.entities["globex"] = {"name": "Globex", "type": "organization", "aliases": []}
    commit_all(clone, "chore: a second entity")

    sweep(clone, FakeConn(), registry, max_changes=1)
    second = sweep(clone, FakeConn(), registry, max_changes=1)

    assert second.stats["written"] == 1
    assert second.stats["unchanged"] == 1        # the first pass's entity now costs nothing
    assert second.skip_reasons == []
    assert "views/globex.md" in remote_files(remote)


def test_a_ceiling_that_is_never_reached_says_nothing(repo):
    """Specificity, not just sensitivity: a pass that fit inside its ceiling must not report a
    deferral, or an operator goes hunting for entities that do not exist."""
    remote, clone = repo
    result = sweep(clone, FakeConn(), registry_of(), max_changes=5)
    assert result.skip_reasons == []
    assert result.stats["deferred"] == 0


def test_the_ceiling_counts_regenerations_not_entities_examined(tmp_path):
    """An `unchanged` entity costs a hash and no model call, so it must not consume the ceiling —
    otherwise a corpus with more entities than the ceiling would leave the tail of the alphabet
    permanently unconverged however little was actually changing."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    for name in ("Globex", "Initech"):
        add_entity_page(clone, name, name.lower())
        registry.entities[name.lower()] = {"name": name, "type": "organization", "aliases": []}
    commit_all(clone, "chore: three anchored entities")
    sweep(clone, FakeConn(), registry)                    # everything converged

    add_decision(clone, "decision-9", entity_id="initech", as_of="2026-08-09",
                 mentions_entity_page="Initech")
    commit_all(clone, "feat: only the LAST entity alphabetically changed")

    result = sweep(clone, FakeConn(), registry, max_changes=1)

    assert result.stats["checked"] == 3                   # walked past two free no-ops
    assert result.stats["written"] == 1
    assert result.skip_reasons == []


# ── ACL: pinned HERE, because this change adds a caller ────────────────────────────────────────
def test_a_sweep_over_labelled_members_writes_an_OPEN_view_without_them(tmp_path):
    """A view carries no label and renders open members only. Pinned on the SWEEP
    path rather than trusted from `regenerate_entity`'s own test, because the periodic pass is an
    unattended caller of it — and this is the pass that would silently republish the retired
    behaviour across the whole corpus."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2,
                               decision_acls=[["a"], ["b"]])

    result = sweep(clone, FakeConn(), registry_of())

    assert result.outcomes[0].acl is None
    page = open(os.path.join(clone, "views", "acme-corp.md")).read()
    assert "acl:" not in page.split("---\n\n")[0]
    assert "Decision 1" not in page and "Decision 2" not in page


# ── the job_runs row the pass leaves behind ────────────────────────────────────────────────────
def test_the_sweep_records_itself_under_its_own_job_name(repo):
    """A maintenance pass, an operator's `regenerate` and the post-meeting hook each get their own
    `job_runs.job`, so a run's history says which of the three did the work."""
    remote, clone = repo
    conn = FakeConn()
    sweep(clone, conn, registry_of())

    jobs = [params[0] for _sql, params in conn.executed]
    assert regenerate.SWEEP_JOB_NAME in jobs
    assert regenerate.SWEEP_JOB_NAME == "views-sweep"


def test_a_fault_mid_sweep_leaves_an_error_row_and_re_raises(repo, monkeypatch):
    """`views.regenerate.run` owns the error row; the caller's job is only to decide whether the
    fault stops it. Pinned here so the worker's "recorded and swallowed" posture rests on a row
    that actually gets written."""
    remote, clone = repo

    async def boom(*a, **kw):
        raise RuntimeError("synthesis backend exploded")

    monkeypatch.setattr(regenerate, "regenerate_entity", boom)
    conn = FakeConn()
    with pytest.raises(RuntimeError):
        sweep(clone, conn, registry_of())

    # The `job_runs` write, found by its shape rather than by being LAST: the sweep's advisory
    # lock releases on the way out, so the final statement on this connection is the unlock.
    job_writes = [params for _sql, params in conn.executed if len(params) == 4]
    assert len(job_writes) == 1
    job, status, _stats, error = job_writes[-1]
    assert job == regenerate.SWEEP_JOB_NAME
    assert status == "error"
    assert error == "RuntimeError"      # the CLASS name; a message can carry captured content
