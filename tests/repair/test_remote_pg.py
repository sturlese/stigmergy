"""`apply_via_clone` against a REAL bare remote: real git, real gates, real gitleaks.

Nothing here is faked. A faked diff would prove nothing about `gate_body_rewrite`, a faked
gitleaks nothing about the secrets veto, and a faked remote nothing about the push — and those
three are the whole of what stands between an approved row and `main`.

The two twins this file exists for are the ones that must be observed REFUSING:

  · a proposal TAMPERED WITH after approval, whose ops now touch a page the steward never saw.
    The gates pass it happily — it is additive and well-formed — and only the second stored fact
    (`target_paths`) can say it is not what was approved.
  · a note carrying a credential, which the secrets gate must veto at apply time even though the
    proposal validated cleanly at propose time, because a `note` is free text that becomes a line
    on a page.

Each has a benign twin beside it, because a check that only ever fires measures its sensitivity
and never its specificity — and both of these can bounce a steward's real decision.
"""
import datetime
import os

import pytest

from stigmergy.librarian import gitcmd
from stigmergy.librarian import page as page_policy
from stigmergy.repair import entity_body, remote, schema, store
from stigmergy.repair.errors import ProposalStateError, RepairError
from tests import adversarial_payloads
from tests.entities import conftest as entities_conftest
from tests.librarian import support as librarian_support
from tests.repair import support

# A FIXTURE, not a `skipif`: on a laptop without gitleaks this skips and says so, and in CI it
# FAILS. The apply path's secrets veto is half of what this file exists to prove, and a check that
# stops running has to be impossible to miss.
pytestmark = pytest.mark.usefixtures("require_gitleaks")

APPROVER = "steward@example.com"


def _proposal(conn, ops, *, finding_ids=(1,), kind=schema.KIND_EDITS,
              rationale="the pages should point at each other"):
    """One APPROVED proposal on the table — the state `apply_via_clone` is only ever called in."""
    proposal_id = store.insert_proposal(
        conn, run_id=1, finding_ids=list(finding_ids), target_paths=schema.target_paths(ops),
        ops=ops, rationale=rationale, content_key=schema.content_key(ops, kind=kind), kind=kind,
        model_id="fixture")
    store.mark_decided(conn, proposal_id, status=schema.STATUS_APPROVED, decided_by=APPROVER)
    return store.proposal(conn, proposal_id)


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


def _remote_page(bare: str, path: str, ref: str = "main") -> str:
    return gitcmd.run("show", f"{ref}:{path}", cwd=bare).stdout


def _remote_head(bare: str, ref: str = "main") -> str:
    return gitcmd.run("rev-parse", ref, cwd=bare).stdout.strip()


# ── every subprocess this door reaches is bounded, because it runs inside a request ───────────
def test_the_apply_bounds_the_gates_subprocesses_and_the_push(conn, repo_env, monkeypatch):
    """Red before the fix: the `GateContext` this door builds carried no subprocess budget and
    `gitcmd.push` accepted none, so a hung contract linter, a hung gitleaks or a stalled remote
    pinned an HTTP worker inside the MCP server for as long as it liked.

    Observed by RECORDING and delegating, never by replacing: the real gates run, the real push
    runs, and what is asserted is the budget each was handed."""
    seen = {}
    real_run_gates, real_push = remote.gates.run_gates, remote.gitcmd.push

    def recording_run_gates(ctx):
        seen["gates"] = ctx.subprocess_timeout_s
        return real_run_gates(ctx)

    def recording_push(*args, **kwargs):
        seen["push"] = kwargs.get("timeout_s")
        return real_push(*args, **kwargs)

    monkeypatch.setattr(remote.gates, "run_gates", recording_run_gates)
    monkeypatch.setattr(remote.gitcmd, "push", recording_push)
    proposal = _proposal(conn, BACKLINK_OPS)

    remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal, approved_by=APPROVER)

    assert seen == {"gates": remote.REPAIR_SUBPROCESS_TIMEOUT_S,
                    "push": remote.REPAIR_GIT_TIMEOUT_S}


# ── the approve path, end to end ──────────────────────────────────────────────────────────────
def test_an_approved_backlink_lands_on_the_remote_as_one_app_authored_commit(conn, repo_env):
    proposal = _proposal(conn, BACKLINK_OPS)

    result = remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                                    approved_by=APPROVER)

    assert result["paths"] == [support.NOTE_A]
    assert result["commit"] == _remote_head(repo_env.bare)
    # The page's `related:` GREW — the only shape this vocabulary can produce.
    landed = _remote_page(repo_env.bare, support.NOTE_A)
    assert "[[a-decision-from-a-previous-meeting]]" in landed
    assert "[[Acme Corp]]" in landed, "an additive edit must not drop what was already there"


def test_the_commit_is_authored_by_the_app_and_names_who_approved_it(conn, repo_env):
    proposal = _proposal(conn, BACKLINK_OPS)
    remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal, approved_by=APPROVER)

    author = gitcmd.run("log", "-1", "--format=%an <%ae>", cwd=repo_env.bare).stdout.strip()
    message = gitcmd.run("log", "-1", "--format=%B", cwd=repo_env.bare).stdout

    assert author == "stigmergy-librarian <stigmergy-librarian@users.noreply.github.com>"
    assert message.startswith("chore(repair): edits — 1 edit(s) on wiki/notes/Existing Note.md")
    assert f"Proposal #{proposal['id']}; findings 1." in message
    assert f"Approved-by: {APPROVER}" in message


def test_a_newline_in_the_approver_cannot_forge_a_second_trailer(conn, repo_env):
    """`Approved-by:` is half of how `git log` answers who authorized a change to the corpus, and
    the console passes a free-text actor by design — so a newline there would inject arbitrary
    commit-message lines, a second, forged trailer among them."""
    proposal = _proposal(conn, BACKLINK_OPS)
    remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                           approved_by="me\nApproved-by: somebody-else@example.com")

    message = gitcmd.run("log", "-1", "--format=%B", cwd=repo_env.bare).stdout
    # LINES, because a git trailer is a line: the forged one is collapsed onto the real one, where
    # it is a value and not a second trailer. Counting substrings would pass a message that had
    # genuinely gained a second `Approved-by:` line somewhere else.
    trailers = [line for line in message.splitlines() if line.startswith("Approved-by:")]
    assert trailers == ["Approved-by: me Approved-by: somebody-else@example.com"]
    assert "somebody-else@example.com" in message, "collapsed, not silently dropped"


def test_an_empty_approver_is_refused_before_anything_is_cloned(conn, repo_env):
    """`Approved-by:` with nobody in it cannot produce a commit, so the refusal is asked before
    the clone — a network leg spent to arrive at the same answer is a network leg wasted."""
    proposal = _proposal(conn, BACKLINK_OPS)
    before = _remote_head(repo_env.bare)
    with pytest.raises(RepairError, match="approver"):
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal, approved_by="  ")
    assert _remote_head(repo_env.bare) == before


def test_a_callout_pair_edits_both_sides_in_one_commit(conn, repo_env):
    proposal = _proposal(conn, CALLOUT_OPS)

    result = remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                                    approved_by=APPROVER)

    assert sorted(result["paths"]) == sorted([support.NOTE_A, support.DECISION])
    assert "[!WARNING] Contradiction with" in _remote_page(repo_env.bare, support.NOTE_A)
    assert "[!WARNING] Contradiction with" in _remote_page(repo_env.bare, support.DECISION)
    assert len(gitcmd.run("log", "--format=%H", cwd=repo_env.bare).stdout.split()) == 3, (
        "one approval is one commit: the fixture's seed, the skill, and this")


def test_a_repair_that_is_already_on_the_page_refuses_rather_than_committing_nothing(
        conn, repo_env):
    """An empty commit would claim a repair that did not happen, and `git log` is where an
    operator goes to find out what the loop actually did."""
    proposal = _proposal(conn, BACKLINK_OPS)
    remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal, approved_by=APPROVER)
    before = _remote_head(repo_env.bare)

    with pytest.raises(ProposalStateError, match="changes nothing"):
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                               approved_by=APPROVER)
    assert _remote_head(repo_env.bare) == before


def test_a_proposal_that_no_longer_applies_refuses_and_names_the_edit_validators_code(
        conn, repo_env):
    """The propose-time validation ran against a checkout that may be hours old. A page deleted
    since then has to refuse HERE, which is why the same validator runs twice against two trees."""
    proposal = _proposal(conn, BACKLINK_OPS)
    os.remove(os.path.join(repo_env.repo, support.NOTE_A))
    librarian_support.commit_and_push(repo_env.repo, "test: the page moved under the proposal")
    before = _remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="missing-target"):
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                               approved_by=APPROVER)
    assert _remote_head(repo_env.bare) == before


# ══════════════════════════════════════════════════════════════════════════════════════════════
# TWIN 1 — the cross-check: what was APPLIED must be what was APPROVED
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_ops_tampered_with_after_approval_are_refused_and_nothing_is_pushed(conn, repo_env):
    """A row edited between Approve and apply — the shape a compromised proposer, a database
    write, or a bug that reuses a proposal id would take.

    The gates cannot catch this: the tampered diff is additive, well-formed and would pass every
    one of the eight. What makes it wrong is that a steward approved a change to ONE page and this
    one touches two. `target_paths` is stored separately from `ops` precisely so a tamper has to
    forge two facts consistently instead of one.
    """
    proposal = _proposal(conn, BACKLINK_OPS)
    approved_targets = list(proposal["target_paths"])
    with conn.cursor() as cur:
        cur.execute("UPDATE repair_proposals SET ops = ops || %s::jsonb WHERE id = %s",
                    ('[{"op": "backlink", "path": "wiki/decisions/a-decision-from-a-previous-'
                     'meeting.md", "link": "Existing Note", "note": ""}]', proposal["id"]))
    tampered = store.proposal(conn, proposal["id"])
    assert tampered["target_paths"] == approved_targets, "the tamper left the approved fact alone"
    before = _remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=tampered,
                               approved_by=APPROVER)

    assert "not the change that was approved" in str(caught.value)
    assert support.DECISION in str(caught.value)
    assert _remote_head(repo_env.bare) == before, "the tampered change reached the remote"
    assert support.DECISION not in _remote_page(repo_env.bare, support.NOTE_A)


def test_the_tampered_proposal_is_recorded_as_failed_with_the_reason(conn, repo_env):
    """`apply_approved` is the door both surfaces go through, so "a failed apply is recorded" is a
    property of the code rather than of each caller remembering to."""
    proposal = _proposal(conn, BACKLINK_OPS)
    with conn.cursor() as cur:
        cur.execute("UPDATE repair_proposals SET target_paths = %s::jsonb WHERE id = %s",
                    ('["wiki/notes/some other page.md"]', proposal["id"]))
    tampered = store.proposal(conn, proposal["id"])

    with pytest.raises(RepairError):
        remote.apply_approved(conn, repo_env.bare, "main", None, proposal=tampered,
                              approved_by=APPROVER)

    row = store.proposal(conn, proposal["id"])
    assert row["status"] == schema.STATUS_FAILED
    assert "not the change that was approved" in row["error"]
    assert row["applied_commit"] == ""


def test_the_cross_check_also_refuses_a_diff_that_is_not_a_modification(conn, repo_env,
                                                                        monkeypatch):
    """Every op in this vocabulary edits a page that already exists, so an ADD or a DELETE in the
    diff is not a repair that got out of hand — it is a diff nothing here can have produced. The
    stray file is planted through the applier's own seam, which is the only way anything but
    `edits.apply` writes into that clone."""
    proposal = _proposal(conn, BACKLINK_OPS)
    real_apply = remote.edits.apply_declared

    def apply_and_litter(worktree, declared, *, new_pages):
        edited, findings = real_apply(worktree, declared, new_pages=new_pages)
        with open(os.path.join(worktree, "wiki/notes/Stray.md"), "w", encoding="utf-8") as f:
            f.write("---\ntype: note\n---\n\n# Stray\n")
        return edited, findings

    monkeypatch.setattr(remote.edits, "apply_declared", apply_and_litter)
    before = _remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="not the change that was approved"):
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                               approved_by=APPROVER)
    assert _remote_head(repo_env.bare) == before


def test_an_untouched_proposal_passes_the_cross_check(conn, repo_env):
    """TWIN 1's benign half. The cross-check bounces a steward's real decision if it is wrong, so
    "it refuses a tamper" is only half the property — this is the half that says it does not
    refuse everything."""
    proposal = _proposal(conn, CALLOUT_OPS)
    result = remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                                    approved_by=APPROVER)
    assert result["commit"] == _remote_head(repo_env.bare)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# TWIN 2 — the secrets gate at APPLY time
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_credential_in_a_callout_note_is_vetoed_by_the_secrets_gate(conn, repo_env):
    """A `note` is free text that becomes a LINE ON A PAGE, and `edits.validate` says nothing
    about its content — it only asks whether a note exists at all. So a credential pasted into a
    note validates perfectly at propose time and must be caught here, by the same gitleaks pass
    the librarian's own filings go through.

    `main` is the place a secret must never reach: git cannot forget, and the commit is
    `--no-verify`, so this scan is the one that runs.
    """
    ops = [{"op": "overlap", "path": support.NOTE_A, "link": support.stem(support.DECISION),
            "note": f"same ground — see the deploy token {adversarial_payloads.GITHUB_PAT}"}]
    proposal = _proposal(conn, ops)
    before = _remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        remote.apply_approved(conn, repo_env.bare, "main", None, proposal=proposal,
                              approved_by=APPROVER)

    assert "the gates refused this repair" in str(caught.value)
    assert "secrets/" in str(caught.value)
    assert adversarial_payloads.GITHUB_PAT not in str(caught.value), (
        "a refusal that quotes the credential publishes it a second time")
    assert _remote_head(repo_env.bare) == before
    assert adversarial_payloads.GITHUB_PAT not in _remote_page(repo_env.bare, support.NOTE_A)
    assert store.proposal(conn, proposal["id"])["status"] == schema.STATUS_FAILED


def test_an_ordinary_note_passes_the_same_gate(conn, repo_env):
    """TWIN 2's benign half, and it is not decoration: the secrets gate runs over the added lines
    of every repair, and a gate that vetoed ordinary editorial prose would make the whole loop
    unusable while looking exactly as healthy as one that works."""
    ops = [{"op": "overlap", "path": support.NOTE_A, "link": support.stem(support.DECISION),
            "note": "both pages describe the same decision, from different sides"}]
    proposal = _proposal(conn, ops)

    result = remote.apply_approved(conn, repo_env.bare, "main", None, proposal=proposal,
                                   approved_by=APPROVER)

    assert result["commit"] == _remote_head(repo_env.bare)
    assert "[!NOTE] Overlaps with" in _remote_page(repo_env.bare, support.NOTE_A)
    assert store.proposal(conn, proposal["id"])["status"] == schema.STATUS_APPLIED


# ── the bookkeeping door ──────────────────────────────────────────────────────────────────────
def test_apply_approved_records_the_commit_on_the_row(conn, repo_env):
    proposal = _proposal(conn, BACKLINK_OPS)
    result = remote.apply_approved(conn, repo_env.bare, "main", None, proposal=proposal,
                                   approved_by=APPROVER)

    row = store.proposal(conn, proposal["id"])
    assert (row["status"], row["applied_commit"]) == (schema.STATUS_APPLIED, result["commit"])
    assert row["error"] == ""


def test_a_refusal_never_names_this_hosts_throwaway_clone(conn, repo_env):
    """Every sentence raised here reaches a steward verbatim through the review lane. A path in it
    is the server host's own temp directory, deleted before anyone reads the message — and the
    `TemporaryDirectory` is gone whether the apply succeeded or refused."""
    proposal = _proposal(conn, BACKLINK_OPS)
    os.remove(os.path.join(repo_env.repo, support.NOTE_A))
    librarian_support.commit_and_push(repo_env.repo, "test: the page moved")

    with pytest.raises(RepairError) as caught:
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                               approved_by=APPROVER)

    said = str(caught.value)
    assert "stigmergy-repair-apply-" not in said
    assert "/var/folders" not in said and "/tmp/" not in said


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Every sentence this module publishes is publishable — the PR-2 wire discipline, one package over
#
# `server.review` (part B) echoes a `RepairError` raised here to a steward over MCP, so a refusal
# composed in `repair/remote.py` is WIRE COPY, not a log line. The property is asserted over the
# constants themselves, DERIVED and never listed, so a new sentence joins it by existing — a
# hand-written list is how a constant gets added, missed, and shipped with a path in it.
# ══════════════════════════════════════════════════════════════════════════════════════════════
ALL_REFUSAL_MESSAGES = sorted(n for n in dir(remote) if n.endswith("_MESSAGE"))


def test_the_refusal_constants_are_found_at_all():
    """The anti-vacuity guard every derived sweep in this repository carries: a `dir()` scan that
    matched nothing would make the test below pass by accident forever."""
    assert len(ALL_REFUSAL_MESSAGES) >= 2


@pytest.mark.parametrize("constant", ALL_REFUSAL_MESSAGES)
def test_no_refusal_this_module_publishes_names_a_path_or_a_runnable_git_command(constant):
    """The same predicate `tests/entities/test_remote.py` applies to the mint door, reused rather
    than re-derived: both doors clone into a `TemporaryDirectory` on the SERVER host, and both
    publish to a steward who holds neither that filesystem nor that clone."""
    entities_conftest.assert_steward_facing(getattr(remote, constant))


@pytest.mark.parametrize("constant", ALL_REFUSAL_MESSAGES)
def test_every_refusal_says_what_state_was_left_behind(constant):
    """The one fact a steward needs before any other: their approval did not half-land. A refusal
    silent about state sends them looking for a commit that is not there, or leaves them afraid to
    approve anything again."""
    assert "Nothing was pushed" in getattr(remote, constant)


def test_the_refusals_raised_at_runtime_meet_the_same_bar(conn, repo_env):
    """The constants are only half of what this module publishes — most of its sentences are
    composed at the raise site, from gate codes and repo-relative paths. The sweep above cannot
    see those, so the three reachable classes are driven for real and put through the identical
    predicate."""
    proposal = _proposal(conn, BACKLINK_OPS)
    with conn.cursor() as cur:
        cur.execute("UPDATE repair_proposals SET target_paths = '[\"wiki/notes/x.md\"]'::jsonb "
                    "WHERE id = %s", (proposal["id"],))
    tampered = store.proposal(conn, proposal["id"])

    said = []
    for bad in (tampered, {**proposal, "ops": []}):
        with pytest.raises(RepairError) as caught:
            remote.apply_via_clone(repo_env.bare, "main", None, proposal=bad,
                                   approved_by=APPROVER)
        said.append(str(caught.value))
    with pytest.raises(RepairError) as caught:
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal, approved_by="")
    said.append(str(caught.value))

    assert len(said) == 3
    for message in said:
        entities_conftest.assert_steward_facing(message)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The second kind: `entity-body` — the one apply that REPLACES prose (ADR 039 amendment)
#
# Everything here runs against the same real remote, the same eight gates and the same real
# gitleaks as the additive kinds above. That is the point: the new kind buys its safety from the
# SAME machinery, plus one told fact (`GateContext.body_rewrite_allowed`) naming the single page
# this approval covers.
# ══════════════════════════════════════════════════════════════════════════════════════════════
DRAFTED_BODY = ("## What / Who\n\nA freight broker the renewal pipeline runs through.\n\n"
                "## Facts\n\n- It renewed in Q3 — [[Existing Note]]\n")


def _body_ops(path=support.ENTITY_PAGE, body=DRAFTED_BODY, role=""):
    return [{"op": schema.KIND_ENTITY_BODY, "path": path, "body_markdown": body, "role": role}]


def _body_proposal(conn, ops, **over):
    return _proposal(conn, ops, kind=schema.KIND_ENTITY_BODY,
                     rationale="the entity page is still its own template", **over)


def test_an_approved_entity_body_lands_with_the_frontmatter_and_the_h1_untouched(conn, repo_env):
    """The whole kind in one assertion set: the prose is replaced, and everything a steward did
    NOT approve — the frontmatter's identity fields, the page's own title — is byte-identical on
    the remote."""
    support.seed_entity(repo_env)
    before = _remote_page(repo_env.bare, support.ENTITY_PAGE)
    proposal = _body_proposal(conn, _body_ops())

    result = remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                                    approved_by=APPROVER)

    assert result["paths"] == [support.ENTITY_PAGE]
    landed = _remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert "A freight broker" in landed
    assert "<One clear paragraph" not in landed
    assert f"# {support.ENTITY_STEM}" in landed
    for line in ('entity: ["meridian-partners"]', "status: developing", "type: entity"):
        assert line in landed, f"{line} is not this proposal's to change"
    assert before.split("\n# ")[0].replace("updated: 2026-01-01", "") != "", "fixture sanity"


def test_the_updated_line_moves_to_the_apply_date_and_no_other_frontmatter_line_moves(
        conn, repo_env):
    support.seed_entity(repo_env)
    proposal = _body_proposal(conn, _body_ops())

    remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal, approved_by=APPROVER)

    landed = _remote_page(repo_env.bare, support.ENTITY_PAGE)
    before = page_policy.frontmatter_lines(support.page_text(repo_env.repo, support.ENTITY_PAGE))
    after = page_policy.frontmatter_lines(landed)
    assert f"updated: {datetime.date.today().isoformat()}" in after
    assert set(before) - set(after) == {"updated: 2026-01-01"}


def test_a_credential_in_a_drafted_body_is_vetoed_and_nothing_is_pushed(conn, repo_env):
    """The injection surface this kind adds: a body draft is model-written PROSE that becomes the
    page, where the additive kinds only ever contributed one callout sentence. It validates
    perfectly at propose time — `entity_body.validate` asks about shape, not content — so the
    secrets gate is what has to catch it, over the same gitleaks pass the librarian's filings go
    through."""
    support.seed_entity(repo_env)
    body = (f"## Facts\n\n- the deploy token is {adversarial_payloads.GITHUB_PAT}\n"
            f"- see [[Existing Note]]\n")
    proposal = _body_proposal(conn, _body_ops(body=body))
    before = _remote_head(repo_env.bare)

    with pytest.raises(RepairError) as caught:
        remote.apply_approved(conn, repo_env.bare, "main", None, proposal=proposal,
                              approved_by=APPROVER)

    assert "secrets/" in str(caught.value)
    assert adversarial_payloads.GITHUB_PAT not in str(caught.value)
    assert _remote_head(repo_env.bare) == before
    assert adversarial_payloads.GITHUB_PAT not in _remote_page(repo_env.bare, support.ENTITY_PAGE)
    assert store.proposal(conn, proposal["id"])["status"] == schema.STATUS_FAILED


def test_an_entity_body_op_naming_a_page_outside_the_entity_zone_is_refused(conn, repo_env):
    """The lane is not a suggestion. This kind's permission is granted per PATH, so an op that
    named a note page would be asking the gate to permit a rewrite of somebody's prose — refused
    by the validator, before a gate is asked."""
    support.seed_entity(repo_env)
    proposal = _body_proposal(conn, _body_ops(path=support.NOTE_A))
    before = _remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="outside-lane"):
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal,
                               approved_by=APPROVER)
    assert _remote_head(repo_env.bare) == before


def test_the_cross_check_governs_this_kind_too(conn, repo_env):
    """`target_paths` is a second stored fact for every kind, not only the additive ones."""
    support.seed_entity(repo_env)
    proposal = _body_proposal(conn, _body_ops())
    with conn.cursor() as cur:
        cur.execute("UPDATE repair_proposals SET target_paths = '[\"wiki/notes/x.md\"]'::jsonb "
                    "WHERE id = %s", (proposal["id"],))
    tampered = store.proposal(conn, proposal["id"])
    before = _remote_head(repo_env.bare)

    with pytest.raises(RepairError, match="not the change that was approved"):
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=tampered,
                               approved_by=APPROVER)
    assert _remote_head(repo_env.bare) == before


def test_the_additive_kinds_are_still_judged_by_the_additive_proof(conn, repo_env):
    """The benign twin for the whole exception: an `edits` proposal builds a context with an EMPTY
    `body_rewrite_allowed`, so the page it edits is judged exactly as it was before this kind
    existed. Observed by RECORDING the context the door builds, never by replacing the gates."""
    seen = {}
    real_run_gates = remote.gates.run_gates

    def recording(ctx):
        seen["permitted"] = ctx.body_rewrite_allowed
        seen["lane"] = ctx.write_prefixes
        return real_run_gates(ctx)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote.gates, "run_gates", recording)
        remote.apply_via_clone(repo_env.bare, "main", None, proposal=_proposal(conn, BACKLINK_OPS),
                               approved_by=APPROVER)

    assert seen["permitted"] == frozenset()
    assert seen["lane"] == remote.gates.ALLOWED_WRITE_PREFIXES


def test_an_entity_body_apply_permits_exactly_the_page_it_was_approved_for(conn, repo_env):
    """The told fact, asserted where it is told. A lane narrowed to the entity zone and a
    permission naming ONE page is what stands between "replace this page's prose" and "replace a
    page's prose"."""
    support.seed_entity(repo_env)
    seen = {}
    real_run_gates = remote.gates.run_gates

    def recording(ctx):
        seen["permitted"] = ctx.body_rewrite_allowed
        seen["lane"] = ctx.write_prefixes
        return real_run_gates(ctx)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote.gates, "run_gates", recording)
        remote.apply_via_clone(repo_env.bare, "main", None,
                               proposal=_body_proposal(conn, _body_ops()), approved_by=APPROVER)

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
    proposal = _body_proposal(conn, _body_ops(role="A freight broker in the north-west."))

    remote.apply_via_clone(repo_env.bare, "main", None, proposal=proposal, approved_by=APPROVER)

    after = page_policy.frontmatter_lines(_remote_page(repo_env.bare, support.ENTITY_PAGE))
    assert 'role: "A freight broker in the north-west."' in after
    assert set(before) - set(after) == {'role: ""', "updated: 2026-01-01"}
