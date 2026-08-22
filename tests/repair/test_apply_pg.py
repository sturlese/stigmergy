"""`apply_in_tree` and `apply_and_record` against a REAL bare remote: real git, real gates, real
gitleaks.

Nothing here is faked. A faked diff would prove nothing about `gate_body_rewrite`, a faked
gitleaks nothing about the secrets veto, and a faked remote nothing about the push — and those
three are the whole of what stands between a derived repair and `main`.

**Nobody reads a repair before it lands** (ADR 044), which is what raised the stakes on every
property below: what used to be an Approve button is now the ledger's memory, two ceilings and
these gates, and the reading happens afterwards from the stored diff. The harness is the worker's
own — `gitcmd.ephemeral_worktree` off the fixture checkout, one tree per repair, detached at a base
just fetched — and a repair is a plain dict, derived by `run.py` in production and built by hand
here so one property can be exercised at a time.

The two twins this file exists for are the ones that must be observed REFUSING:

  · a repair whose `ops` disagree with its own `target_paths`, so the diff touches a page the
    derivation never named. The gates pass it happily — it is additive and well-formed — and only
    the second stored fact (`target_paths`) can say it is not the change this repair declared.
  · a note carrying a credential, which the secrets gate must veto at apply time even though the
    repair validated cleanly when it was derived, because a `note` is free text that becomes a line
    on a page.

Each has a benign twin beside it, because a check that only ever fires measures its sensitivity
and never its specificity — and both of these can bounce a night's real work.
"""
import contextlib
import datetime
import os

import pytest

from stigmergy.librarian import gitcmd, githubapp
from stigmergy.librarian import page as page_policy
from stigmergy.repair import (
    apply,
    deletion,
    entity_alias,
    entity_body,
    schema,
    store,
)
from stigmergy.repair.errors import ProposalStateError, RepairError
from tests import adversarial_payloads
from tests.librarian import support as librarian_support
from tests.repair import support

# A FIXTURE, not a `skipif`: on a laptop without gitleaks this skips and says so, and in CI it
# FAILS. The apply path's secrets veto is half of what this file exists to prove, and a check that
# stops running has to be impossible to miss.
pytestmark = pytest.mark.usefixtures("require_gitleaks")

# The commit author every repair carries, resolved through the same function the pass resolves it
# with. An EMPTY environment, deliberately: `_apply_accepted` hands `dict(os.environ)` over, and a
# machine that happened to carry App credentials would otherwise change what this file asserts.
APP_AUTHOR = githubapp.identity({})

# A person's name on a commit, for the ONE repair somebody performs rather than the worker deriving
# it — `brain_delete` (ADR 043 D2). Every other test here passes no actor at all, which is the
# point: nobody approved a derived repair, and the trailer says so.
ACTOR = "steward@example.com"


@contextlib.contextmanager
def _tree(repo_env):
    """One repair's worktree, made the way `run._apply_one` makes it: detached at a base fetched
    for this repair, off the worker's own checkout, and removed however the block ends."""
    base = gitcmd.base_ref(repo_env.repo, support.BRANCH)
    with gitcmd.ephemeral_worktree(repo_env.repo, base.sha) as tree:
        yield tree


def _apply(repo_env, repair, **over):
    with _tree(repo_env) as tree:
        return apply.apply_in_tree(tree, support.BRANCH, None, repair=repair, author=APP_AUTHOR,
                                   **over)


def _apply_and_record(conn, repo_env, repair, **over):
    with _tree(repo_env) as tree:
        return apply.apply_and_record(conn, tree, support.BRANCH, None, repair=repair,
                                      author=APP_AUTHOR, **over)


def _repair(ops, *, kind=schema.KIND_EDITS, finding_ids=(1,), check="model-unlinked-mention",
            rationale="the pages should point at each other", **over):
    """One derived repair, in the shape `run._apply_accepted` hands the applier.

    `target_paths` and `content_key` are DERIVED here exactly as they are there — never typed —
    so no fixture in this file can seed a repair whose two stored facts already disagree, which is
    the state the cross-check exists to catch and which several tests below then create on purpose.
    """
    return {"kind": kind, "ops": ops, "target_paths": schema.target_paths(ops),
            "rationale": rationale, "content_key": schema.content_key(ops, kind=kind),
            "run_id": 1, "finding_ids": list(finding_ids), "check": check,
            "model_id": "fixture", "finding_subjects": [], **over}


def _ledger(conn) -> dict:
    """The one row the ledger gained, whatever its outcome."""
    (row,) = store.recent(conn, limit=2)
    return row


# The link targets here are deliberately ASCII-stemmed. `page._yaml_list` emits a related-link
# scalar through `json.dumps` with `ensure_ascii=True`, so a link naming the fixture's accented
# page lands as `"[[Caf\\u00e9 …]]"` and the frozen contract linter — whose frontmatter parser is
# line-oriented and decodes no `\\uXXXX` escape — reads it as a dead link. That is a pre-existing
# defect on the LIBRARIAN's own declared-edit path (`page.with_related_link`), not this
# package's, and it is reported rather than worked around here: these fixtures simply do not
# exercise it, and `tests/repair/test_propose_pg.py` keeps the accented page where it belongs —
# proving a path with spaces and accents survives the prompt index.
BACKLINK_OPS = [{"op": "backlink", "path": support.NOTE_A,
                 "link": support.stem(support.DECISION), "note": ""}]
CALLOUT_OPS = [
    {"op": "contradiction", "path": support.NOTE_A, "link": support.stem(support.DECISION),
     "note": "these two disagree about what was decided"},
    {"op": "contradiction", "path": support.DECISION, "link": support.stem(support.NOTE_A),
     "note": "these two disagree about what was decided"},
]


# ── every subprocess this applier reaches is bounded ─────────────────────────────────────────
def test_the_apply_bounds_the_gates_subprocesses_and_the_push(conn, repo_env, monkeypatch):
    """Red before the fix: the `GateContext` this applier builds carried no subprocess budget and
    `gitcmd.push` accepted none, so a hung contract linter, a hung gitleaks or a stalled remote
    pinned whatever ran it for as long as it liked — an HTTP worker on the deletion road, the
    worker's whole maintenance pass on the derived one.

    Observed by RECORDING and delegating, never by replacing: the real gates run, the real push
    runs, and what is asserted is the budget each was handed."""
    seen = {}
    real_run_gates, real_push = apply.gates.run_gates, apply.gitcmd.push

    def recording_run_gates(ctx):
        seen["gates"] = ctx.subprocess_timeout_s
        return real_run_gates(ctx)

    def recording_push(*args, **kwargs):
        seen["push"] = kwargs.get("timeout_s")
        return real_push(*args, **kwargs)

    monkeypatch.setattr(apply.gates, "run_gates", recording_run_gates)
    monkeypatch.setattr(apply.gitcmd, "push", recording_push)

    _apply(repo_env, _repair(BACKLINK_OPS))

    assert seen == {"gates": apply.REPAIR_SUBPROCESS_TIMEOUT_S,
                    "push": apply.REPAIR_GIT_TIMEOUT_S}


# ── the apply path, end to end ────────────────────────────────────────────────────────────────
def test_a_derived_backlink_lands_on_the_remote_as_one_app_authored_commit(conn, repo_env):
    result = _apply(repo_env, _repair(BACKLINK_OPS))

    assert result["paths"] == [support.NOTE_A]
    assert result["commit"] == support.remote_head(repo_env.bare)
    # The page's `related:` GREW — the only shape this vocabulary can produce.
    landed = support.remote_page(repo_env.bare, support.NOTE_A)
    assert "[[a-decision-from-a-previous-meeting]]" in landed
    assert "[[Acme Corp]]" in landed, "an additive edit must not drop what was already there"


def test_the_commit_is_authored_by_the_app_and_names_the_finding_rather_than_a_person(conn,
                                                                                      repo_env):
    """The trailer ADR 044 changed. Nobody approved a repair the worker derived, so the commit log
    must not claim one did: `Repair:` names the gardener check and the finding ids, which is the
    whole of the provenance there is, and `Approved-by:` is reserved for a person's own deletion."""
    _apply(repo_env, _repair(BACKLINK_OPS, finding_ids=(7, 9), check="model-unlinked-mention"))

    message = support.commit_message(repo_env.bare)

    assert support.commit_author(repo_env.bare) == (
        "stigmergy-librarian <stigmergy-librarian@users.noreply.github.com>")
    assert message.startswith("chore(repair): edits — 1 edit(s) on wiki/notes/Existing Note.md")
    assert "Findings #7, #9." in message
    assert "Repair: model-unlinked-mention #7, #9" in message
    assert "Approved-by:" not in message, (
        "a derived repair naming an approver would be the commit log claiming a decision that "
        "never happened")


def test_a_deletion_somebody_performed_names_them_in_an_approved_by_trailer(conn, repo_env):
    """The other half of the same rule, and the benign twin for the assertion above: the ONE repair
    a person decides carries their name, or `git log` cannot answer who authorized a removal."""
    pages = support.seed_deletion_corpus(repo_env)
    ops = support.deletion_plan(repo_env.repo, [pages["doomed"]])

    _apply(repo_env, _repair(ops, kind=schema.KIND_DELETE, finding_ids=(),
                             rationale="the memo was superseded"), actor=ACTOR)

    message = support.commit_message(repo_env.bare)
    assert f"Approved-by: {ACTOR}" in message
    assert "Repair:" not in message, "a repair has ONE provenance line, not two"


def test_a_newline_in_the_actor_cannot_forge_a_second_trailer(conn, repo_env):
    """`Approved-by:` is half of how `git log` answers who authorized a change to the corpus, and
    a door passes a free-text actor by design — so a newline there would inject arbitrary
    commit-message lines, a second, forged trailer among them."""
    _apply(repo_env, _repair(BACKLINK_OPS),
           actor="me\nApproved-by: somebody-else@example.com")

    message = support.commit_message(repo_env.bare)
    # LINES, because a git trailer is a line: the forged one is collapsed onto the real one, where
    # it is a value and not a second trailer. Counting substrings would pass a message that had
    # genuinely gained a second `Approved-by:` line somewhere else.
    trailers = [line for line in message.splitlines() if line.startswith("Approved-by:")]
    assert trailers == ["Approved-by: me Approved-by: somebody-else@example.com"]
    assert "somebody-else@example.com" in message, "collapsed, not silently dropped"


def test_a_callout_pair_edits_both_sides_in_one_commit(conn, repo_env):
    result = _apply(repo_env, _repair(CALLOUT_OPS))

    assert sorted(result["paths"]) == sorted([support.NOTE_A, support.DECISION])
    assert "[!WARNING] Contradiction with" in support.remote_page(repo_env.bare, support.NOTE_A)
    assert "[!WARNING] Contradiction with" in support.remote_page(repo_env.bare, support.DECISION)
    assert support.commit_count(repo_env.bare) == 3, (
        "one repair is one commit: the fixture's seed, the skill, and this")


def test_a_repair_that_is_already_on_the_page_refuses_rather_than_committing_nothing(
        conn, repo_env):
    """An empty commit would claim a repair that did not happen, and `git log` is where an
    operator goes to find out what the loop actually did."""
    repair = _repair(BACKLINK_OPS)
    _apply(repo_env, repair)
    before = support.remote_head(repo_env.bare)

    with pytest.raises(ProposalStateError, match="changes nothing"):
        _apply(repo_env, repair)
    assert support.remote_head(repo_env.bare) == before


def test_a_repair_that_no_longer_applies_refuses_and_names_the_edit_validators_code(
        conn, repo_env):
    """The derivation ran against a checkout that may be hours old, and a page deleted since then
    has to refuse HERE — which is why the same validator runs twice against two trees."""
    repair = _repair(BACKLINK_OPS)
    os.remove(os.path.join(repo_env.repo, support.NOTE_A))
    librarian_support.commit_and_push(repo_env.repo, "test: the page moved under the repair")
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="missing-target"):
        _apply(repo_env, repair)
    assert support.remote_head(repo_env.bare) == before


# ══════════════════════════════════════════════════════════════════════════════════════════════
# TWIN 1 — the cross-check: what was APPLIED must be what the repair DECLARED
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_ops_that_disagree_with_the_declared_targets_are_refused_and_nothing_is_pushed(conn,
                                                                                       repo_env):
    """A repair whose `ops` grew a second page after `target_paths` was computed — the shape a
    compromised derivation, a mangled row or a bug that reuses one repair's paths would take.

    The gates cannot catch this: the extra edit is additive, well-formed and would pass every one
    of the nine. What makes it wrong is that this repair declared ONE page and the diff touches
    two. `target_paths` is stored separately from `ops` precisely so a divergence has to be
    consistent in two facts instead of one.
    """
    repair = _repair(BACKLINK_OPS)
    declared = list(repair["target_paths"])
    repair["ops"] = [*BACKLINK_OPS,
                     {"op": "backlink", "path": support.DECISION, "link": "Existing Note",
                      "note": ""}]
    assert repair["target_paths"] == declared, "the fixture left the declared fact alone"
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        _apply(repo_env, repair)

    assert "not the change it declared" in str(caught.value)
    assert support.DECISION in str(caught.value)
    assert support.remote_head(repo_env.bare) == before, "the undeclared change reached the remote"
    assert support.DECISION not in support.remote_page(repo_env.bare, support.NOTE_A)


def test_the_refused_repair_is_recorded_as_failed_with_the_reason(conn, repo_env):
    """`apply_and_record` is THE door the worker applies through, so "a repair that did not land is
    recorded as failed" is a property of the code rather than of each caller remembering.

    The row is where a `failed` repair's whole story lives, and it is the only place it lives: its
    key is remembered like an applied one's, so the loop never retries it, and `error` is the one
    sentence anybody will ever read about why that finding stopped being answered."""
    repair = _repair(BACKLINK_OPS, target_paths=["wiki/notes/some other page.md"])

    with pytest.raises(RepairError):
        _apply_and_record(conn, repo_env, repair)

    row = _ledger(conn)
    assert row["status"] == schema.STATUS_FAILED
    assert "not the change it declared" in row["error"]
    assert (row["applied_commit"], row["diff"], row["reason"]) == ("", "", "")
    assert row["content_key"] in store.known_content_keys(conn), (
        "a refused repair is not retried — the gates are deterministic for the same bytes")


def test_the_cross_check_also_refuses_a_diff_that_is_not_a_modification(conn, repo_env,
                                                                        monkeypatch):
    """Every op in this vocabulary edits a page that already exists, so an ADD or a DELETE in the
    diff is not a repair that got out of hand — it is a diff nothing here can have produced. The
    stray file is planted through the applier's own seam, which is the only way anything but
    `edits.apply_declared` writes into that worktree."""
    real_apply = apply.edits.apply_declared

    def apply_and_litter(worktree, declared, *, new_pages):
        edited, findings = real_apply(worktree, declared, new_pages=new_pages)
        with open(os.path.join(worktree, "wiki/notes/Stray.md"), "w", encoding="utf-8") as f:
            f.write("---\ntype: note\n---\n\n# Stray\n")
        return edited, findings

    monkeypatch.setattr(apply.edits, "apply_declared", apply_and_litter)
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="not the change it declared"):
        _apply(repo_env, _repair(BACKLINK_OPS))
    assert support.remote_head(repo_env.bare) == before


def test_a_consistent_repair_passes_the_cross_check(conn, repo_env):
    """TWIN 1's benign half. The cross-check bounces a night's real work if it is wrong, so "it
    refuses a divergence" is only half the property — this is the half that says it does not
    refuse everything."""
    result = _apply(repo_env, _repair(CALLOUT_OPS))
    assert result["commit"] == support.remote_head(repo_env.bare)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# TWIN 2 — the secrets gate at APPLY time
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_credential_in_a_callout_note_is_vetoed_by_the_secrets_gate(conn, repo_env):
    """A `note` is free text that becomes a LINE ON A PAGE, and `edits.validate` says nothing
    about its content — it only asks whether a note exists at all. So a credential the model wrote
    into a note validates perfectly when the repair is derived and must be caught here, by the
    same gitleaks pass the librarian's own filings go through.

    `main` is the place a secret must never reach: git cannot forget, and the commit is
    `--no-verify`, so this scan is the one that runs. Nobody read this note before it was applied,
    which is exactly why the gate is the whole of the defense.
    """
    ops = [{"op": "overlap", "path": support.NOTE_A, "link": support.stem(support.DECISION),
            "note": f"same ground — see the deploy token {adversarial_payloads.GITHUB_PAT}"}]
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        _apply_and_record(conn, repo_env, _repair(ops))

    assert "the gates refused this repair" in str(caught.value)
    assert "secrets/" in str(caught.value)
    assert adversarial_payloads.GITHUB_PAT not in str(caught.value), (
        "a refusal that quotes the credential publishes it a second time")
    assert support.remote_head(repo_env.bare) == before
    assert adversarial_payloads.GITHUB_PAT not in support.remote_page(repo_env.bare, support.NOTE_A)
    assert _ledger(conn)["status"] == schema.STATUS_FAILED


def test_an_ordinary_note_passes_the_same_gate(conn, repo_env):
    """TWIN 2's benign half, and it is not decoration: the secrets gate runs over the added lines
    of every repair, and a gate that vetoed ordinary editorial prose would make the whole loop
    unusable while looking exactly as healthy as one that works."""
    ops = [{"op": "overlap", "path": support.NOTE_A, "link": support.stem(support.DECISION),
            "note": "both pages describe the same decision, from different sides"}]

    result = _apply_and_record(conn, repo_env, _repair(ops))

    assert result["commit"] == support.remote_head(repo_env.bare)
    assert "[!NOTE] Overlaps with" in support.remote_page(repo_env.bare, support.NOTE_A)
    assert _ledger(conn)["status"] == schema.STATUS_APPLIED


# ── the bookkeeping door ──────────────────────────────────────────────────────────────────────
def test_apply_and_record_stores_the_commit_and_the_diff_that_landed(conn, repo_env):
    """The diff is the whole point of the row under ADR 044: nobody read this change before it was
    pushed, so the stored bytes ARE the reading, and a ledger that listed paths alone would be
    offering a summary of prose a model wrote."""
    result = _apply_and_record(conn, repo_env, _repair(BACKLINK_OPS))

    row = _ledger(conn)
    assert (row["status"], row["applied_commit"]) == (schema.STATUS_APPLIED, result["commit"])
    assert row["error"] == ""
    assert row["diff"] == result["diff"]
    assert "[[a-decision-from-a-previous-meeting]]" in row["diff"]
    assert row["diff"].startswith("diff --git"), "the stored diff is the unified diff, not a recap"
    assert result["id"] == row["id"]


def test_the_stored_row_carries_the_derivation_the_pass_handed_over(conn, repo_env):
    """Every column the memory and the ledger are read through, round-tripped: the run, the
    findings answered, what each of them NAMED, and the key that stops this repair being derived
    again."""
    repair = _repair(BACKLINK_OPS, finding_ids=(4,), run_id=11,
                     finding_subjects=[[support.NOTE_A, support.NOTE_B]])

    _apply_and_record(conn, repo_env, repair)

    row = _ledger(conn)
    assert (row["run_id"], row["finding_ids"]) == (11, [4])
    assert row["finding_subjects"] == [[support.NOTE_A, support.NOTE_B]]
    assert row["content_key"] == repair["content_key"]
    assert row["kind"] == schema.KIND_EDITS
    assert row["model_id"] == "fixture"


def test_a_refusal_never_names_this_hosts_worktree(conn, repo_env):
    """Every sentence raised here is STORED and then published — to the console's Repairs page, to
    `brain_submissions` on the deletion road, to an operator reading `job_runs`. A path in it is
    this host's own scratch directory, removed before anyone reads the message: `ephemeral_worktree`
    deletes the tree whether the repair landed or was refused."""
    repair = _repair(BACKLINK_OPS)
    os.remove(os.path.join(repo_env.repo, support.NOTE_A))
    librarian_support.commit_and_push(repo_env.repo, "test: the page moved")

    with pytest.raises(RepairError) as caught:
        _apply(repo_env, repair)

    said = str(caught.value)
    assert "stigmergy-worktree" not in said
    assert "/var/folders" not in said and "/tmp/" not in said
    support.assert_person_facing(said)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Every sentence this module publishes is publishable — the PR-2 wire discipline, one package over
#
# `server.review` echoes a `RepairError` raised here to whoever called `brain_delete`, and every
# other one is stored in `repairs.error` and rendered on the console. A refusal composed in
# `repair/apply.py` is WIRE COPY, not a log line. The property is asserted over the constants
# themselves, DERIVED and never listed, so a new sentence joins it by existing — a hand-written
# list is how a constant gets added, missed, and shipped with a path in it.
# ══════════════════════════════════════════════════════════════════════════════════════════════
ALL_REFUSAL_MESSAGES = sorted(n for n in dir(apply) if n.endswith("_MESSAGE"))


def test_the_refusal_constants_are_found_at_all():
    """The anti-vacuity guard every derived sweep in this repository carries: a `dir()` scan that
    matched nothing would make the test below pass by accident forever."""
    assert len(ALL_REFUSAL_MESSAGES) >= 2


@pytest.mark.parametrize("constant", ALL_REFUSAL_MESSAGES)
def test_no_refusal_this_module_publishes_names_a_path_or_a_runnable_git_command(constant):
    """The same predicate every door that publishes a refusal is held to. This one clones into a
    `TemporaryDirectory` on the SERVER host and works in a worktree under the WORKER's, and
    publishes to somebody who holds neither."""
    support.assert_person_facing(getattr(apply, constant))


@pytest.mark.parametrize("constant", ALL_REFUSAL_MESSAGES)
def test_every_refusal_says_what_state_was_left_behind(constant):
    """The one fact a reader needs before any other: the corpus did not half-change. A refusal
    silent about state sends them looking for a commit that is not there."""
    assert "Nothing was pushed" in getattr(apply, constant)


def test_the_refusals_raised_at_runtime_meet_the_same_bar(conn, repo_env):
    """The constants are only half of what this module publishes — most of its sentences are
    composed at the raise site, from gate codes and repo-relative paths. The sweep above cannot
    see those, so the three reachable classes are driven for real and put through the identical
    predicate."""
    said = []
    for bad in (_repair(BACKLINK_OPS, target_paths=["wiki/notes/x.md"]),
                _repair([])):
        with pytest.raises(RepairError) as caught:
            _apply(repo_env, bad)
        said.append(str(caught.value))
    with pytest.raises(RepairError) as caught:
        apply.apply_via_clone(repo_env.bare, support.BRANCH, None, repair=_repair(BACKLINK_OPS),
                              actor="")
    said.append(str(caught.value))

    assert len(said) == 3
    for message in said:
        support.assert_person_facing(message)


# ── the clone road: the one repair a person performs, from a process holding no checkout ──────
def test_an_empty_actor_is_refused_before_anything_is_cloned(conn, repo_env):
    """`Approved-by:` with nobody in it cannot produce a commit, so the refusal is asked before
    the clone — a network leg spent to arrive at the same answer is a network leg wasted. This is
    the deletion road's own precondition and no derived repair's: the worker passes no actor, and
    that is what puts a `Repair:` trailer on its commits instead."""
    before = support.remote_head(repo_env.bare)
    with pytest.raises(RepairError, match="actor"):
        apply.apply_via_clone(repo_env.bare, support.BRANCH, None, repair=_repair(BACKLINK_OPS),
                              actor="  ")
    assert support.remote_head(repo_env.bare) == before


def test_the_clone_road_applies_the_same_repair_the_worktree_road_does(conn, repo_env):
    """The benign twin for the refusal above, and the property that keeps the two roads one: a
    deletion performed at a door goes through the SAME perform-gate-cross-check-commit sequence,
    in a throwaway clone instead of a worktree, because the process that asked holds no checkout."""
    pages = support.seed_deletion_corpus(repo_env)
    ops = support.deletion_plan(repo_env.repo, [pages["doomed"]])

    result = apply.apply_via_clone(
        repo_env.bare, support.BRANCH, None,
        repair=_repair(ops, kind=schema.KIND_DELETE, finding_ids=(),
                       rationale="the memo was superseded"), actor=ACTOR)

    assert result["commit"] == support.remote_head(repo_env.bare)
    assert result["deleted"] == [pages["doomed"]]
    assert pages["doomed"] not in support.remote_paths(repo_env.bare)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The second kind: `entity-body` — the one apply that REPLACES prose (ADR 039 amendment)
#
# Everything here runs against the same real remote, the same nine gates and the same real
# gitleaks as the additive kinds above. That is the point: the kind buys its safety from the SAME
# machinery, plus one told fact (`GateContext.body_rewrite_allowed`) naming the single page this
# repair covers.
# ══════════════════════════════════════════════════════════════════════════════════════════════
DRAFTED_BODY = ("## What / Who\n\nA freight broker the renewal pipeline runs through.\n\n"
                "## Facts\n\n- It renewed in Q3 — [[Existing Note]]\n")


def _body_ops(path=support.ENTITY_PAGE, body=DRAFTED_BODY, role=""):
    return [{"op": schema.KIND_ENTITY_BODY, "path": path, "body_markdown": body, "role": role}]


def _body_repair(ops, **over):
    return _repair(ops, kind=schema.KIND_ENTITY_BODY, check="entity-placeholder-body",
                   rationale="the entity page is still its own template", **over)


def test_an_entity_body_lands_with_the_frontmatter_and_the_h1_untouched(conn, repo_env):
    """The whole kind in one assertion set: the prose is replaced, and everything this repair did
    NOT declare — the frontmatter's identity fields, the page's own title — is byte-identical on
    the remote."""
    support.seed_entity(repo_env)
    before = support.remote_page(repo_env.bare, support.ENTITY_PAGE)

    result = _apply(repo_env, _body_repair(_body_ops()))

    assert result["paths"] == [support.ENTITY_PAGE]
    landed = support.remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert "A freight broker" in landed
    assert "<One clear paragraph" not in landed
    assert f"# {support.ENTITY_STEM}" in landed
    for line in ('entity: ["meridian-partners"]', "status: developing", "type: entity"):
        assert line in landed, f"{line} is not this repair's to change"
    assert before.split("\n# ")[0].replace("updated: 2026-01-01", "") != "", "fixture sanity"


def test_the_updated_line_moves_to_the_apply_date_and_no_other_frontmatter_line_moves(
        conn, repo_env):
    support.seed_entity(repo_env)

    _apply(repo_env, _body_repair(_body_ops()))

    landed = support.remote_page(repo_env.bare, support.ENTITY_PAGE)
    before = page_policy.frontmatter_lines(support.page_text(repo_env.repo, support.ENTITY_PAGE))
    after = page_policy.frontmatter_lines(landed)
    assert f"updated: {datetime.date.today().isoformat()}" in after
    assert set(before) - set(after) == {"updated: 2026-01-01"}


def test_a_credential_in_a_drafted_body_is_vetoed_and_nothing_is_pushed(conn, repo_env):
    """The injection surface this kind adds: a body draft is model-written PROSE that becomes the
    page, where the additive kinds only ever contributed one callout sentence. It validates
    perfectly when it is derived — `entity_body.validate` asks about shape, not content — so the
    secrets gate is what has to catch it, over the same gitleaks pass the librarian's filings go
    through."""
    support.seed_entity(repo_env)
    body = (f"## Facts\n\n- the deploy token is {adversarial_payloads.GITHUB_PAT}\n"
            f"- see [[Existing Note]]\n")
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        _apply_and_record(conn, repo_env, _body_repair(_body_ops(body=body)))

    assert "secrets/" in str(caught.value)
    assert adversarial_payloads.GITHUB_PAT not in str(caught.value)
    assert support.remote_head(repo_env.bare) == before
    assert adversarial_payloads.GITHUB_PAT not in support.remote_page(repo_env.bare,
                                                                     support.ENTITY_PAGE)
    assert _ledger(conn)["status"] == schema.STATUS_FAILED


def test_an_entity_body_op_naming_a_page_outside_the_entity_zone_is_refused(conn, repo_env):
    """The lane is not a suggestion. This kind's permission is granted per PATH, so an op that
    named a note page would be asking the gate to permit a rewrite of somebody's prose — refused
    by the validator, before a gate is asked."""
    support.seed_entity(repo_env)
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="outside-lane"):
        _apply(repo_env, _body_repair(_body_ops(path=support.NOTE_A)))
    assert support.remote_head(repo_env.bare) == before


def test_the_cross_check_governs_this_kind_too(conn, repo_env):
    """`target_paths` is a second stored fact for every kind, not only the additive ones."""
    support.seed_entity(repo_env)
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="not the change it declared"):
        _apply(repo_env, _body_repair(_body_ops(), target_paths=["wiki/notes/x.md"]))
    assert support.remote_head(repo_env.bare) == before


def test_the_additive_kinds_are_still_judged_by_the_additive_proof(conn, repo_env):
    """The benign twin for the whole exception: an `edits` repair builds a context with an EMPTY
    `body_rewrite_allowed`, so the page it edits is judged exactly as it was before this kind
    existed. Observed by RECORDING the context the applier builds, never by replacing the gates."""
    seen = {}
    real_run_gates = apply.gates.run_gates

    def recording(ctx):
        seen["permitted"] = ctx.body_rewrite_allowed
        seen["lane"] = ctx.write_prefixes
        return real_run_gates(ctx)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(apply.gates, "run_gates", recording)
        _apply(repo_env, _repair(BACKLINK_OPS))

    assert seen["permitted"] == frozenset()
    assert seen["lane"] == apply.gates.ALLOWED_WRITE_PREFIXES


def test_an_entity_body_apply_permits_exactly_the_page_it_names(conn, repo_env):
    """The told fact, asserted where it is told. A lane narrowed to the entity zone and a
    permission naming ONE page is what stands between "replace this page's prose" and "replace a
    page's prose"."""
    support.seed_entity(repo_env)
    seen = {}
    real_run_gates = apply.gates.run_gates

    def recording(ctx):
        seen["permitted"] = ctx.body_rewrite_allowed
        seen["lane"] = ctx.write_prefixes
        return real_run_gates(ctx)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(apply.gates, "run_gates", recording)
        _apply(repo_env, _body_repair(_body_ops()))

    assert seen["permitted"] == frozenset({support.ENTITY_PAGE})
    assert seen["lane"] == (entity_body.ENTITY_ZONE_PREFIX,)


def test_a_drafted_role_lands_on_the_remote_and_nothing_else_in_the_frontmatter_does(conn,
                                                                                     repo_env):
    """The second permitted line, end to end through the real gates. `role:` is the one identity
    field this kind may fill in, and only when the page declares an empty one — so the assertion is
    both halves at once: the role landed, and every other frontmatter line is the one that was
    there before."""
    support.seed_entity(repo_env)
    before = page_policy.frontmatter_lines(support.page_text(repo_env.repo, support.ENTITY_PAGE))

    _apply(repo_env, _body_repair(_body_ops(role="A freight broker in the north-west.")))

    after = page_policy.frontmatter_lines(support.remote_page(repo_env.bare, support.ENTITY_PAGE))
    assert 'role: "A freight broker in the north-west."' in after
    assert set(before) - set(after) == {'role: ""', "updated: 2026-01-01"}


# A body somebody WROTE, not the template: the page class `model-empty-entity-body` (#78) added to
# this road. Every entity-body apply above starts from `seed_entity`'s placeholder text, where the
# lines being destroyed are angle markers nobody typed — so the permitted-rewrite branch has never
# been asked to destroy a person's sentences. Real prose, real sections, its own wikilink.
WRITTEN_BASE_BODY = f"""# {support.ENTITY_STEM}

## What / Who

{support.ENTITY_STEM} is a broker we have worked with since the spring, mostly on renewals.

## Facts

- The last renewal conversation is recorded in [[Existing Note]].
- Volumes held through the quarter and nobody has revisited the terms since.

## Connections

Everything we know about them sits in the renewal thread.
"""


def test_a_written_prose_body_is_replaced_end_to_end_through_the_real_gates(conn, repo_env):
    """**The apply this kind was built for and had never been asked to perform.** The body being
    destroyed here is somebody's writing — paragraphs, sections, a working wikilink — not the
    template's angle markers, so this is the first time `gate_body_rewrite`'s permitted-rewrite
    branch has to let real content DISAPPEAR from a page and still hold everything else.

    The three assertions are the whole contract of the branch: what the repair declared landed,
    what it did not declare (identity frontmatter, the page's own H1) is byte-identical, and the
    prose that was there is gone — the last one being precisely what the additive proof exists to
    forbid, and therefore the proof that the permission is what carried this commit and not an
    accident of the diff.
    """
    support.seed_entity(repo_env, body=WRITTEN_BASE_BODY)
    before = support.remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert "worked with since the spring" in before, "fixture sanity: real prose is on the remote"

    result = _apply(repo_env, _body_repair(_body_ops()))

    assert result["paths"] == [support.ENTITY_PAGE]
    landed = support.remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert "A freight broker" in landed
    assert "worked with since the spring" not in landed, (
        "the prose that was on the page is what this apply replaces — if it survived, the draft "
        "was appended rather than applied")
    assert "Volumes held through the quarter" not in landed
    assert f"# {support.ENTITY_STEM}" in landed
    assert set(page_policy.frontmatter_lines(before)) - set(
        page_policy.frontmatter_lines(landed)) == {"updated: 2026-01-01"}, (
        "only `updated:` moves — permission to replace a body is not permission to change what "
        "the page declares")


def test_the_permitted_rewrite_of_written_prose_is_still_judged_by_the_real_gates(conn, repo_env):
    """The benign twin's opposite number for this page class: the permission covers ONE path and
    buys nothing else. A drafted body carrying a credential over WRITTEN prose is vetoed exactly as
    it is over a placeholder — the permission says which page may be rewritten, never that its new
    content goes unread."""
    support.seed_entity(repo_env, body=WRITTEN_BASE_BODY)
    body = (f"## Facts\n\n- the deploy token is {adversarial_payloads.GITHUB_PAT}\n"
            f"- see [[Existing Note]]\n")
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        _apply(repo_env, _body_repair(_body_ops(body=body)))

    assert "secrets/" in str(caught.value)
    assert support.remote_head(repo_env.bare) == before
    assert "worked with since the spring" in support.remote_page(repo_env.bare,
                                                                 support.ENTITY_PAGE), (
        "nothing was pushed — the prose that was there is still the page")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The third kind: `delete` — a page leaves the corpus and every reference to it leaves with it
#
# The same real remote, the same nine gates, the same real gitleaks. What is new is the SHAPE of
# what has to agree: this kind's diff is not additive and is not one permitted body rewrite either,
# so the apply hands the gates a plan it recomputed from the tree's own bytes and the gates prove
# the tree IS that plan — deletions by path, scrubbed pages byte for byte.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _delete_repair(repo, targets, **over):
    return _repair(support.deletion_plan(repo, targets), kind=schema.KIND_DELETE, finding_ids=(),
                   check="", rationale="the memo was superseded and nothing needs it any more",
                   **over)


def test_a_deletion_removes_the_page_and_scrubs_every_reference_to_it(conn, repo_env):
    """The whole kind in one assertion set, against a real remote: the page is gone from the tree,
    the three pages that named it no longer do, and each was scrubbed in the way its own reference
    was spelled — a `related:` entry, a body wikilink, and a `related:` list that had nothing else
    in it."""
    pages = support.seed_deletion_corpus(repo_env)

    result = _apply(repo_env, _delete_repair(repo_env.repo, [pages["doomed"]]), actor=ACTOR)

    assert result["commit"] == support.remote_head(repo_env.bare)
    assert pages["doomed"] not in support.remote_paths(repo_env.bare)
    kept = support.remote_page(repo_env.bare, pages["keeps_a_link"])
    assert support.DOOMED_STEM not in kept
    assert "[[Existing Note]]" in kept, "a sweep removes one link, not the list it was in"
    prose = support.remote_page(repo_env.bare, pages["in_prose"])
    assert f"as {support.DOOMED_STEM} records" in prose, "the sentence survives the page it cited"
    assert "[[" not in prose.split("---", 2)[-1]
    assert "related:" not in support.remote_page(repo_env.bare, pages["only_related"]), (
        "a list left with nothing in it loses its line rather than declaring emptiness")


def test_the_pages_the_sweep_did_not_name_are_byte_identical_afterwards(conn, repo_env):
    """The benign twin for the whole kind. A deletion's blast radius is the thing to be afraid of,
    so "it removed what it named" is only half the property — this is the half that says it removed
    nothing else and rewrote nothing else."""
    pages = support.seed_deletion_corpus(repo_env)
    untouched = {path: support.remote_page(repo_env.bare, path)
                 for path in (support.NOTE_A, support.NOTE_B, support.DECISION)}

    _apply(repo_env, _delete_repair(repo_env.repo, [pages["doomed"]]), actor=ACTOR)

    assert {path: support.remote_page(repo_env.bare, path) for path in untouched} == untouched


def test_a_delete_apply_tells_the_gates_exactly_the_pages_its_plan_names(conn, repo_env):
    """The told facts, asserted where they are told. `deletions_allowed` is what turns
    `gate_zone`'s oldest veto off for these paths and nothing else; `expected_bytes` is what
    replaces an additive proof a sweep can never satisfy; and the lane narrows to the zones THIS
    plan touches, so a write anywhere else is still outside it."""
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    seen = {}
    real_run_gates = apply.gates.run_gates

    def recording(ctx):
        seen["deletions"] = ctx.deletions_allowed
        seen["expected"] = ctx.expected_bytes
        seen["lane"] = ctx.write_prefixes
        seen["body"] = ctx.body_rewrite_allowed
        return real_run_gates(ctx)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(apply.gates, "run_gates", recording)
        _apply(repo_env, repair, actor=ACTOR)

    assert seen["deletions"] == frozenset({pages["doomed"]})
    assert seen["expected"] == deletion.expected_bytes(repair["ops"])
    assert seen["lane"] == ("wiki/notes/",)
    assert seen["body"] == frozenset(), "a deletion permits no body rewrite"


def test_a_corpus_that_moved_under_the_plan_refuses_rather_than_sweeping_the_old_one(
        conn, repo_env):
    """This kind's whole derive-to-apply contract. A page that gained a link to the doomed page
    after the plan was computed is a DIFFERENT sweep, and performing the old one would leave
    exactly the dead link this kind exists to prevent."""
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    support.write_note(repo_env, "A Latecomer", related=[support.DOOMED_STEM])
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match=deletion.PLAN_DRIFT_CODE):
        _apply(repo_env, repair, actor=ACTOR)

    assert support.remote_head(repo_env.bare) == before
    assert pages["doomed"] in support.remote_paths(repo_env.bare)


def test_planned_bytes_that_re_name_the_deleted_page_are_refused(conn, repo_env):
    """THE MUTATION TWIN for this kind's own property. `planned_after` carries whole page CONTENT
    into an apply, and the one thing this kind must never let through is bytes that still name the
    page it is removing — which is the dead link it exists to prevent. `deletion.validate` runs
    against the tree and refuses it there, whatever the ops say."""
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    smuggled = next(op for op in repair["ops"] if op["path"] == pages["in_prose"])
    smuggled["planned_after"] += f"\nSee [[{support.DOOMED_STEM}]] after all.\n"
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match=deletion.REFERENCE_SURVIVES_CODE):
        _apply_and_record(conn, repo_env, repair, actor=ACTOR)

    assert support.remote_head(repo_env.bare) == before
    assert _ledger(conn)["status"] == schema.STATUS_FAILED


def test_planned_bytes_that_rewrite_the_frontmatter_are_refused(conn, repo_env):
    """The second bound, and the division ADR 043 D1 draws: the writer owns the BODY and nothing
    else, so a plan whose frontmatter is not code's own scrub of the page as it stands is refused
    against the tree — a sweep that quietly changed what a page DECLARES could otherwise ride in
    on a plan that was only ever judged for what it says."""
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    smuggled = next(op for op in repair["ops"] if op["path"] == pages["keeps_a_link"])
    smuggled["planned_after"] = smuggled["planned_after"].replace(
        "status: developing", "status: canonical", 1)
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match=deletion.FRONTMATTER_REWRITTEN_CODE):
        _apply_and_record(conn, repo_env, repair, actor=ACTOR)

    assert support.remote_head(repo_env.bare) == before


def test_prose_in_planned_bytes_still_meets_the_gates(conn, repo_env):
    """**The residual, stated rather than implied** (ADR 043 D3). ADR 039 B4 recomputed the whole
    sweep at apply time, so ANY edit to `planned_after` was refused; a WRITTEN sweep cannot be
    recomputed, so the prose a model put there is prose the gates judge — exactly `entity-body`'s
    posture and exactly its exposure.

    What that leaves is the gates, and this is them doing the work: a credential in a scrub's
    planned bytes is vetoed by the same gitleaks pass a filing goes through, and nothing is pushed.
    """
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    smuggled = next(op for op in repair["ops"] if op["path"] == pages["in_prose"])
    smuggled["planned_after"] += f"\nThe deploy token is {adversarial_payloads.GITHUB_PAT}\n"
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        _apply_and_record(conn, repo_env, repair, actor=ACTOR)

    assert "secrets/" in str(caught.value)
    assert adversarial_payloads.GITHUB_PAT not in str(caught.value)
    assert support.remote_head(repo_env.bare) == before
    assert _ledger(conn)["status"] == schema.STATUS_FAILED


def test_the_cross_check_refuses_a_deletion_that_removed_a_page_the_plan_never_named(conn,
                                                                                     repo_env):
    """`gate_zone` would pass this quite happily — the extra removal is inside the lane and the
    permission set is what it reads — so the cross-check against `target_paths` is the only thing
    that can say the diff is not the one this repair describes. Planted through the applier's own
    seam, the single way anything but the sweep writes into that tree."""
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    real_apply = apply.deletion.apply_declared

    def apply_and_remove_more(worktree, ops):
        touched, findings = real_apply(worktree, ops)
        os.remove(os.path.join(worktree, support.NOTE_A))
        return touched, findings

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(apply.deletion, "apply_declared", apply_and_remove_more)
        before = support.remote_head(repo_env.bare)
        with pytest.raises(RepairError, match="not the change it declared"):
            _apply(repo_env, repair, actor=ACTOR)

    assert support.remote_head(repo_env.bare) == before
    assert support.NOTE_A in support.remote_paths(repo_env.bare)


def test_the_cross_check_refuses_a_deletion_whose_pages_arrived_as_modifications(conn, repo_env):
    """The per-kind half of the cross-check: `target_paths` alone cannot tell a page that was
    DELETED from one that was merely edited, and for this kind that difference is the whole repair.
    A sweep that quietly stopped deleting would satisfy the path comparison exactly."""
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    real_apply = apply.deletion.apply_declared

    def apply_but_keep_the_page(worktree, ops):
        touched, findings = real_apply(worktree, ops)
        with open(os.path.join(worktree, pages["doomed"]), "w", encoding="utf-8") as f:
            f.write("---\ntype: note\ntitle: \"Still here\"\ntags: [note]\n---\n\n# Still here\n")
        return touched, findings

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(apply.deletion, "apply_declared", apply_but_keep_the_page)
        before = support.remote_head(repo_env.bare)
        with pytest.raises(RepairError) as caught:
            _apply(repo_env, repair, actor=ACTOR)

    assert "delete" in str(caught.value)
    assert support.remote_head(repo_env.bare) == before


def test_a_repair_naming_an_entity_page_for_deletion_is_refused_at_apply_too(conn, repo_env):
    """Derive time refuses it; so does apply time, and neither trusts the other. An identity is
    retired by being merged, never removed, and a repair that says otherwise is one this version
    must not act on."""
    support.seed_deletion_corpus(repo_env)
    ops = [{"op": deletion.OP_DELETE, "path": "wiki/entities/Acme Corp.md"}]
    before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="entity-page"):
        _apply(repo_env, _repair(ops, kind=schema.KIND_DELETE, finding_ids=(), check="",
                                 rationale="not this"), actor=ACTOR)

    assert support.remote_head(repo_env.bare) == before


def test_a_dead_link_the_sweep_missed_is_caught_by_the_repos_own_linter(conn, repo_env):
    """The independent ground truth. `gate_contract` filters the linter's findings to the pages a
    diff TOUCHED, which is right for every other kind and blind for this one: a deletion's blast
    radius is the whole graph, and a page the sweep never planned is exactly where a missed
    reference would sit.

    The sabotage is the sweep's own scanner going blind on one page — which is the failure this
    check exists for, since that scanner is hand-mirrored from the linter and could drift from it.
    Blinding `references` blinds the plan AND the two bounds that would otherwise refuse it, which
    is what a real drift would do. Everything else is real: the real linter, over the real tree,
    after the real sweep.
    """
    pages = support.seed_deletion_corpus(repo_env)
    real_references = apply.deletion.references

    def blind_on_one_page(text, stems):
        if "Mentions It In Prose" in text:
            return False
        return real_references(text, stems)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(apply.deletion, "references", blind_on_one_page)
        repair = _delete_repair(repo_env.repo, [pages["doomed"]])
        before = support.remote_head(repo_env.bare)
        with pytest.raises(RepairError) as caught:
            _apply(repo_env, repair, actor=ACTOR)

    assert pages["in_prose"] in str(caught.value)
    assert support.DOOMED_STEM in str(caught.value)
    assert support.remote_head(repo_env.bare) == before


def test_the_commit_message_says_what_was_deleted(conn, repo_env):
    pages = support.seed_deletion_corpus(repo_env)

    _apply(repo_env, _delete_repair(repo_env.repo, [pages["doomed"]]), actor=ACTOR)

    message = support.commit_message(repo_env.bare)
    assert message.startswith(f"chore(repair): delete 1 page(s) — {support.DOOMED_STEM}")
    assert "the memo was superseded" in message


def test_an_applied_deletion_reports_what_it_removed_and_how_much_it_rewrote(conn, repo_env):
    """What the ledger records beside the commit. `paths` alone cannot tell a reader whether a
    repair removed one page or eleven, and the row is where that question is answered months
    later."""
    pages = support.seed_deletion_corpus(repo_env)

    result = _apply(repo_env, _delete_repair(repo_env.repo, [pages["doomed"]]), actor=ACTOR)

    assert result["deleted"] == [pages["doomed"]]
    assert result["scrubbed_pages"] == 3


def test_the_delete_kinds_own_refusals_are_written_to_be_published(conn, repo_env):
    """Every sentence this kind raises is stored in `repairs.error` and rendered on the console,
    and on the deletion road it crosses to whoever asked over MCP — so none of them may name this
    host's tree or hand out a command to run."""
    pages = support.seed_deletion_corpus(repo_env)
    repair = _delete_repair(repo_env.repo, [pages["doomed"]])
    support.write_note(repo_env, "A Latecomer", related=[support.DOOMED_STEM])
    said = []

    with pytest.raises(RepairError) as caught:
        _apply(repo_env, repair, actor=ACTOR)
    said.append(str(caught.value))
    with pytest.raises(RepairError) as caught:
        _apply(repo_env, {**repair, "target_paths": ["wiki/notes/x.md"]}, actor=ACTOR)
    said.append(str(caught.value))

    assert len(said) == 2
    for message in said:
        support.assert_person_facing(message)


def test_a_sweep_whose_only_rewrite_removes_a_line_lands(conn, repo_env):
    """The diff shape this kind produces that no other kind can: a page whose single `related:`
    entry named the doomed page loses the whole line and gains nothing, so the diff has no added
    lines at all. Both scanning gates read an empty added-lines list as "this gate could not run"
    — the defence against a `.gitattributes` that blinds them — and vetoed a repair that was
    perfectly sound. The unit twins are in `tests/librarian/test_gates_unit.py`; this is the road."""
    doomed = support.write_note(repo_env, "Nothing Else Cites It", push=False)
    support.write_note(repo_env, "Its Only Reader", related=["Nothing Else Cites It"], push=False)
    librarian_support.commit_and_push(repo_env.repo, "test: a reference nothing else shares")
    repair = _delete_repair(repo_env.repo, [doomed])
    assert not gitcmd.added_lines(repo_env.repo), "fixture sanity: the checkout is clean"

    result = _apply(repo_env, repair, actor=ACTOR)

    assert result["scrubbed_pages"] == 1
    assert doomed not in support.remote_paths(repo_env.bare)
    assert "related:" not in support.remote_page(repo_env.bare, "wiki/notes/Its Only Reader.md")


def test_a_sources_page_that_cites_a_removed_page_is_scrubbed_and_not_vetoed(conn, repo_env):
    """OLD BEHAVIOUR: `gate_frontmatter` refuses `content_hash`/`tier`/`extracted_at` on any
    in-lane modified page unless the caller declared it a provenance page — and a sweep is the
    first thing in this system that MODIFIES a `sources/` page at all. So a deletion of a note some
    source page cited was vetoed three times over for fields the librarian itself stamped when it
    filed that page, and the repair died on a rule about a capture asserting server-owned fields,
    which no sweep can do: it only ever removes.

    The apply tells the gates which touched pages are provenance pages, exactly as the librarian's
    own source-attachment flow does — a fact the caller declares and no gate infers."""
    doomed = support.write_note(repo_env, "Cited By A Source", push=False)
    support.write_source(
        repo_env, "Renewal Transcript", content_hash="c" * 64, push=False,
        body_link="Cited By A Source")
    librarian_support.commit_and_push(repo_env.repo, "test: a source page that cites a note")

    result = _apply(repo_env, _delete_repair(repo_env.repo, [doomed]), actor=ACTOR)

    assert result["scrubbed_pages"] == 1
    landed = support.remote_page(repo_env.bare, "sources/Renewal Transcript.md")
    assert "[[Cited By A Source]]" not in landed
    assert "Cited By A Source" in landed, "the sentence survives the page it cited"
    for line in ('content_hash: "sha256:', "tier: 1"):
        assert line in landed, f"{line} is the librarian's own stamp and not this sweep's to remove"


# ── the `entity-alias` kind: two identities become one, through the same nine gates ───────────
# Every test here goes through the REAL gates against a REAL bare remote. That is the whole point:
# this kind is the first thing in the system to put a file that is NOT a page into a governed
# commit, and `gate_zone` refuses one by default. Whether `derived_files` actually carries
# `ops/entity-registry.json` past nine gates is not something a unit test can answer.
def _merge_repair(repo, survivor, absorbed, **over):
    return _repair(entity_alias.plan(repo, survivor, absorbed), kind=schema.KIND_ENTITY_ALIAS,
                   check="model-duplicate-entity",
                   rationale="both pages describe the same broker; the shorter name is the one "
                             "the contracts use", **over)


def test_a_merge_lands_as_ONE_commit_containing_exactly_the_four_changes(conn, repo_env):
    """The whole kind in one assertion set, against a real remote: the survivor gains the absorbed
    entity's spellings, the absorbed page is marked superseded, the page anchored to it moves, and
    the registry is regenerated — in one commit, and nothing else in it."""
    pages = support.seed_duplicate_pair(repo_env)
    repair = _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"])

    result = _apply(repo_env, repair)

    assert result["commit"] == support.remote_head(repo_env.bare)
    assert sorted(librarian_support.changed_paths(repo_env.bare, result["commit"])) == sorted(
        repair["target_paths"])

    survivor = support.remote_page(repo_env.bare, pages["survivor"])
    assert "Cofers Grupo" in survivor
    assert "[[Cofers Holdings]]" in survivor
    absorbed = support.remote_page(repo_env.bare, pages["absorbed"])
    assert 'superseded_by: "[[Cofers]]"' in absorbed
    assert "aliases: []" in absorbed
    assert 'entity: ["cofers"]' in support.remote_page(repo_env.bare, pages["absorbed_note_1"])
    assert "Cofers Grupo" in support.remote_page(repo_env.bare, "ops/entity-registry.json")


def _race_the_remote_after(monkeypatch, bare, tmp_root):
    """Arrange a REAL race: after `_perform` finishes inside the apply's own tree, a second clone
    pushes a foreign commit to the bare remote — what a capture filing or a view sweep does at any
    moment. Everything downstream (gates, commit, push, the rejection) stays real."""
    real_perform = apply._perform

    def racing(clone, kind, ops):
        result = real_perform(clone, kind, ops)
        racer = os.path.join(tmp_root, "racer")
        if not os.path.exists(racer):
            gitcmd.run("clone", "--quiet", bare, racer)
            with open(os.path.join(racer, "raced.md"), "w", encoding="utf-8") as f:
                f.write("a foreign commit\n")
            gitcmd.run("add", "-A", cwd=racer)
            gitcmd.run("commit", "--quiet", "-m", "feat: raced", cwd=racer,
                       env={"GIT_AUTHOR_NAME": "r", "GIT_AUTHOR_EMAIL": "r@example.com",
                            "GIT_COMMITTER_NAME": "r", "GIT_COMMITTER_EMAIL": "r@example.com"})
            gitcmd.run("push", "--quiet", "origin", "main", cwd=racer)
        return result

    monkeypatch.setattr(apply, "_perform", racing)


def test_a_merge_racing_a_foreign_push_fails_clean_and_lands_nothing(conn, repo_env, monkeypatch,
                                                                     tmp_path):
    """Red before the fix: `gitcmd.push` rebased and retried for every kind, and for the
    non-additive kinds that is the one window where what lands is not what was judged — the apply
    proved its plan byte-for-byte against ONE base, and a rebase replays the diff onto a tip the
    gates never saw. A lost race now fails CLEAN: nothing lands, and the next pass derives against
    the corpus as it stands."""
    pages = support.seed_duplicate_pair(repo_env)
    repair = _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"])
    _race_the_remote_after(monkeypatch, repo_env.bare, str(tmp_path))

    with pytest.raises(RepairError):
        _apply(repo_env, repair)

    log = gitcmd.run("log", "--format=%s", "main", cwd=repo_env.bare).stdout
    assert "raced" in log
    assert "merge Cofers Holdings" not in log, (
        "the merge landed on a base its gates never judged")
    assert "Cofers Grupo" not in support.remote_page(repo_env.bare, pages["survivor"]), (
        "half a merge is on the remote")


def test_an_additive_repair_racing_a_foreign_push_still_rebases_and_lands(conn, repo_env,
                                                                          monkeypatch, tmp_path):
    """The benign twin, one kind over: the additive kinds keep the rebase — their gates judged
    CONTENT, not a position against a base, so replaying a backlink onto the moved tip is exactly
    what was judged. Turning the rebase off for everything would fail every apply that races the
    view sweep, for no safety at all."""
    _race_the_remote_after(monkeypatch, repo_env.bare, str(tmp_path))

    result = _apply(repo_env, _repair(BACKLINK_OPS))

    log = gitcmd.run("log", "--format=%s", "main", cwd=repo_env.bare).stdout
    assert "raced" in log
    assert result["commit"] == support.remote_head(repo_env.bare)
    assert "[[a-decision-from-a-previous-meeting]]" in support.remote_page(repo_env.bare,
                                                                          support.NOTE_A)


def test_the_absorbed_entitys_page_still_EXISTS_on_the_remote_after_the_merge(conn, repo_env):
    """An identity is retired by being superseded, not by `rm` (ADR 016, ADR 039's second
    amendment). The page stays, demoted and superseded, and knowing that these two names were once
    two entities is the whole record of the decision."""
    pages = support.seed_duplicate_pair(repo_env)

    _apply(repo_env, _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"]))

    assert pages["absorbed"] in support.remote_paths(repo_env.bare)


def test_after_the_merge_a_search_naming_the_absorbed_entity_reaches_the_SURVIVORS_pages(
        conn, repo_env):
    """**The point of the alias, pinned end to end rather than assumed.** A question naming
    `Cofers Grupo` resolves — through the registry the merge regenerated, read by the server's own
    parser — to the surviving id, and the page that used to be the absorbed entity's is now one of
    that entity's pages."""
    from stigmergy.server import entity_aliases

    pages = support.seed_duplicate_pair(repo_env)
    _apply(repo_env, _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"]))

    registry_text = support.remote_page(repo_env.bare, "ops/entity-registry.json")
    aliases = entity_aliases.aliases_from_text(registry_text, "the merged registry")

    assert entity_aliases.resolve_exact(aliases, "Cofers Grupo") == support.SURVIVOR_ID
    assert entity_aliases.resolve_entity(
        aliases, "what did we agree with Cofers Grupo last quarter?") == support.SURVIVOR_ID
    assert 'entity: ["cofers"]' in support.remote_page(repo_env.bare, pages["absorbed_note_1"])


def test_the_pages_the_merge_did_not_name_are_byte_identical_afterwards(conn, repo_env):
    """**The benign twin for the whole kind.** A merge rewrites four files and re-anchors a page's
    whole history, so "it changed what it named" is only half the property — this is the half that
    says it changed nothing else."""
    pages = support.seed_duplicate_pair(repo_env)
    untouched = {path: support.remote_page(repo_env.bare, path)
                 for path in (support.NOTE_A, support.DECISION, pages["survivor_note"])}

    _apply(repo_env, _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"]))

    for path, before in untouched.items():
        assert support.remote_page(repo_env.bare, path) == before, f"{path} was rewritten by a merge"


def test_a_merge_apply_tells_the_gates_exactly_the_files_its_plan_names(conn, repo_env):
    """The told facts, observed rather than assumed. `derived_files` names the registry and ONLY
    the registry — `gate_zone` refuses an in-lane write that is not a `.md` page, and a permission
    wide enough for a second file is a permission for a file nothing validated. No deletion and no
    body rewrite are granted at all: a merge removes nothing and replaces no prose."""
    pages = support.seed_duplicate_pair(repo_env)
    ops = entity_alias.plan(repo_env.repo, pages["survivor"], pages["absorbed"])
    told = apply._lane_and_permission(schema.KIND_ENTITY_ALIAS, ops)

    assert told.derived_files == frozenset({"ops/entity-registry.json"})
    assert told.deletions_allowed == frozenset()
    assert told.body_rewrite_allowed == frozenset()
    assert told.provenance_pages == frozenset()
    assert set(told.expected_bytes) == {op["path"] for op in ops}
    assert told.lane == ("ops/", "wiki/entities/", "wiki/notes/")


def test_a_merge_with_an_extra_reanchored_page_is_refused_and_nothing_is_pushed(conn, repo_env):
    """**A plan that does not match the corpus, end to end.** An extra page in the re-anchor set is
    a page no finding named and no plan computed. The row lands `failed`, `main` does not move, and
    the page the extra op aimed at is byte-identical."""
    pages = support.seed_duplicate_pair(repo_env)
    ops = entity_alias.plan(repo_env.repo, pages["survivor"], pages["absorbed"])
    victim = pages["survivor_note"]
    victim_before = support.remote_page(repo_env.bare, victim)
    with open(os.path.join(repo_env.repo, *victim.split("/")), encoding="utf-8") as f:
        smuggled = f.read().replace('entity: ["cofers"]', 'entity: ["cofers", "smuggled"]')
    doctored = [*ops[:3], {schema.OP_KIND_KEY: entity_alias.OP_REANCHOR, "path": victim,
                           "expected_before_hash": "0" * 64, "planned_after": smuggled}, ops[-1]]
    repair = _repair(doctored, kind=schema.KIND_ENTITY_ALIAS, check="model-duplicate-entity",
                     rationale="a merge whose plan does not match the corpus")
    head_before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as excinfo:
        _apply_and_record(conn, repo_env, repair)

    assert support.remote_head(repo_env.bare) == head_before, "nothing may be pushed"
    assert support.remote_page(repo_env.bare, victim) == victim_before
    row = _ledger(conn)
    assert row["status"] == schema.STATUS_FAILED
    assert row["error"] == str(excinfo.value)
    assert entity_alias.PLAN_DRIFT_CODE in row["error"]


def test_a_merge_whose_absorbed_page_left_the_ops_is_refused_by_the_cross_check(conn, repo_env):
    """The path comparison alone cannot see this: a merge that re-anchors the absorbed entity's
    pages onto the survivor without marking it absorbed leaves TWO live identities and no record of
    the decision. The shape half of the cross-check is what says so."""
    pages = support.seed_duplicate_pair(repo_env)
    ops = entity_alias.plan(repo_env.repo, pages["survivor"], pages["absorbed"])
    # Every path the plan names EXCEPT the absorbed page, as modifications — the exact diff an ops
    # list that lost its retire op would produce, and one the path comparison cannot fault on its
    # own once `target_paths` matches.
    entries = [gitcmd.DiffEntry(status="M", path=path, old_mode="", new_mode="")
               for path in schema.target_paths(ops) if path != pages["absorbed"]]

    with pytest.raises(RepairError) as excinfo:
        apply._cross_check_entity_alias(entries, ops)

    assert pages["absorbed"] in str(excinfo.value)
    assert "retired by being superseded" in str(excinfo.value)


def test_the_merge_cross_check_passes_a_diff_that_does_mark_the_absorbed_page(conn, repo_env):
    """The benign twin: the shape check must not bounce the merge it exists to protect."""
    pages = support.seed_duplicate_pair(repo_env)
    ops = entity_alias.plan(repo_env.repo, pages["survivor"], pages["absorbed"])
    entries = [gitcmd.DiffEntry(status="M", path=path, old_mode="", new_mode="")
               for path in schema.target_paths(ops)]

    apply._cross_check_entity_alias(entries, ops)      # no raise


def test_a_corpus_that_moved_under_the_merge_refuses_rather_than_merging_the_old_plan(
        conn, repo_env):
    """A page that gained the absorbed entity's anchor after the plan was computed is a DIFFERENT
    merge, and performing the old one would leave that page anchored to a retired identity."""
    pages = support.seed_duplicate_pair(repo_env)
    repair = _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"])
    support.write_anchored_note(repo_env, "Arrived Later", entity_id=support.ABSORBED_ID)
    head_before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as excinfo:
        _apply(repo_env, repair)

    assert entity_alias.PLAN_DRIFT_CODE in str(excinfo.value)
    assert support.remote_head(repo_env.bare) == head_before


def test_a_credential_hidden_in_an_alias_the_merge_would_MOVE_is_vetoed(conn, repo_env):
    """This kind's own secrets surface, and it is the only one it has: a merge writes no prose, but
    it does COPY the absorbed entity's aliases onto the survivor's page, and an alias is free text
    somebody typed. So the one line a merge adds anywhere is a line that came from another page,
    and the gate has to see it as an addition to the page it lands on.

    The credential is already on `main` — it is the absorbed entity's alias — and the merge is what
    would put it somewhere new. Nothing is pushed, and the veto's sentence never reproduces it."""
    pages = support.seed_duplicate_pair(
        repo_env, absorbed_aliases=(f"Grupo {adversarial_payloads.GITHUB_PAT}",))
    repair = _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"])
    head_before = support.remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        _apply(repo_env, repair)

    assert "secrets/" in str(caught.value)
    assert adversarial_payloads.GITHUB_PAT not in str(caught.value), (
        "a refusal reproduced is a payload delivered twice")
    assert support.remote_head(repo_env.bare) == head_before


def test_the_commit_message_says_which_identity_absorbed_which(conn, repo_env):
    """Reading `git log` after a merge is how somebody finds out which identity absorbed which, so
    the subject line carries both names rather than a path and an op count — and the trailer names
    the finding, because nobody approved this."""
    pages = support.seed_duplicate_pair(repo_env)

    _apply(repo_env, _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"],
                                   finding_ids=(3,)))

    message = support.commit_message(repo_env.bare)
    assert "merge Cofers Holdings into Cofers" in message
    assert "Repair: model-duplicate-entity #3" in message


def test_an_applied_merge_reports_which_identity_survived_and_how_many_pages_moved(conn,
                                                                                   repo_env):
    """`paths` cannot say it: the two entity pages are two entries in one sorted list, and which
    absorbed which is the whole of what happened."""
    pages = support.seed_duplicate_pair(repo_env, anchored=2)

    result = _apply(repo_env, _merge_repair(repo_env.repo, pages["survivor"], pages["absorbed"]))

    assert result["survivor"] == pages["survivor"]
    assert result["absorbed"] == pages["absorbed"]
    assert result["reanchored_pages"] == 2
    # `diff` is a COLUMN of its own and deliberately outside this tuple, which names what the ledger
    # records per KIND; everything else an apply hands back has to be in it, or a surface reading
    # the result by name would silently render an empty cell for the half that matters.
    assert set(result) - {"diff"} <= set(apply.LEDGER_RESULT_KEYS)


def test_a_merge_diff_containing_an_ADDED_file_is_refused_by_the_cross_check(conn, repo_env):
    """**`gate_zone` delegates its creation bound to this, and the bound is the ABSENCE of a
    `return`.**

    The `entity-alias` kind is the one caller allowed to write a file that is not a page
    (`ctx.derived_files`, ADR 039's third amendment), and `gate_zone` says so in its own comment:
    the page-shape proof is suspended, and what still stops this kind from CREATING an arbitrary
    file is that every entry in its diff must be a modification. That is enforced by
    `_cross_check`'s `entity-alias` branch falling THROUGH to the shape check — the `delete` branch
    two lines above returns, this one deliberately does not — so one added `return` would remove a
    bound a gate is relying on and nothing else in the suite would notice.
    """
    pages = support.seed_duplicate_pair(repo_env)
    ops = entity_alias.plan(repo_env.repo, pages["survivor"], pages["absorbed"])
    paths = schema.target_paths(ops)
    entries = [gitcmd.DiffEntry(status=("A" if path == paths[0] else "M"),
                                path=path, old_mode="", new_mode="100644")
               for path in paths]

    with pytest.raises(RepairError) as excinfo:
        apply._cross_check(entries, {"target_paths": paths},
                           kind=schema.KIND_ENTITY_ALIAS, ops=ops)

    assert paths[0] in str(excinfo.value)
    assert "(A)" in str(excinfo.value), (
        "the refusal names the status, because 'something other than edit existing pages' is not "
        "actionable without saying which entry and what it was")
