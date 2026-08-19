"""Whether the pass actually CONVERGES, over more than one pass and more than one entity.

`tests/views/test_sweep.py` proves each rule in isolation on a one- or two-entity corpus. The
properties here need a population and a sequence, because they are the ones a design like this
fails at silently:

- **starvation** — a ceiling that always defers the same tail leaves those entities permanently
  stale while every pass reports success. That is the failure mode this whole feature exists to
  prevent, arriving through its own bound.
- **cost across passes** — "no model call when nothing changed" is only worth anything if it also
  holds for the entities a PREVIOUS pass converged. A ceiling that re-billed them would turn N
  entities into N model calls per interval forever.
- **the single-parse optimisation and the place it stops** — `regenerate_entity` shares the
  population's parse for the MEMBER set and deliberately does not share it for `backlinks_of`,
  because `views/` is an indexed zone and a view written earlier in the same pass is a real
  backlink source. That reasoning is a comment in `regenerate.py`; here it is a behaviour.
- **the benign twin and the ACL rule**, re-derived independently of `test_sweep.py`: the twin at
  the remote tip (a commit counter git itself keeps) over a multi-entity corpus, and the ACL rule
  with its own benign twin, since a defense tested only where it fires measures sensitivity and
  never specificity.
"""
import asyncio
import os

import pytest

from stigmergy.kernel.registry import Registry
from stigmergy.views import regenerate, synthesis
from tests.views.conftest import (
    FakeConn,
    build_repo,
    entity_page,
    git,
    registry_of,
    remote_files,
    remote_log,
)

_COMMIT_ENV = {"GIT_AUTHOR_NAME": "Test Steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
               "GIT_COMMITTER_NAME": "Test Steward", "GIT_COMMITTER_EMAIL": "steward@example.com"}


def _commit_all(clone: str, message: str) -> None:
    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", message, cwd=clone, env=_COMMIT_ENV)
    git("push", "--quiet", cwd=clone)


def _sweep(clone, registry, **kw) -> regenerate.RunResult:
    return asyncio.run(regenerate.sweep(clone, FakeConn(), registry=registry, **kw))


def _count_syntheses(monkeypatch) -> list:
    """The MODEL-CALL counter, at the one seam a view's synthesis goes through. Counting commits
    is not a substitute: a removal commits without calling a model, and a withheld synthesis calls
    one without producing a view."""
    calls = []
    real = synthesis.write_synthesis

    async def counting(*a, **kw):
        calls.append(a)
        return await real(*a, **kw)

    monkeypatch.setattr(regenerate.synthesis, "write_synthesis", counting)
    return calls


def _views_on_remote(remote: str) -> set[str]:
    return {f for f in remote_files(remote) if f.startswith("views/")}


def _population_far_above_the_ceiling(tmp_path, n: int = 7):
    """`n` registered entities, each with its own self-anchored page and no view — the state a
    deployment that has just enabled the sweep is in, and the state #76's `Ferrovial Nexus`
    evidence describes at n=1."""
    remote, clone = build_repo(str(tmp_path / "git"), entity_id="ent-a", entity_name="Ent A",
                               n_decisions=0)
    registry = Registry()
    ids = [f"ent-{chr(ord('a') + i)}" for i in range(n)]
    for entity_id in ids:
        name = f"Ent {entity_id[-1].upper()}"
        registry.entities[entity_id] = {"name": name, "type": "organization", "aliases": []}
        with open(os.path.join(clone, "wiki", "entities", f"{name}.md"), "w") as f:
            f.write(entity_page(name, entity_id))
    _commit_all(clone, f"chore: {n} entities, none of them with a view")
    return remote, clone, registry, ids


# ── starvation: the silent failure a per-run ceiling can have ──────────────────────────────────
def test_consecutive_passes_make_monotonic_progress_and_starve_nobody(tmp_path):
    """**Seven entities, a ceiling of two, run to convergence.**

    A ceiling is a bound on work, and a bound on work applied to a population sorted the same way
    every time is how a tail gets deferred forever. The population is recomputed from STATE, so
    what a pass converged is free next time and the frontier advances — but that is an argument,
    and the argument is worth exactly as much as the run that demonstrates it.

    Asserted: every pass writes something until there is nothing left (no pass stalls), the set of
    converged entities only ever grows, and the entity that sorts LAST — the starvation candidate,
    since the loop walks the population in sorted order — has its view by the end.
    """
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=7)
    ceiling = 2

    converged: list[set[str]] = []
    written_per_pass: list[int] = []
    for _ in range(4):                       # ceil(7 / 2) = 4 passes to converge seven entities
        result = _sweep(clone, registry, max_changes=ceiling)
        written_per_pass.append(result.stats["written"])
        converged.append(_views_on_remote(remote))

    assert written_per_pass == [2, 2, 2, 1], (
        f"a pass that wrote nothing while entities were still divergent is a starved population: "
        f"{written_per_pass}")
    for earlier, later in zip(converged, converged[1:], strict=False):
        assert earlier < later, "a pass must never lose ground a previous pass gained"
    assert converged[-1] == {f"views/{i}.md" for i in ids}
    # The last id alphabetically is the one a fixed-order ceiling would defer forever.
    assert f"views/{ids[-1]}.md" in converged[-1]


def test_a_pass_never_re_bills_an_entity_an_earlier_pass_converged(tmp_path, monkeypatch):
    """The cost half of the same property, and the one that decides whether a periodic pass is
    affordable: seven entities converged over four ceilinged passes must cost SEVEN model calls in
    total, not seven per pass. A ceiling that counted entities examined instead of regenerations
    would keep re-reaching the same prefix and never reach the tail — and would still bill for it,
    because `--force` is the only thing that should ever re-synthesize a converged entity."""
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=7)
    calls = _count_syntheses(monkeypatch)

    for _ in range(5):                       # one more pass than convergence needs
        _sweep(clone, registry, max_changes=2)

    assert len(calls) == 7, (
        f"seven entities converged over five passes cost {len(calls)} model calls — a converged "
        f"entity must cost a hash and nothing else")
    assert _views_on_remote(remote) == {f"views/{i}.md" for i in ids}


def test_a_fully_converged_population_reports_no_deferral_however_large_it_is(tmp_path):
    """Specificity for the ceiling on a population that is BIGGER than it: seven entities, ceiling
    two, everything already current. Nothing is billed, so nothing is deferred and the operator is
    told nothing — a "run-ceiling-reached" on a converged corpus would send somebody hunting."""
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=7)
    for _ in range(4):
        _sweep(clone, registry, max_changes=2)

    result = _sweep(clone, registry, max_changes=2)

    assert result.stats["unchanged"] == 7
    assert result.stats["checked"] == result.stats["population"] == 7
    assert result.stats["deferred"] == 0
    assert result.skip_reasons == []


# ── the benign twin, re-derived at git's own counter ───────────────────────────────────────────
def test_a_converged_multi_entity_corpus_moves_neither_the_remote_tip_nor_a_model(tmp_path,
                                                                                  monkeypatch):
    """"It regenerated everything every night" is how this feature becomes a bill, so both
    counters are somebody else's: the model call counted at `synthesis.write_synthesis`, and the
    commit counted by the REMOTE's own tip sha — not a return value the code under test computes
    about itself.

    Deliberately over seven entities rather than one: a per-entity no-op that quietly rewrote an
    unchanged file would show up as a commit here and not on a corpus of one.
    """
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=7)
    _sweep(clone, registry)                                   # first pass: everything is written
    tip_before = git("rev-parse", "main", cwd=remote).stdout.strip()
    log_before = remote_log(remote)

    calls = _count_syntheses(monkeypatch)
    result = _sweep(clone, registry)

    assert result.stats["unchanged"] == 7
    assert result.stats["written"] == 0 and result.stats["removed"] == 0
    assert calls == [], "a converged corpus must cost zero model calls"
    assert git("rev-parse", "main", cwd=remote).stdout.strip() == tip_before
    assert remote_log(remote) == log_before


# ── ACL: the intersection rule, with the twin that proves it is an intersection ────────────────
def test_a_sweep_over_disjoint_audiences_writes_acl_nobody(tmp_path):
    """`acl: []` is "nobody", never "open" — a rollup must not widen access to what it summarizes.
    Pinned on the SWEEP path because the periodic pass is a new, unattended caller of
    `regenerate_entity`, and asserted on the FILE as well as on the outcome: the frontmatter is
    what `server.acl.visible()` will read, and an outcome field agreeing with itself proves
    nothing about what was committed."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2,
                               decision_acls=[["finance"], ["legal"]])

    result = _sweep(clone, registry_of())

    assert result.outcomes[0].acl == []
    view = open(os.path.join(clone, "views", "acme-corp.md")).read()
    assert "acl: []" in view
    # And nothing leaked the members' own labels into the rollup's audience.
    assert "finance" not in view.split("---")[1]
    assert "legal" not in view.split("---")[1]


def test_a_sweep_over_overlapping_audiences_writes_the_intersection_not_nobody(tmp_path):
    """The benign twin. A rule tested only where it produces `[]` is indistinguishable from "the
    sweep always writes `acl: []`", which would make every view invisible to everyone and read as
    a passing security test. Two members sharing one label must yield exactly that label."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2,
                               decision_acls=[["finance", "legal"], ["legal"]])

    result = _sweep(clone, registry_of())

    assert result.outcomes[0].acl == ["legal"]
    assert "acl: [legal]" in open(os.path.join(clone, "views", "acme-corp.md")).read()


# ── the single parse, and the line it deliberately stops at ────────────────────────────────────
def test_a_view_written_earlier_in_the_same_pass_is_a_backlink_of_a_later_one(tmp_path):
    """**Why `rows` is threaded into `members_of` and NOT into `backlinks_of`.**

    The member-set parse is safe to share across a batch because `skeleton.MEMBER_ZONES` excludes
    `views/`, so nothing the loop commits can change any member set. `backlinks_of` scans every
    INDEXED zone, `views/` included — so a view this same pass wrote a moment ago is a legitimate
    backlink source, and a shared parse taken before the pass started could not contain it.

    The rig makes that concrete rather than hypothetical: `Beta Co`'s own entity page is anchored
    to `alpha-co` as well, so `alpha-co`'s view (written first, the population is sorted) links
    `[[Beta Co]]`, which resolves to Beta's entity page — a real inbound link that exists only
    because of a commit made earlier in this same pass.
    """
    remote, clone = build_repo(str(tmp_path / "git"), entity_id="alpha-co",
                               entity_name="Alpha Co", n_decisions=1)
    with open(os.path.join(clone, "wiki", "entities", "Beta Co.md"), "w") as f:
        f.write(entity_page("Beta Co", "beta-co").replace("entity: [beta-co]",
                                                          "entity: [beta-co, alpha-co]"))
    _commit_all(clone, "chore: Beta Co's own page is also anchored to Alpha Co")
    registry = Registry()
    for entity_id, name in (("alpha-co", "Alpha Co"), ("beta-co", "Beta Co")):
        registry.entities[entity_id] = {"name": name, "type": "organization", "aliases": []}

    result = _sweep(clone, registry)

    assert result.stats["written"] == 2
    beta_view = open(os.path.join(clone, "views", "beta-co.md")).read()
    assert "views/alpha-co.md" in beta_view, (
        "beta-co's Backlinks section lost the view alpha-co got earlier in this same pass — the "
        "population's shared parse must not reach `backlinks_of`")


def test_a_removal_earlier_in_the_same_pass_leaves_no_stale_backlink_behind(tmp_path):
    """The other direction of the same argument: a view REMOVED earlier in the pass must not still
    be cited by a view REGENERATED later in it. Same reason — the fresh parse — from the side where
    a shared parse would over-report instead of under-reporting, which is the worse half: a
    dangling `[[...]]` in a generated page.

    `decision-1` is anchored to both entities, so deleting it changes beta-co's member set too and
    beta-co is genuinely regenerated in the same pass that removes alpha-co's view.
    """
    remote, clone = build_repo(str(tmp_path / "git"), entity_id="alpha-co",
                               entity_name="Alpha Co", n_decisions=1)
    with open(os.path.join(clone, "wiki", "entities", "Beta Co.md"), "w") as f:
        f.write(entity_page("Beta Co", "beta-co").replace("entity: [beta-co]",
                                                          "entity: [beta-co, alpha-co]"))
    decision = os.path.join(clone, "wiki", "decisions", "decision-1.md")
    with open(decision) as f:
        text = f.read()
    with open(decision, "w") as f:
        f.write(text.replace("entity: [alpha-co]", "entity: [alpha-co, beta-co]"))
    _commit_all(clone, "chore: one page anchored to both entities")
    registry = Registry()
    for entity_id, name in (("alpha-co", "Alpha Co"), ("beta-co", "Beta Co")):
        registry.entities[entity_id] = {"name": name, "type": "organization", "aliases": []}
    _sweep(clone, registry)
    assert "views/alpha-co.md" in open(os.path.join(clone, "views", "beta-co.md")).read()

    # alpha-co is de-registered (its view is REMOVED first, the population is sorted) and the
    # shared page is deleted, so beta-co's member set changes and beta-co is rewritten after it.
    os.remove(decision)
    _commit_all(clone, "chore(repair): a deletion that also changes beta's member set")
    del registry.entities["alpha-co"]

    result = _sweep(clone, registry)

    assert result.stats["removed"] == 1 and result.stats["written"] == 1
    assert "views/alpha-co.md" not in remote_files(remote)
    assert "views/alpha-co.md" not in open(os.path.join(clone, "views", "beta-co.md")).read(), (
        "beta-co's Backlinks section still cites a view removed earlier in this same pass — the "
        "backlink parse has to be taken fresh, per regenerated entity")


# ── an interrupted pass leaves a coherent repo ─────────────────────────────────────────────────
def test_an_interrupted_pass_leaves_every_view_it_did_commit_coherent(tmp_path, monkeypatch):
    """There is no cancellation point INSIDE an entity — the cooperative stop only acts between
    them — so the safety argument for any harder interruption is not "it stops cleanly" but "one
    commit per entity means any prefix of it is a valid repo state". A `KeyboardInterrupt` at the
    third entity must leave the first two views complete — frontmatter, `member_hash` and all —
    and the rest simply absent, which is exactly what the next pass's population is derived
    from."""
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=5)
    real = regenerate.regenerate_entity
    seen: list[str] = []

    async def interrupt_at_the_third(repo, entity_id, **kw):
        if len(seen) == 2:
            raise KeyboardInterrupt
        seen.append(entity_id)
        return await real(repo, entity_id, **kw)

    monkeypatch.setattr(regenerate, "regenerate_entity", interrupt_at_the_third)
    try:
        _sweep(clone, registry)
    except KeyboardInterrupt:
        pass

    assert _views_on_remote(remote) == {f"views/{ids[0]}.md", f"views/{ids[1]}.md"}
    for entity_id in ids[:2]:
        view = open(os.path.join(clone, "views", f"{entity_id}.md")).read()
        assert view.startswith("---\n") and "member_hash:" in view

    monkeypatch.setattr(regenerate, "regenerate_entity", real)
    result = _sweep(clone, registry)

    assert result.stats["written"] == 3 and result.stats["unchanged"] == 2
    assert _views_on_remote(remote) == {f"views/{i}.md" for i in ids}


# ── what the staleness definition does NOT cover, and #76 is what makes that matter ────────────
_BACKLINK_SOURCE = ('---\ntype: decision\ntitle: "Project Nightingale"\nas_of: "2026-08-02"\n'
                    'created: "2026-08-02"\nupdated: "2026-08-02"\nstatus: developing\n{acl}'
                    'tags: [decision]\n---\n\n# Project Nightingale\n\nSee [[Acme Corp]].\n')


@pytest.mark.parametrize("how", ["deleted", "restricted"])
def test_a_backlink_that_stopped_qualifying_survives_a_convergence_pass(tmp_path, how):
    """**CHARACTERIZATION of a known defect — issue #85. This test asserts what the system DOES,
    which is not what it should do.**

    A view keeps citing a backlink that has stopped qualifying, and no pass will notice, because
    staleness is defined solely by `member_hash` and a backlink source is not a member. Both halves
    below are real and neither is fixed here:

    - `deleted` — the `delete` repair kind removes the backlink source and the view keeps a
      dangling `[[...]]` pointing at a path that no longer exists. The gardener's `dead-link`-shaped
      checks cannot see it: they read the corpus, where the link is real until the view is
      regenerated.
    - `restricted` — a steward narrows the source's `acl`. `skeleton.backlinks_of` gates every
      backlink through `kernel.acl.visible_to_view` **at generation time only**, so the
      already-committed view keeps that page's STEM and PATH (`render_backlinks` links by file
      stem) readable by everyone the view is readable by. That is in tension with this system's own
      existence-leak rule (an unknown page and a forbidden page return the same string,
      deliberately) and with the invariant #76 and ADR 021 both state — a view "cannot widen access
      to what it summarizes".

    **Why it is characterized here and not fixed here.** It is PRE-EXISTING: both halves are as old
    as `backlinks_of` and neither was introduced by the convergence pass, which only made them
    permanent rather than eventual. The honest fix — folding a backlink signal (path + acl per row)
    into the staleness comparison, or recording a `backlink_hash` beside `member_hash` — makes
    every backlink change a regeneration, which is a model call per affected entity per pass, and
    that cost argument deserves its own change rather than a rider on this one.

    **What would make this test change**: #85 landing. When a backlink signal enters the staleness
    definition these assertions invert — the pass reports `written` instead of `unchanged` and the
    citation is gone — and this test becomes the one the tester wrote first, which is preserved
    verbatim in the issue. Until then the last block below is the whole recovery: `--force`, an
    operator's act, which is what `docs/reference/views.md`'s limits section documents.
    """
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    source = os.path.join(clone, "wiki", "decisions", "nightingale.md")
    with open(source, "w") as f:
        f.write(_BACKLINK_SOURCE.format(acl=""))
    _commit_all(clone, "chore: a non-member page that links to the entity page")
    _sweep(clone, registry_of())
    assert "wiki/decisions/nightingale.md" in open(
        os.path.join(clone, "views", "acme-corp.md")).read()

    if how == "deleted":
        os.remove(source)
    else:
        with open(source, "w") as f:
            f.write(_BACKLINK_SOURCE.format(acl="acl: [board-only]\n"))
    _commit_all(clone, f"chore: the backlink source is {how}")

    result = _sweep(clone, registry_of())

    # The defect, stated as behaviour: the member set did not change, so the pass has nothing to
    # compare the Backlinks section against and reports the view as current.
    assert result.stats["unchanged"] == 1 and result.stats["written"] == 0
    view = open(os.path.join(clone, "views", "acme-corp.md")).read()
    assert "[[nightingale]]" in view, "the citation the pass should have dropped (#85)"
    assert "wiki/decisions/nightingale.md" in view, (
        "and its path — which for the `restricted` case is the existence leak, not just staleness")

    # And the recovery that does exist, which is what keeps this a limit rather than a trap.
    forced = _sweep(clone, registry_of(), force=True)
    assert forced.stats["written"] == 1
    view = open(os.path.join(clone, "views", "acme-corp.md")).read()
    assert "nightingale" not in view

# ── the cooperative stop, and the mid-batch rebase guard ───────────────────────────────────────
class _StopAfter:
    """A `should_stop` that answers False `n` times and True forever after — the shape
    `Worker._stop_requested` takes when a signal lands mid-pass."""

    def __init__(self, n: int):
        self.remaining = n

    def __call__(self) -> bool:
        self.remaining -= 1
        return self.remaining < 0


def test_a_stop_requested_mid_pass_stops_between_entities_with_the_remainder_deferred(tmp_path):
    """The cooperative shutdown, made to FIRE against the real loop. Until now nothing in the
    suite called `run` with a `should_stop` that ever flipped, so "a signal is honoured between
    entities" was a docstring — and the shutdown-delay bound it buys (ONE entity's regeneration,
    not a whole ceiling's worth) was claimed in two documents and proven nowhere."""
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=5)

    result = _sweep(clone, registry, should_stop=_StopAfter(1))

    assert result.stats["written"] == 1, "the entity in flight completes; no new one starts"
    (reason,) = result.skip_reasons
    assert reason == regenerate.SHUTTING_DOWN_REASON.format(deferred=4)
    assert len(_views_on_remote(remote)) == 1


def test_a_stop_flag_that_never_flips_never_truncates_a_pass(tmp_path):
    """The benign twin: the deployed worker hands `should_stop` on EVERY pass, so the flag's mere
    presence must cost nothing — only its answer may."""
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=3)

    result = _sweep(clone, registry, should_stop=lambda: False)

    assert result.stats["written"] == 3
    assert result.skip_reasons == []
    assert len(_views_on_remote(remote)) == 3


def test_a_foreign_commit_landing_mid_batch_stops_the_batch_with_the_remainder_deferred(
        tmp_path, monkeypatch):
    """The third early stop, driven end to end: a commit from OUTSIDE the run lands on the branch
    while the batch is pushing, the next push rebases onto it, and the batch must stop — the
    shared corpus parse now describes a tree that is gone, and every remaining entity would be
    summarized off post-rebase bytes under a pre-rebase member set. The race is real: a second
    clone pushes between entities, exactly what a capture filing or an applied repair does."""
    remote, clone, registry, ids = _population_far_above_the_ceiling(tmp_path, n=4)
    racer = os.path.join(str(tmp_path), "racer")
    git("clone", "--quiet", remote, racer, cwd=str(tmp_path))
    real = regenerate.regenerate_entity
    calls: list[str] = []

    async def racing(repo, entity_id, **kw):
        outcome = await real(repo, entity_id, **kw)
        calls.append(entity_id)
        if len(calls) == 1:
            os.makedirs(os.path.join(racer, "wiki", "notes"), exist_ok=True)
            with open(os.path.join(racer, "wiki", "notes", "raced.md"), "w") as f:
                f.write("---\ntype: note\ntitle: Raced\n---\n\n# Raced\n")
            git("add", "--all", cwd=racer)
            git("commit", "--quiet", "-m", "feat: a foreign commit mid-batch", cwd=racer,
                env=_COMMIT_ENV)
            # The racer clones before the batch starts, so entity 1's own push has already moved
            # the branch under it — rebase onto the tip first, as any real racer would.
            git("pull", "--quiet", "--rebase", cwd=racer, env=_COMMIT_ENV)
            git("push", "--quiet", cwd=racer)
        return outcome

    monkeypatch.setattr(regenerate, "regenerate_entity", racing)

    result = _sweep(clone, registry)

    # Entity 1 pushed clean; the racer then moved the branch; entity 2's push rebased — and THAT
    # is where the batch must stop: two written, two deferred, said out loud.
    assert result.stats["written"] == 2
    assert len(calls) == 2
    (reason,) = result.skip_reasons
    assert reason == regenerate.BRANCH_MOVED_REASON.format(entity_id=ids[1], deferred=2)
    assert len(_views_on_remote(remote)) == 2

