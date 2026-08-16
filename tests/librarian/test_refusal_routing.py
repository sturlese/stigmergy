"""`processing._refuse` and `_refuse_meeting`: which terminal state a surviving veto earns, what it
tells the submitter, and why.

The routing that turns a set of findings into a terminal state reads gates and codes, and most of it
is proven end to end in `test_processing_pg.py`, over a real queue, a real git repo and the offline
double. That is the right place for a refusal the double can actually stage. Two kinds of routing
are left over, and this module is for both of them.

**The branch the double cannot reach.** `zone/type-not-creatable` is a defensive veto:
`gates._check_created_type` documents that both derived views of `page.PAGE_TYPES` currently agree,
so `ensure_creatable` cannot raise for a type `type_for_folder` just returned, and no capture — and
no double directive — can produce the finding today. The routing behind it is still worth writing
and worth testing, because the destination is *mechanically derivable* (the folder the page landed
in supplies `judged_type`) and because the alternative destination is the one the routing must
never pick: an agent that recognizes a governed type parks the capture and lands in `triage`, and
one that writes the page anyway used to be reported as the librarian breaking.

The future is simulated with ONE line — a governed type given a folder
(`FOLDER_BY_TYPE["meeting"]`) — which is exactly the table change that makes the guard live. Both
halves are driven by the real code under that patch: the gate helper produces the finding, and the
router derives the type back out of its locator.

**The sentence a person actually receives.** The secrets section at the bottom is the opposite case:
an ordinary, reachable refusal, where the routing decides not just the terminal state but WHERE it
tells a submitter their credential is. Those tests write real pages and shell out to the real
gitleaks binary (so this module skips, loudly in CI, without it — see `require_gitleaks`), because a
finding a test author typed out proves only that the router handles a shape someone imagined. Every
case there runs against BOTH routers: the defect they were written for was duplicated verbatim
across the two, and a fix to one site alone would otherwise have shipped green.
"""
import json
from types import SimpleNamespace

import pytest

from stigmergy.capture import schema
from stigmergy.librarian import gates, processing
from stigmergy.librarian import page as page_policy
from tests import adversarial_payloads as payloads

ITEM = {"id": 7, "attempts": 1, "submitted_by": "someone@acme.test"}
OUTCOME = SimpleNamespace(summary="it reads like a meeting, so it went to the meetings folder",
                          findings=[])
GOVERNED_PAGE = "wiki/meetings/Renewal Sync.md"


@pytest.fixture()
def governed_folder(monkeypatch):
    """A `meeting` type that has a folder but is still not creatable — the disagreement between
    `page.PAGE_TYPES`' two derived views that `zone/type-not-creatable` exists to catch.

    `_BY_NAME` is left alone on purpose: `classify_page_type("meeting")` therefore still answers
    "known, not creatable, meeting pages arrive with the meeting distiller", which is what makes
    this a simulation of the real future (a governed type that gains a folder) rather than of a
    typo.
    """
    monkeypatch.setitem(page_policy.FOLDER_BY_TYPE, "meeting", "wiki/meetings")


def _type_veto(governed_page: str = GOVERNED_PAGE) -> gates.Finding:
    """The finding, from the real gate helper rather than typed out here.

    `gate_zone` itself cannot be used: it filters on `ALLOWED_WRITE_PREFIXES`, a tuple computed at
    import from the same table, so under the patch above the page reads as `outside-lane` instead.
    The helper is where the type half lives and is what a page inside a known folder reaches.
    """
    ctx = gates.GateContext(worktree="", entries=[], added=[], material="",
                            outcome=SimpleNamespace(page_type="meeting"), registry=None)
    findings = gates._check_created_type(ctx, governed_page)
    assert [f.code for f in findings] == [gates.TYPE_NOT_CREATABLE]
    return findings[0]


def test_a_type_the_fast_lane_may_not_create_is_parked_with_the_type_its_folder_names(
        governed_folder):
    """Criterion 4's destination, reached from the veto side. The same capture parked cooperatively
    (`processing._triage`, `unsupported-type`) produces the same sentence — which is the point: an
    agent that recognizes the governed type and one that writes the page anyway must not end in two
    different places, with the worse one reached by trying harder."""
    result = processing._refuse(ITEM, [_type_veto()], OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE
    assert result.result_ref == ""
    assert "a meeting page" in result.report["summary"]           # the label, from the ONE table
    assert result.report["open_question"] == "where does a meeting page belong?"
    assert result.report["agent_rationale"] == OUTCOME.summary
    # It must not read as a system fault: nothing here asks the submitter to do anything.
    assert "could not finish" not in result.report["summary"]
    assert json.dumps(result.report).count(schema.FAILED) == 0


def test_the_judged_type_is_derived_from_the_folder_and_never_from_what_the_agent_declared(
        governed_folder):
    """Why this routing needed no measurement: nothing is judged. The folder the page landed in is
    what the agent DID, and `page.type_for_folder` is the same inverse lookup the gate used to
    refuse it — so an outcome claiming another type cannot move the destination."""
    lying = SimpleNamespace(summary="", findings=[], page_type="note")

    result = processing._refuse(ITEM, [_type_veto()], lying, agent_attempts=2)

    assert result.status == schema.TRIAGE
    assert "a meeting page" in result.report["summary"]
    assert "a note page" not in result.report["summary"]


def test_a_second_veto_beside_it_still_fails_rather_than_parking(governed_folder):
    """The same guard `_unanchorable` carries. A park says "this material is fine, it just belongs
    elsewhere"; if the librarian also wrote a page git treats as binary, that sentence would bury a
    real fault under a routine one and send the item to the wrong queue."""
    binary = gates.Finding("binary", "binary-page", "a NUL byte", locator=GOVERNED_PAGE)

    result = processing._refuse(ITEM, [_type_veto(), binary], OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


def test_a_folder_that_names_a_creatable_type_falls_back_to_a_system_fault():
    """Deliberately NOT patched: with the table as it ships, the veto's own locator names a folder
    the fast lane may create in — the gate and this router disagreeing, which is a defect rather
    than a governed type. `failed` is honest there, and it is what keeps the absurd sentence a
    silent disagreement would produce ("this reads like a note page… note needs a steward's
    review") from ever reaching a submitter."""
    veto = gates.Finding("zone", gates.TYPE_NOT_CREATABLE, "wiki/notes/A.md: …",
                         locator="wiki/notes/A.md")

    result = processing._refuse(ITEM, [veto], OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED
    assert processing._uncreatable_type([veto]) == ""


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


# ── the anchoring park, and when a co-occurring dead_links finding still lets it through ──────
# `wiki/SKILL.md` still tells the agent to carry a wikilink to the entity it declares, so the
# ORDINARY unresolved-anchor capture is an agent that writes `[[Acme Ventures Inc]]` AND declares
# `"entities": ["Acme Ventures Inc"]` in the same breath: one anchoring veto, one dead_links veto,
# the SAME name. Routing that combination to `failed` — the exact case the routing is supposed to
# park — was the regression; an UNRELATED dead link must still fall through to `failed`.
ANCHOR_PAGE = "wiki/notes/Acme Ventures Inc.md"


def _anchor_veto(*names: str) -> gates.Finding:
    """`values` carries every unresolved name VERBATIM (what `_unanchorable` matches a companion
    dead link against), while `locator` keeps its DISPLAY role — the first one, for a human or a
    prompt to read. Defaults to the single-name shape, which is what most cases here use."""
    names = names or ("Acme Ventures Inc",)
    return gates.Finding("anchoring", gates.ANCHORING_UNRESOLVED,
                         f'declared entity anchor {names!r} does not resolve in the entity '
                         f"registry read at the base commit",
                         locator=names[0], values=tuple(names))


def _dead_link_veto(target: str, path: str = ANCHOR_PAGE) -> gates.Finding:
    return gates.Finding("contract", gates.DEAD_LINKS_CHECK,
                         f"{path}: dead link: [[{target}]]", locator=path)


def test_case_1_unresolved_alone_parks_with_the_steward():
    result = processing._refuse(ITEM, [_anchor_veto()], OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE
    assert "Acme Ventures Inc" in result.report["summary"]
    assert json.dumps(result.report).count(schema.FAILED) == 0


def test_case_2_a_dead_link_naming_the_same_entity_still_parks():
    """The ordinary case: the agent wrote the wikilink it also declared, per SKILL.md."""
    veto = [_anchor_veto("Acme Ventures Inc"), _dead_link_veto("Acme Ventures Inc")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE
    assert "Acme Ventures Inc" in result.report["summary"]


def test_case_2b_the_name_match_is_case_insensitive():
    veto = [_anchor_veto("Acme Ventures Inc"), _dead_link_veto("acme ventures inc")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE


def test_case_3_a_dead_link_naming_something_else_falls_through_to_failed():
    """The conservative half: an UNRELATED dead link is a separate content defect, and admitting
    it into the park would tell a steward "this material is fine" about a page that may also carry
    a real, unrelated fault."""
    veto = [_anchor_veto("Acme Ventures Inc"), _dead_link_veto("Some Other Page")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


def test_case_4_a_binary_page_veto_beside_it_falls_through_to_failed():
    veto = [_anchor_veto(), gates.Finding("binary", "binary-page", "a NUL byte",
                                          locator=ANCHOR_PAGE)]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


def test_two_dead_links_only_park_when_every_one_of_them_matches():
    """Not just "at least one matches" — EVERY companion dead_links finding must name the same
    value, or the refusal falls through. One matching link beside one unrelated one must not park."""
    veto = [_anchor_veto("Acme Ventures Inc"), _dead_link_veto("Acme Ventures Inc"),
           _dead_link_veto("Some Other Page")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


# ── a SET test over `values` (verbatim), not a single-string `locator` ─────────────────────────
def test_two_unresolved_entities_with_two_matching_dead_links_both_park():
    """`entity:` is plural — 1-3 names is the expected shape. Two unregistered entities produce
    ONE anchoring veto (carrying both names in `values`) and TWO `dead_links` vetoes, which is how
    the regression this test guards re-enters: through cardinality."""
    veto = [_anchor_veto("Acme Ventures Inc", "Globex Corp"),
           _dead_link_veto("Acme Ventures Inc"), _dead_link_veto("Globex Corp")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE


def test_an_nfd_wikilink_still_matches_an_nfc_declared_name():
    """`[[Nestlé]]` written in NFD (the accent is a COMBINING acute, U+0301, after a bare `e`)
    against a declared `Nestlé` in NFC (the precomposed `é`, U+00E9) — the same name, different
    byte sequences. `page.path_key` already normalizes NFC for exactly this reason; `_unanchorable`
    must too."""
    import unicodedata
    nfc_name = unicodedata.normalize("NFC", "Nestlé")   # precomposed é, U+00E9
    nfd_target = unicodedata.normalize("NFD", nfc_name)  # bare e + combining acute, U+0301
    assert nfc_name != nfd_target                # genuinely different strings, byte for byte
    veto = [_anchor_veto(nfc_name), _dead_link_veto(nfd_target)]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE


def test_a_declared_name_longer_than_the_80_char_display_clamp_still_matches():
    """`gates.MAX_BRIEF_NAME_LEN` (80) clamps the DISPLAY locator with an ellipsis;
    `agent.MAX_IDENTIFIER_LEN` allows up to 400. A name in between used to never match its own
    un-clamped dead-link target."""
    long_name = "Extremely Long Registered-Sounding Entity Name That Keeps Going On And On " * 2
    assert len(long_name) > 80
    veto = [_anchor_veto(long_name), _dead_link_veto(long_name)]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE


def test_one_matching_and_one_unrelated_dead_link_among_two_declared_entities_still_fails():
    """The conservative direction still holds with plural anchors: a dead link that does not name
    ANY of the declared values is a separate content defect, and must not be swallowed into the
    park just because one OTHER dead link happened to match."""
    veto = [_anchor_veto("Acme Ventures Inc", "Globex Corp"),
           _dead_link_veto("Acme Ventures Inc"), _dead_link_veto("Some Other Page")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


def test_nothing_declared_at_all_beside_a_dead_link_falls_through_to_failed():
    """`values` is empty when NOTHING was declared (`kind: "entity"`, `entities: []`) — the display
    `locator` still falls back to the non-empty placeholder for the solo-park case, but with a
    companion dead-link finding present there is nothing real to match it against, so this must
    fall through to `failed` rather than let a literal `[[something unnamed]]` wikilink
    coincidentally match the placeholder string (the old bug this guards the opposite of)."""
    empty_anchor = gates.Finding("anchoring", gates.ANCHORING_UNRESOLVED,
                                 '"anchoring.entities" names no entity at all',
                                 locator="something unnamed", values=())
    veto = [empty_anchor, _dead_link_veto("something unnamed")]

    result = processing._refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED


def test_nothing_declared_at_all_with_no_companion_still_parks():
    """The solo case is unaffected by the `values` guard — a lone anchoring veto with nothing
    declared still reaches the steward park."""
    empty_anchor = gates.Finding("anchoring", gates.ANCHORING_UNRESOLVED,
                                 '"anchoring.entities" names no entity at all',
                                 locator="something unnamed", values=())

    result = processing._refuse(ITEM, [empty_anchor], OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE


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


# ── `processing._refuse_meeting`'s zone-plus-category branch must not route an UNREPAIRABLE zone
# finding to `rejected`/steering. `zone/meeting-edit-refused`'s own documented
# meaning is "no producer inside this flow" — a system fault, never something the material's
# injected text could have caused — and `zone/body-rewrite` (on the fast lane, always
# `repairable=False`) shares that class. A coincident declared category must not relabel either
# as the submitter's doing; both fall through to `failed_system` exactly like a zone veto with no
# category at all. ──────────────────────────────────────────────────────────────────────────────
MEETING_ITEM = {"id": 9, "attempts": 1}
STEERED_CATEGORY = SimpleNamespace(summary="", findings=[{"category": "declare-canonical"}])


def test_meeting_edit_refused_beside_a_declared_category_still_fails_as_a_system_fault():
    """The exploit this guards: a transcript with injection-looking text (so the agent declares a
    category) coincides with an anomalous `M` in the worktree. Before this fix the refusal landed
    as `rejected`/steering, naming the SUBMITTER's capture as the cause; the honest cause is a
    worker defect or worktree interference, which is what `failed_system` says."""
    veto = [gates.Finding("zone", "meeting-edit-refused",
                         "modified wiki/decisions/A.md: no edit mechanism exists here",
                         locator="wiki/decisions/A.md", repairable=False)]

    result = processing._refuse_meeting(MEETING_ITEM, veto, STEERED_CATEGORY, agent_attempts=1)

    assert result.status == schema.FAILED, (
        f"an unrepairable zone/meeting-edit-refused finding must route to failed_system even "
        f"beside a declared injection category — got {result.status!r}, "
        f"report={result.report!r}")
    assert schema.REASON_STEERING not in json.dumps(result.report)


def test_body_rewrite_beside_a_declared_category_still_fails_as_a_system_fault():
    """Same class, deliberately included (not scope creep): `zone/body-rewrite` diagnoses work
    `edits.apply_declared` did on the agent's behalf and is always `repairable=False` on the fast
    lane, so it is exactly as much a system fault as `meeting-edit-refused` is."""
    veto = [gates.Finding("zone", "body-rewrite",
                         "rewrote existing content in wiki/decisions/A.md",
                         locator="wiki/decisions/A.md", repairable=False)]

    result = processing._refuse_meeting(MEETING_ITEM, veto, STEERED_CATEGORY, agent_attempts=1)

    assert result.status == schema.FAILED, (
        f"an unrepairable zone/body-rewrite finding must route to failed_system even beside a "
        f"declared injection category — got {result.status!r}, report={result.report!r}")
    assert schema.REASON_STEERING not in json.dumps(result.report)


def test_a_repairable_zone_finding_beside_a_declared_category_still_routes_as_steering():
    """The negative space the branch above must not break: a REPAIRABLE zone finding beside a
    declared category
    is exactly the case this branch exists for — the agent's own material may really have steered
    it — so it must still reach `rejected`/steering, in `_refuse_meeting` exactly as in `_refuse`
    (see `test_a_traceable_steering_attempt_beside_the_type_veto_stays_content_actionable`)."""
    veto = [gates.Finding("zone", "outside-lane",
                         "wrote wiki/entities/Rogue.md, which is outside the fast lane's "
                         "folders", locator="wiki/entities/Rogue.md")]

    result = processing._refuse_meeting(MEETING_ITEM, veto, STEERED_CATEGORY, agent_attempts=1)

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
@pytest.mark.parametrize("refuse", [processing._refuse, processing._refuse_meeting],
                         ids=["fast-lane", "meeting"])
def test_a_secret_split_across_a_line_break_is_not_reported_as_a_line_number(
        refuse, veto_fixture, request):
    """OLD BEHAVIOUR: both routers recovered the line with `locator.rsplit(":", 1)[-1]`. For the
    rejoined shape the locator IS the page path and holds no line, so that expression returned the
    path — and the submitter was told the credential sat "near line wiki/notes/Renewal Terms.md".
    `rejected_secret`'s "split across a line break" branch, written for precisely this finding,
    was unreachable from here. This reason code purges the person's material immediately, so the
    locator in this sentence is the only thing they have left to act on.
    """
    result = refuse(ITEM, request.getfixturevalue(veto_fixture), OUTCOME, agent_attempts=2)

    summary = result.report["summary"]
    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_SECRET
    assert "split across a line break" in summary, summary
    assert "near line" not in summary, summary
    assert "wiki/notes" not in summary, summary
    assert "(rule: github-pat)" in summary, summary


@pytest.mark.parametrize("refuse", [processing._refuse, processing._refuse_meeting],
                         ids=["fast-lane", "meeting"])
def test_an_ordinary_secret_still_names_the_line_its_author_wrote(refuse, one_line_secret_veto):
    """The benign twin. The fix must not flatten every secret refusal into "somewhere in the page":
    a hit that does sit on one line keeps that line, because the number is most of the message's
    value to whoever has to go and remove the credential."""
    result = refuse(ITEM, one_line_secret_veto, OUTCOME, agent_attempts=2)

    summary = result.report["summary"]
    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_SECRET
    assert "near line 1 of the drafted page" in summary, summary
    assert "split across a line break" not in summary, summary
    assert "(rule: github-pat)" in summary, summary


def test_every_secrets_finding_carries_the_pair_the_routing_unpacks(
        rejoined_secret_veto, one_line_secret_veto, rejoined_secret_veto_on_an_edited_page):
    """The invariant both routers now depend on, pinned rather than left to inspection.

    They read `line, rule = secret.values`, so a secrets finding that ever reached them without a
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

    for refuse in (processing._refuse, processing._refuse_meeting):
        result = refuse(ITEM, veto, OUTCOME, agent_attempts=2)   # selecting on code alone: raises

        assert result.status == schema.FAILED, result.report
        assert result.report.get(schema.REASON_CODE_KEY) != schema.REASON_SECRET, result.report
        assert "gitleaks" not in result.report["summary"], result.report


@pytest.mark.parametrize("refuse", [processing._refuse, processing._refuse_meeting],
                         ids=["fast-lane", "meeting"])
def test_a_knowledge_repo_linter_check_named_pii_does_not_purge_the_submitters_material(refuse):
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

    result = refuse(ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.FAILED, result.report
    assert result.report.get(schema.REASON_CODE_KEY) != schema.REASON_PII, result.report


@pytest.mark.parametrize("refuse", [processing._refuse, processing._refuse_meeting],
                         ids=["fast-lane", "meeting"])
def test_an_unrepairable_zone_finding_is_a_system_fault_in_both_routers(refuse):
    """OLD BEHAVIOUR: `_refuse` blamed the SUBMITTER for work the agent could not have done.

    `gate_body_rewrite`'s findings are all `repairable=False`, and its docstring says why: on the
    fast lane a modified page came from `edits.apply_declared` or from nothing, so the message
    "describes work the agent did not do and cannot reach". Beside a declared injection category,
    `_refuse` routed it to `rejected_steering` — telling the submitter their material had tried to
    write outside the lane, and naming a colleague's page — for a fault this module's own docstring
    classifies as a system fault. `_refuse_meeting` already carried the `and f.repairable` clause.
    """
    veto = [gates.Finding("zone", "body-rewrite", "rewrote existing content in wiki/notes/V.md",
                          locator="wiki/notes/V.md", repairable=False)]

    result = refuse(MEETING_ITEM, veto, STEERED_CATEGORY, agent_attempts=2)

    assert result.status == schema.FAILED, result.report
    assert schema.REASON_STEERING not in json.dumps(result.report)


@pytest.mark.parametrize("refuse", [processing._refuse, processing._refuse_meeting],
                         ids=["fast-lane", "meeting"])
def test_a_repairable_zone_finding_beside_a_category_still_reaches_the_submitter(refuse):
    """The benign twin for the clause above, in BOTH routers: a REPAIRABLE zone finding beside a
    declared category is exactly the case that branch exists for — the material really may have
    steered the agent — so it must still reach `rejected`/steering."""
    veto = [gates.Finding("zone", "outside-lane",
                          "wrote wiki/entities/Rogue.md, outside the fast lane's folders",
                          locator="wiki/entities/Rogue.md")]

    result = refuse(MEETING_ITEM, veto, STEERED_CATEGORY, agent_attempts=2)

    assert result.status == schema.REJECTED
    assert result.report[schema.REASON_CODE_KEY] == schema.REASON_STEERING


# ── the flow flag on the SECOND call site of the park builder ─────────────────────────────────
# `report.triage_entity(meeting=...)` is called from two places: `_ask_or_park` (both flows, via
# its own `meeting` parameter — pinned in `test_ordinary_multi_entity_park_unit.py`) and here, from
# `_refuse_meeting`'s anchoring branch, which is meeting-only and passes `meeting=True`
# literally. That literal is the kind of argument a refactor drops without any other test noticing:
# every assertion this file already makes is about which STATE a veto earns, and none reads the
# sentence a submitter is handed.
def test_a_meeting_refused_on_two_unresolved_anchors_parks_it_as_a_MEETING():
    """A transcript whose page set could not anchor two of its entities parks the whole thing, and
    the submitter is told what that costs HIM — a meeting, not "a capture". A page set is what is
    stuck, and the ordinary noun would understate it."""
    veto = [_anchor_veto("Jack", "Acme Capital")]

    result = processing._refuse_meeting(MEETING_ITEM, veto, OUTCOME, agent_attempts=2)

    assert result.status == schema.TRIAGE
    assert "place this meeting where it actually belongs" in result.report["summary"]
    assert result.report[schema.SITUATION_NAMES_KEY] == ["Jack", "Acme Capital"]


def test_a_meeting_refused_on_ONE_unresolved_anchor_writes_the_same_one_shape():
    """INVERTED by the plural collapse. This used to assert that one name went through a SINGULAR
    builder writing `SITUATION_NAME_KEY` with no plural key — the branch had a `len(names) > 1`
    fork. There is no fork: one builder, one written key, a list of one. The singular key is
    retired as an output, so its ABSENCE is half of what is asserted here.

    Kept as its own case rather than folded into the two-name one above: the single-name park is
    the common shape, and the count still chooses the SENTENCE, which is what the last assertion
    reads."""
    result = processing._refuse_meeting(MEETING_ITEM, [_anchor_veto("Jack")], OUTCOME,
                                        agent_attempts=2)

    assert result.status == schema.TRIAGE
    assert result.report[schema.SITUATION_NAMES_KEY] == ["Jack"]
    assert schema.SITUATION_NAME_KEY not in result.report
    assert 'seems to be about "Jack"' in result.report["summary"]


# ── the THIRD park road: `_refuse`'s own unanchorable branch, ordinary flow ────────────────────
# `_refuse` parks an unanchorable ordinary capture through the same builder, and after the collapse
# it does so with a LIST (`names=[unanchorable.locator]`). Nothing pinned its written shape before:
# `test_case_1_unresolved_alone_parks_with_the_steward` reads the SUMMARY only, so this road could
# have kept writing the retired singular key — or stopped writing a name key at all — with every
# test in this file green.
def test_an_unanchorable_ordinary_capture_parks_through_the_one_written_shape():
    result = processing._refuse(ITEM, [_anchor_veto("Acme Ventures Inc")], OUTCOME,
                                agent_attempts=2)

    assert result.status == schema.TRIAGE
    assert result.report[schema.SITUATION_NAMES_KEY] == ["Acme Ventures Inc"]
    assert schema.SITUATION_NAME_KEY not in result.report
    # ORDINARY, so the noun is the ordinary one — `_refuse` passes no `meeting` flag and the
    # one-name sentence has no slot for one anyway.
    assert "meeting" not in result.report["summary"]


def test_characterization_an_ordinary_refusal_parks_on_the_ANCHOR_VETOS_FIRST_NAME_ONLY():
    """CHARACTERIZATION, and a gap named rather than hidden. `_anchor_veto` carries every
    unresolved name in `values`; `_refuse_meeting` collects them all, but `_refuse` still parks on
    `unanchorable.locator` — the FIRST one. That predates the plural collapse (the old singular
    builder could hold nothing else) and the collapse did not close it: the builder now takes a
    list and this call site still hands it one element.

    Recorded here so the asymmetry between the two refusal roads is a decision somebody takes,
    not something a reader discovers from a steward's queue. If the intended behaviour is that an
    ordinary refusal names every unresolved anchor the way the meeting one does, this assertion is
    the line that changes — and it is a `src/` change, not a test one."""
    result = processing._refuse(ITEM, [_anchor_veto("Jack", "Acme Capital")], OUTCOME,
                                agent_attempts=2)

    assert result.report[schema.SITUATION_NAMES_KEY] == ["Jack"]
    assert "Acme Capital" not in result.report["summary"]
