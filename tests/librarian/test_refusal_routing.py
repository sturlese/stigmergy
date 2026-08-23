"""`processing._refuse`: which terminal state a surviving veto earns, what it tells the submitter,
and why.

OLD BEHAVIOUR: there were TWO routers — `_refuse` and `_refuse_meeting` — and every case below ran
against both, because the defect each one was written for had been duplicated verbatim across the
pair and a fix to one site alone would have shipped green. The meeting flow is gone and
`_route_refusal` is the one road a veto takes, so the parametrization has no second side to
disagree with; what each case asserts is unchanged.

The routing that turns a set of findings into a terminal state reads gates and codes, and most of it
is proven end to end in `test_processing_pg.py`, over a real queue, a real git repo and the offline
double. That is the right place for a refusal the double can actually stage. Two kinds of routing
are left over, and this module is for both of them.

**The branch the double cannot reach.** `zone/type-not-creatable` is a defensive veto:
`gates._check_created_type` documents that both derived views of `page.PAGE_TYPES` currently agree,
so `ensure_creatable` cannot raise for a type `type_for_folder` just returned, and no capture — and
no double directive — can produce the finding today. Its destination is still pinned: a page of a
type the fast lane may not create is the LIBRARIAN's fault (the brief tells it which three types
exist and how to introduce an entity instead), so it is `failed`, never `rejected` onto a submitter
who did nothing wrong — and never parked, because nothing parks any more.

The future is simulated with ONE line — a governed type given a folder
(`FOLDER_BY_TYPE["meeting"]`) — which is exactly the table change that makes the guard live.

**The sentence a person actually receives.** The secrets section at the bottom is the opposite case:
an ordinary, reachable refusal, where the routing decides not just the terminal state but WHERE it
tells a submitter their credential is. Those tests write real pages and shell out to the real
gitleaks binary (so this module skips, loudly in CI, without it — see `require_gitleaks`), because a
finding a test author typed out proves only that the router handles a shape someone imagined.
"""
import json
from types import SimpleNamespace

import pytest

from stigmergy.capture import schema
from stigmergy.librarian import gates, processing
from stigmergy.librarian import page as page_policy
from tests import adversarial_payloads as payloads

ITEM = {"id": 7, "attempts": 1, "submitted_by": "someone@acme.test"}
OUTCOME = SimpleNamespace(summary="it reads like an identity, so it went to the entities folder",
                          findings=[])
GOVERNED_PAGE = "wiki/entities/Renewal Sync.md"


@pytest.fixture()
def governed_folder(monkeypatch):
    """An `entity` type that has a folder but is still not creatable — the disagreement between
    `page.PAGE_TYPES`' two derived views that `zone/type-not-creatable` exists to catch.

    `entity` is the type this gate exists for: `wiki/entities/` is a real folder with a real
    writer, and `FOLDER_BY_TYPE` leaves it out precisely so the fast lane cannot reach it. Patching
    it IN is therefore the exact disagreement the gate is defensive about — the two derived views
    of one table disagreeing — and not a typo nothing would ever produce. `_BY_NAME` is left alone,
    so `classify_page_type("entity")` still answers "known, not creatable" and the refusal the
    submitter reads still carries the identity writer's own reason.
    """
    monkeypatch.setitem(page_policy.FOLDER_BY_TYPE, "entity", "wiki/entities")


def _type_veto(governed_page: str = GOVERNED_PAGE) -> gates.Finding:
    """The finding, from the real gate helper rather than typed out here.

    `gate_zone` itself cannot be used: it filters on `ALLOWED_WRITE_PREFIXES`, a tuple computed at
    import from the same table, so under the patch above the page reads as `outside-lane` instead.
    The helper is where the type half lives and is what a page inside a known folder reaches.
    """
    ctx = gates.GateContext(worktree="", entries=[], added=[], material="",
                            outcome=SimpleNamespace(page_type="entity"), registry=None)
    findings = gates._check_created_type(ctx, governed_page)
    assert [f.code for f in findings] == [gates.TYPE_NOT_CREATABLE]
    return findings[0]


def test_a_type_the_fast_lane_may_not_create_is_the_librarians_fault_never_filed_never_parked(
        governed_folder):
    """OLD BEHAVIOUR: this veto parked the capture in `triage` with "where does a meeting page
    belong?" for a steward. There is no park: the agent was told the three creatable types and how
    to PROPOSE an entity for anything else, so a governed page in the diff is the librarian
    misbehaving — `failed`, naming the stage, with the submitter told their material is fine."""
    result = processing._refuse(ITEM, [_type_veto()], OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED
    assert result.result_ref == ""
    assert "not your capture" in result.report["summary"]
    assert "zone" in result.report["summary"]


def test_a_traceable_steering_attempt_beside_the_type_veto_stays_content_actionable(
        governed_folder):
    """The precedence this park sits under, asserted rather than assumed. `_refuse` routes a zone
    veto WITH a traceable steering attempt in the material to `rejected` before any park is
    considered, and that ordering is right here too: a document that asked to be filed as an entity
    page, and got the librarian to write into a governed folder, is something the submitter can act
    on. The park is for the librarian's own misjudgment, which is the case with no category."""
    steered = SimpleNamespace(summary="", findings=[{"category": "declare-canonical"}])

    result = processing._refuse(ITEM, [_type_veto()], steered, agent_attempts=2)

    assert result.status == schema.REJECTED
    assert "declare-canonical" in result.report["summary"]
    assert GOVERNED_PAGE in result.report["summary"]


# ── an unresolved anchor that survives the corrective pass is the librarian's fault ──────────
# OLD BEHAVIOUR: an anchoring veto that was the WHOLE story parked the capture on a steward
# (`_unanchorable`, with a companion dead link admitted when it named the same entity). The brief
# now offers the agent a third outcome that FILES — introduce the entity — so an anchor that still
# resolves to nothing after the retry is a model that did not take it: `failed`, never a park.
ANCHOR_PAGE = "wiki/notes/Acme Ventures Inc.md"


def _anchor_veto(*names: str) -> gates.Finding:
    names = names or ("Acme Ventures Inc",)
    return gates.Finding("anchoring", gates.ANCHORING_UNRESOLVED,
                         f'declared entity anchor {names!r} does not resolve in the entity '
                         f"registry read at the base commit",
                         locator=names[0], values=tuple(names))


def _dead_link_veto(target: str, path: str = ANCHOR_PAGE) -> gates.Finding:
    return gates.Finding("contract", gates.DEAD_LINKS_CHECK,
                         f"{path}: dead link: [[{target}]]", locator=path)


def test_an_unresolved_anchor_after_the_retry_fails_as_the_librarians_fault():
    result = processing._refuse(ITEM, [_anchor_veto()], OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED
    assert "anchoring" in result.report["summary"]
    assert "not your capture" in result.report["summary"]


def test_an_unresolved_anchor_beside_its_own_dead_link_fails_the_same_way():
    """The shape the old park admitted specially — the agent wrote `[[Acme Ventures Inc]]` AND
    declared it — is no special case now: two vetoes, one `failed`."""
    veto = [_anchor_veto("Acme Ventures Inc"), _dead_link_veto("Acme Ventures Inc")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


def test_dead_link_target_parses_the_linters_own_message_shape():
    finding = _dead_link_veto("Acme Ventures Inc")
    assert gates.dead_link_target(finding) == "Acme Ventures Inc"


def test_dead_link_target_is_empty_for_a_finding_that_does_not_match_the_shape():
    finding = gates.Finding("contract", gates.DEAD_LINKS_CHECK, "not the expected shape",
                            locator=ANCHOR_PAGE)
    assert gates.dead_link_target(finding) == ""


# ── content-caused unparseable frontmatter routes to `rejected` ────────────────────────────────
def test_frontmatter_unparseable_alone_is_content_caused_not_a_system_fault():
    veto = [gates.Finding("frontmatter", "unparseable",
                          "wiki/notes/A.md: its frontmatter is not valid YAML",
                          locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_MALFORMED_FRONTMATTER


def test_frontmatter_unparseable_beside_another_veto_stays_a_system_fault():
    """The same "whole story" discipline `_uncreatable_type`/`_unanchorable` use: a second,
    unrelated finding means this is not provably just the submitter's shape mistake."""
    veto = [gates.Finding("frontmatter", "unparseable", "…", locator="wiki/notes/A.md"),
           gates.Finding("binary", "binary-page", "a NUL byte", locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


# ── the same asymmetry, closed for the gate's other two codes too ──────────────────────────────
def test_a_forged_field_alone_is_content_caused_not_a_system_fault():
    """The asymmetry that used to stand: `forged-field` from the SAME gate as `unparseable`
    used to route to `failed` ("the librarian broke") for material that tried to assert a
    server-owned field itself."""
    veto = [gates.Finding("frontmatter", "forged-field",
                          "wiki/notes/A.md: its 'owner' is not the value the server stamped",
                          locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_MALFORMED_FRONTMATTER
    # The copy used to tell the submitter the server "computes" `verification` and "fills it in
    # from what it actually verifies", both false: nothing computes a verification verdict.
    # Asserted on the two things that must survive any rewording — it names the refusal and it
    # does NOT promise a verdict is computed.
    summary = result.report["summary"]
    assert "declared a frontmatter field it may not assert" in summary
    assert "nothing computes" in summary and "actually verifies" not in summary


def test_a_forbidden_field_alone_is_content_caused_not_a_system_fault():
    veto = [gates.Finding("frontmatter", "forbidden-field",
                          "wiki/notes/A.md: the filed page still declares 'owner'",
                          locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_MALFORMED_FRONTMATTER


def test_a_forged_field_and_an_unparseable_finding_together_still_reject_with_the_forged_prose():
    """Both codes from the SAME gate, still the whole veto — content-caused either way. The
    forged-field prose is the one that names what actually happened (a server-owned field was
    declared), so it wins over the generic malformed-shape sentence when both are present."""
    veto = [gates.Finding("frontmatter", "unparseable", "…", locator="wiki/notes/A.md"),
           gates.Finding("frontmatter", "forged-field", "declares 'owner'",
                        locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_MALFORMED_FRONTMATTER
    # The copy used to tell the submitter the server "computes" `verification` and "fills it in
    # from what it actually verifies", both false: nothing computes a verification verdict.
    # Asserted on the two things that must survive any rewording — it names the refusal and it
    # does NOT promise a verdict is computed.
    summary = result.report["summary"]
    assert "declared a frontmatter field it may not assert" in summary
    assert "nothing computes" in summary and "actually verifies" not in summary


def test_a_forged_field_beside_a_veto_from_another_gate_still_fails():
    """Part of the duplicate-key catch lives INSIDE this gate, which makes this combination MORE
    likely — the "whole story" guard must still hold: a second, unrelated gate's veto means this
    is not provably just the submitter's frontmatter mistake."""
    veto = [gates.Finding("frontmatter", "forged-field", "declares 'owner'",
                         locator="wiki/notes/A.md"),
           gates.Finding("binary", "binary-page", "a NUL byte", locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


# ── the zone-plus-category branch must not route an UNREPAIRABLE zone finding to
# `rejected`/steering. `zone/modification-refused`'s own documented meaning is "no producer inside
# this flow" — a system fault, never something the material's injected text could have caused — and
# `zone/body-rewrite` (always `repairable=False`) shares that class. A coincident declared category
# must not relabel either as the submitter's doing; both fall through to `failed_system` exactly
# like a zone veto with no category at all.
#
# OLD BEHAVIOUR: these three ran against `_refuse_meeting`, because the branch was written for the
# meeting flow's own refusal. There is one router, and the CODE keeps its name
# (`gates.py` says why: a preserved refused diff on a deployed stack already carries it in its
# `# refused by:` header). ────────────────────────────────────────────────────────────────────────
UNREPAIRABLE_ITEM = {"id": 9, "attempts": 1}
STEERED_CATEGORY = SimpleNamespace(summary="", findings=[{"category": "declare-canonical"}])


def test_meeting_edit_refused_beside_a_declared_category_still_fails_as_a_system_fault():
    """The exploit this guards: a transcript with injection-looking text (so the agent declares a
    category) coincides with an anomalous `M` in the worktree. Before this fix the refusal landed
    as `rejected`/steering, naming the SUBMITTER's capture as the cause; the honest cause is a
    worker defect or worktree interference, which is what `failed_system` says."""
    veto = [gates.Finding("zone", "modification-refused",
                         "modified wiki/notes/A.md: no edit mechanism exists here",
                         locator="wiki/notes/A.md", repairable=False)]

    result = processing._refuse(UNREPAIRABLE_ITEM, veto, STEERED_CATEGORY, agent_attempts=1)

    assert result.status == schema.FAILED, (
        f"an unrepairable zone/modification-refused finding must route to failed_system even "
        f"beside a declared injection category — got {result.status!r}, "
        f"report={result.report!r}")
    assert schema.REASON_STEERING not in json.dumps(result.report)


def test_body_rewrite_beside_a_declared_category_still_fails_as_a_system_fault():
    """Same class, deliberately included (not scope creep): `zone/body-rewrite` diagnoses work
    `edits.apply_declared` did on the agent's behalf and is always `repairable=False`, so it is
    exactly as much a system fault as `modification-refused` is — and, unlike that one, a caller
    can still reach it, which is what makes this the pair's live half."""
    veto = [gates.Finding("zone", "body-rewrite",
                         "rewrote existing content in wiki/notes/A.md",
                         locator="wiki/notes/A.md", repairable=False)]

    result = processing._refuse(UNREPAIRABLE_ITEM, veto, STEERED_CATEGORY, agent_attempts=1)

    assert result.status == schema.FAILED, (
        f"an unrepairable zone/body-rewrite finding must route to failed_system even beside a "
        f"declared injection category — got {result.status!r}, report={result.report!r}")
    assert schema.REASON_STEERING not in json.dumps(result.report)


def test_a_repairable_zone_finding_beside_a_declared_category_still_routes_as_steering():
    """The negative space the branch above must not break: a REPAIRABLE zone finding beside a
    declared category is exactly the case this branch exists for — the agent's own material may
    really have steered it — so it must still reach `rejected`/steering (see
    `test_a_traceable_steering_attempt_beside_the_type_veto_stays_content_actionable`)."""
    veto = [gates.Finding("zone", "outside-lane",
                         "wrote wiki/entities/Rogue.md, which is outside the fast lane's "
                         "folders", locator="wiki/entities/Rogue.md")]

    result = processing._refuse(UNREPAIRABLE_ITEM, veto, STEERED_CATEGORY, agent_attempts=1)

    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_STEERING


# ── the secrets locator: what the person is told to go and look at ────────────────────────────
#
# `gates._secret_findings` emits two shapes. `Finding.values` carries `(line, rule)` so this
# routing never has to recover either by re-reading the sentence the gate printed — the same
# contract `_pre_agent` already honours. The REJOINED shape (a credential only visible once
# adjacent lines were rejoined) carries an empty line and a locator with no line number in it,
# because no single line points at the value.


@pytest.fixture()
def rejoined_secret_veto(tmp_path, require_gitleaks):
    """The rejoined finding, from the real scanner rather than typed out here.

    A hand-built `Finding` would prove only that the router handles a shape a test author
    imagined. `scan_worktree_files` is what actually reaches `_refuse`, and the split payload is
    the one case where it has no line number to hand over.
    """
    page = tmp_path / "wiki" / "notes" / "Renewal Terms.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"# Notes\n\nwe agreed on {payloads.GITHUB_PAT_SPLIT_ACROSS_LINES} "
                    f"before lunch\n", encoding="utf-8")
    findings = gates.scan_worktree_files(str(tmp_path), ["wiki/notes/Renewal Terms.md"],
                                         gitleaks_bin="gitleaks")
    assert [f.code for f in findings] == ["secret"]
    assert findings[0].values == ("", "github-pat"), (
        f"this fixture must stage the REJOINED shape — an empty line, and the bare page as the "
        f"locator; got values={findings[0].values!r} locator={findings[0].locator!r}")
    return findings


@pytest.fixture()
def rejoined_secret_veto_on_an_edited_page(require_gitleaks):
    """The SECOND surface the same defect reaches, where the locator is not a path at all.

    `gate_secrets`' edited-page branch scans the added lines with `label="the drafted page"`, and
    `scan_secrets` uses that label as the locator — so the rejoined finding there carries neither a
    line NOR a path. It matters because it is what separates a real fix from one that only looks
    for a `/` or a `:` before deciding whether the locator holds a line number.
    """
    findings = gates.scan_secrets(
        f"we agreed on {payloads.GITHUB_PAT_SPLIT_ACROSS_LINES} before lunch",
        gitleaks_bin="gitleaks", label="the drafted page")
    assert [f.code for f in findings] == ["secret"]
    assert findings[0].values == ("", "github-pat"), (
        f"this fixture must stage the REJOINED shape; got values={findings[0].values!r} "
        f"locator={findings[0].locator!r}")
    return findings


@pytest.fixture()
def one_line_secret_veto(tmp_path, require_gitleaks):
    """The benign twin's finding: an ordinary hit that really does sit on one line."""
    page = tmp_path / "wiki" / "notes" / "Plain Terms.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"key: {payloads.GITHUB_PAT}\n", encoding="utf-8")
    findings = gates.scan_worktree_files(str(tmp_path), ["wiki/notes/Plain Terms.md"],
                                         gitleaks_bin="gitleaks")
    assert [f.code for f in findings] == ["secret"]
    return findings


@pytest.mark.parametrize("veto_fixture", ["rejoined_secret_veto",
                                          "rejoined_secret_veto_on_an_edited_page"],
                         ids=["new-page", "edited-page"])
def test_a_secret_split_across_a_line_break_is_not_reported_as_a_line_number(
        veto_fixture, request):
    """OLD BEHAVIOUR: the router recovered the line with `locator.rsplit(":", 1)[-1]`. For the
    rejoined shape the locator IS the page path and holds no line, so that expression returned the
    path — and the submitter was told the credential sat "near line wiki/notes/Renewal Terms.md".
    `rejected_secret`'s "split across a line break" branch, written for precisely this finding,
    was unreachable from here. This reason code purges the person's material immediately, so the
    locator in this sentence is the only thing they have left to act on.
    """
    result = processing._refuse(ITEM, request.getfixturevalue(veto_fixture), OUTCOME,
                                agent_attempts=2)

    summary = result.report["summary"]
    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_SECRET
    assert "split across a line break" in summary, summary
    assert "near line" not in summary, summary
    assert "wiki/notes" not in summary, summary
    assert "(rule: github-pat)" in summary, summary


def test_an_ordinary_secret_still_names_the_line_its_author_wrote(one_line_secret_veto):
    """The benign twin. The fix must not flatten every secret refusal into "somewhere in the page":
    a hit that does sit on one line keeps that line, because the number is most of the message's
    value to whoever has to go and remove the credential."""
    result = processing._refuse(ITEM, one_line_secret_veto, OUTCOME, agent_attempts=2)

    summary = result.report["summary"]
    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_SECRET
    assert "near line 1 of the drafted page" in summary, summary
    assert "split across a line break" not in summary, summary
    assert "(rule: github-pat)" in summary, summary


def test_every_secrets_finding_carries_the_pair_the_routing_unpacks(
        rejoined_secret_veto, one_line_secret_veto, rejoined_secret_veto_on_an_edited_page):
    """The invariant the routing depends on, pinned rather than left to inspection.

    It reads `line, rule = secret.values`, so a secrets finding that reached it without a
    2-tuple would raise inside the worker — and `worker._finish` keys the immediate purge on the
    reason code, so a crash here turns the one capture that must not linger into a `failed` row
    that keeps its payload. Both shapes are asserted together because the pair is a property of
    the GATE, not of whichever branch produced a given finding.
    """
    for findings in (rejoined_secret_veto, one_line_secret_veto,
                     rejoined_secret_veto_on_an_edited_page):
        for f in findings:
            assert f.gate == "secrets", f
            assert isinstance(f.values, tuple) and len(f.values) == 2, f
            line, rule = f.values
            assert rule, f
            assert line == "" or line.isdigit(), f


def test_a_knowledge_repo_linter_check_named_secret_is_not_read_as_a_gitleaks_hit():
    """`gate_contract` builds its `code` VERBATIM from the knowledge repo's linter JSON, so `code`
    alone is not this repo's namespace to promise. Selecting on it alone, a linter check named
    `secret` would reach the secrets branch carrying the default empty `values` and raise — and a
    `failed` row does not trigger the immediate purge that `rejected`/secret does. It must route as
    the ordinary contract refusal it is."""
    veto = [gates.Finding("contract", "secret", "wiki/notes/A.md: some linter complaint",
                          locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)  # on code alone: raises

    assert result.status == schema.FAILED, result.report
    assert result.report.get(schema.REASON_CODE_KEY) != schema.REASON_SECRET, result.report
    assert "gitleaks" not in result.report["summary"], result.report


def test_a_knowledge_repo_linter_check_named_pii_does_not_purge_the_submitters_material():
    """OLD BEHAVIOUR: the submitter's payload and hints were DESTROYED over a lint finding.

    The sibling test above pins the same hazard for `secret`; this is the branch two lines below
    it, which was still selecting on `code` alone. `gate_contract` builds its code VERBATIM from
    the knowledge repo's linter JSON, so a check named `pii` reached `report.rejected_pii` and set
    `reason_code = pii` — which is in `schema.WITHHELD_REASONS`, so `worker._finish` calls
    `retention.purge_secret_capture_immediately`. Irreversible, for a contract-lint complaint.

    It also read `locator.rsplit(":", 1)[-1]` as a line number (the page path) and echoed the
    linter's whole message as the "pattern label", inside a sentence promising the value is not
    repeated in this report.
    """
    veto = [gates.Finding("contract", "pii",
                          "wiki/notes/A.md: page mentions a person without an entity link",
                          locator="wiki/notes/A.md")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED, result.report
    assert result.report.get(schema.REASON_CODE_KEY) != schema.REASON_PII, result.report


def test_an_unrepairable_zone_finding_is_a_system_fault():
    """OLD BEHAVIOUR: `_refuse` blamed the SUBMITTER for work the agent could not have done.

    `gate_body_rewrite`'s findings are all `repairable=False`, and its docstring says why: on the
    fast lane a modified page came from `edits.apply_declared` or from nothing, so the message
    "describes work the agent did not do and cannot reach". Beside a declared injection category,
    `_refuse` routed it to `rejected_steering` — telling the submitter their material had tried to
    write outside the lane, and naming a colleague's page — for a fault this module's own docstring
    classifies as a system fault. The sibling router that already carried the `and f.repairable`
    clause is gone; this is the one that had to learn it.
    """
    veto = [gates.Finding("zone", "body-rewrite", "rewrote existing content in wiki/notes/V.md",
                          locator="wiki/notes/V.md", repairable=False)]

    result = processing._refuse(UNREPAIRABLE_ITEM, veto, STEERED_CATEGORY, agent_attempts=2)

    assert result.status == schema.FAILED, result.report
    assert schema.REASON_STEERING not in json.dumps(result.report)


def test_a_repairable_zone_finding_beside_a_category_still_reaches_the_submitter():
    """The benign twin for the clause above: a REPAIRABLE zone finding beside a declared category
    is exactly the case that branch exists for — the material really may have steered the agent —
    so it must still reach `rejected`/steering."""
    veto = [gates.Finding("zone", "outside-lane",
                          "wrote wiki/entities/Rogue.md, outside the fast lane's folders",
                          locator="wiki/entities/Rogue.md")]

    result = processing._refuse(UNREPAIRABLE_ITEM, veto, STEERED_CATEGORY, agent_attempts=2)

    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_STEERING
