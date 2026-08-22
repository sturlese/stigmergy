"""`processing.process_item` end to end, over a real Postgres queue, a real git repo + bare
remote, and the offline double — the filing engine's whole contract.

Every test claims and finishes the item through `worker.process_next` (claim -> process ->
finish), the same wiring production uses, rather than calling `processing.process_item` in
isolation: `queue.finish` and the row it leaves are as much the contract as the `Result` object,
so the assertions read rows, not only return values. For `filed` outcomes the assertion reads the
FILED PAGE back from the repo's object database with `support.read_filed_page`, never what the
agent merely drafted — a forged field is inert only if it is inert on the committed bytes, and
that distinction is the whole reason the forged-frontmatter cases exist.

Payload choices are deliberate and verified empirically against the installed `gitleaks` binary
and the PII regexes before being used here (see the module-level comments beside each), so a
"the scanner fired" assertion is never accidental.
"""
import json
import os
import pathlib
import unicodedata

import pytest

from stigmergy.capture import queue, schema
from stigmergy.librarian import double as double_module
from stigmergy.librarian import page as page_policy
from stigmergy.librarian import processing, worker
from stigmergy.librarian import report as report_module
from tests import adversarial_payloads as payloads
from tests.librarian import support

ACME_MATERIAL = "A short note about how the Acme Corp renewal is going."

# The page name the double derives from `ACME_MATERIAL`. Computed, never retyped: it is the link a
# declared edit has to resolve to, and a hardcoded copy would silently become a `dead-link` test the
# moment the double's title derivation changed (which it just did, to stop destroying accents).
ACME_TITLE = double_module.DoubleAgent._title(ACME_MATERIAL)
ACME_PAGE = f"wiki/notes/{ACME_TITLE}.md"

# The adversarial payloads live in ONE place (`tests/adversarial_payloads.py`), shared with
# `test_adversarial.py` and with the docker e2e driver, and each is verified there against the tool
# it is meant to trip. Only the BENIGN twins are needed in this module — the attack halves live in
# the adversarial suite (see the pointers at each section below).
LUHN_INVALID_16_DIGITS = payloads.LUHN_INVALID_16_DIGITS


def _file(conn, deps, material, **kw):
    support.submit(conn, deps, material, **kw)
    return worker.process_next(conn, deps)


def _row(conn, submission_id):
    return queue.get_submission_trace(conn, submission_id)


# ── a queued capture becomes a committed page ───────────────────────────────────────────────────
def test_an_ordinary_capture_files_within_one_worker_cycle(rig, clean_queue):
    env, deps = rig
    before = support.branch_sha(env.bare)

    item, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FILED
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert page_path.startswith("wiki/notes/")
    after = support.branch_sha(env.bare)
    assert after == sha != before

    row = _row(clean_queue, item["id"])
    assert row["status"] == schema.FILED
    assert row["result_ref"] == result.result_ref
    assert row["report"]["status"] == schema.FILED


# ── nothing is filed ownerless — both halves ────────────────────────────────────────────────────
def test_a_capture_about_a_registered_entity_files_anchored_to_it(rig, clean_queue):
    _, deps = rig
    _, result = _file(clean_queue, deps, ACME_MATERIAL)
    assert result.status == schema.FILED
    assert "Acme Corp" in result.report["anchored_to"]


def test_a_filed_page_carries_the_same_resolved_id_the_report_names(rig, clean_queue):
    """Same value, two places, one source: the committed page's `entity:` frontmatter and the
    submission report's `anchored_to` phrase both name the registry id `deps.registry` resolved,
    spelled the same way."""
    env, deps = rig
    _, result = _file(clean_queue, deps, ACME_MATERIAL)
    assert result.status == schema.FILED

    page_path, sha = result.result_ref.rsplit("@", 1)
    text = support.read_filed_page(env.bare, sha, page_path)
    assert 'entity: ["acme-corp"]' in text
    assert result.report["anchored_to"] == "Acme Corp (`acme-corp`)"


def test_a_company_wide_capture_files_with_entity_empty_on_the_page_and_the_reason_only_in_the_report(
        rig, clean_queue):
    """A page whose anchoring outcome is company-wide is filed with `entity: []`, and the written
    reason is in the REPORT, never on the page."""
    env, deps = rig
    _, result = _file(clean_queue, deps, f"DOUBLE:company\n{ACME_MATERIAL}")
    assert result.status == schema.FILED
    reason = "a practice that applies across the whole company, not to one client or product"
    assert "company-wide scope" in result.report["anchored_to"]
    assert reason in result.report["anchored_to"]

    page_path, sha = result.result_ref.rsplit("@", 1)
    text = support.read_filed_page(env.bare, sha, page_path)
    assert "entity: []" in text
    assert reason not in text          # the reason justifies a FILING decision, never the page's


def test_a_capture_about_an_unregistered_entity_introduces_it_and_files_anchored_to_the_newborn(
        rig, clean_queue):
    """OLD BEHAVIOUR: a capture about a name the registry did not know was PARKED on a question to
    its submitter (`needs_input`), delivered to a tool result nobody polls; two of five captures on
    staging were lost to the button beside it. Then it filed with the identity waiting on a steward
    (`approved_by: ""`, `proposed: true`). ADR 044: the capture IS the approval — code creates the
    page with `approved_by` naming the SUBMITTER and regenerates the registry in the SAME commit as
    the note, the note lands anchored to the newborn id, and nothing waits on anybody."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    item, result = _file(clean_queue, deps, "DOUBLE:propose=Globex Corp\n" + ACME_MATERIAL)

    assert result.status == schema.FILED, result.report.get("summary")
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert sha != before
    changed = support.changed_paths(env.bare, sha)
    assert page_path in changed
    assert "wiki/entities/Globex Corp.md" in changed and "ops/entity-registry.json" in changed
    note = support.read_filed_page(env.bare, sha, page_path)
    assert 'entity: ["globex-corp"]' in note
    entity_page = support.read_filed_page(env.bare, sha, "wiki/entities/Globex Corp.md")
    assert f'approved_by: "{support.DEFAULT_SUBMITTER}"' in entity_page
    assert 'entity: ["globex-corp"]' in entity_page
    assert "proposed by the offline double" in entity_page     # every section filled, not a stub
    registry = json.loads(support.read_filed_page(env.bare, sha, "ops/entity-registry.json"))
    assert registry["entities"]["globex-corp"]["approved_by"] == support.DEFAULT_SUBMITTER
    assert "proposed" not in registry["entities"]["globex-corp"]
    # the submitter reads both halves
    assert result.report["anchored_to"] == "Globex Corp (`globex-corp`)"
    assert result.report["entities_born"] == [
        {"id": "globex-corp", "name": "Globex Corp", "type": "organization",
         "confirmed_by": support.DEFAULT_SUBMITTER}]
    assert "It introduces 1 new entity: Globex Corp (`globex-corp`)" in result.report["summary"]
    assert "the identity is confirmed by you" in result.report["summary"]
    body = support.commit_message_body(env.bare, sha)
    assert "Introduces 1 new entity page(s) — Globex Corp" in body
    assert "born confirmed by the submitter" in body

    row = _row(clean_queue, item["id"])
    assert row["status"] == schema.FILED


def test_several_unregistered_names_are_born_together_in_one_commit(rig, clean_queue):
    """Issue #32's shape, on the identity road: a capture naming TWO new things creates both —
    each its own page, each its own registry entry — in the one commit, and the report names both.
    A person and an organization, so the type travels too."""
    env, deps = rig
    _, result = _file(clean_queue, deps,
                      "DOUBLE:propose=Jack Reeve|person,Acme Capital\n" + ACME_MATERIAL)

    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.bare, sha)
    assert "wiki/entities/Jack Reeve.md" in changed and "wiki/entities/Acme Capital.md" in changed
    registry = json.loads(support.read_filed_page(env.bare, sha, "ops/entity-registry.json"))
    assert registry["entities"]["jack-reeve"]["type"] == "person"
    assert registry["entities"]["acme-capital"]["type"] == "organization"
    # one capture, one approver, however many identities it introduced
    assert {registry["entities"][cid]["approved_by"] for cid in ("jack-reeve", "acme-capital")} == {
        support.DEFAULT_SUBMITTER}
    assert [e["id"] for e in result.report["entities_born"]] == ["jack-reeve", "acme-capital"]
    assert "It introduces 2 new entities" in result.report["summary"]


def test_a_spelling_the_material_uses_for_a_registered_entity_is_added_to_its_aliases(
        rig, clean_queue):
    """OLD BEHAVIOUR: the spelling was appended to a second frontmatter list, `proposed_aliases:`,
    for a steward to confirm or decline. ADR 044: a spelling the material uses IS one of the
    entity's names, so it goes straight onto `aliases:` and is regenerated into the registry — the
    next capture using it resolves without asking anybody."""
    env, deps = rig
    _, result = _file(clean_queue, deps,
                      "DOUBLE:alias=Acme Corporation\nAcme Corporation renewed the contract.")

    assert result.status == schema.FILED, result.report.get("summary")
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert 'entity: ["acme-corp"]' in support.read_filed_page(env.bare, sha, page_path)
    entity_page = support.read_filed_page(env.bare, sha, "wiki/entities/Acme Corp.md")
    assert 'aliases: ["Acme", "Acme Corporation"]' in entity_page
    assert "proposed_aliases" not in entity_page
    registry = json.loads(support.read_filed_page(env.bare, sha, "ops/entity-registry.json"))
    assert registry["entities"]["acme-corp"]["aliases"] == ["Acme", "Acme Corporation"]
    assert result.report["aliases_added"] == [{"entity": "acme-corp", "alias": "Acme Corporation"}]
    assert ('It teaches the registry 1 new spelling: "Acme Corporation" for `acme-corp`'
            in result.report["summary"])


def test_a_proposal_that_collides_with_a_registered_spelling_is_refused_and_the_retry_anchors_there(
        rig, clean_queue):
    """The identity gate's main refusal, through the whole retry loop: pass one declares the
    registered entity under a legal form, the brief names the id to anchor to instead, pass two
    anchors there. One commit, no twin entity, the registry untouched."""
    env, deps = rig
    _, result = _file(clean_queue, deps, "DOUBLE:propose-collides\n" + ACME_MATERIAL)

    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.bare, sha)
    assert not any(path.startswith("wiki/entities/") for path in changed), changed
    assert "ops/entity-registry.json" not in changed
    assert result.report["anchored_to"] == "Acme Corp (`acme-corp`)"
    assert result.report["entities_born"] == []


def test_a_declared_name_the_material_never_uses_is_the_librarians_fault(rig, clean_queue):
    """An entity the capture never mentions is an invention, refused on every pass, and nothing
    lands — `failed`, naming the identity stage, with the submitter told their material is fine."""
    env, deps = rig
    before = support.branch_sha(env.bare)
    _, result = _file(clean_queue, deps, "DOUBLE:propose-unnamed\n" + ACME_MATERIAL)

    assert result.status == schema.FAILED, result.report.get("summary")
    assert "identity" in result.report["summary"] and "not your capture" in result.report["summary"]
    assert support.branch_sha(env.bare) == before


class _UnresolvableAnchorAgent:
    """Wraps the double and rewrites its DECLARED anchoring outcome to a name the registry does
    not know, while leaving the page itself untouched — the attack `gate_anchoring`'s `unresolved`
    refusal exists to catch: the gate checks the declared `anchoring.entities` list, never the
    page's body, so this wrapper mutates exactly that and nothing else. (It used to rewrite a
    wikilink on the page as well. That is vestigial now the gate reads no links.)

    The birth test above proves the COOPERATIVE half of "nothing is filed ownerless": an agent
    that correctly recognizes a new name introduces the entity. It proves nothing about what happens
    if the agent (or a document that talked it into lying) instead CLAIMS an anchor it does not
    have —
    the double's own anchor is always real (`DoubleAgent._registry_entity` reads the actual
    registry), so that path is otherwise unreachable through anything the double can be driven to
    do. This is the real refusal: a page that could otherwise have filed ownerless is stopped by
    the gate, not by the double's good behavior.
    """

    def __init__(self, inner):
        self.inner = inner
        # The declared port member, copied from what this wraps (ADR 033). Plain attribute
        # access with NO default: `processing._one_pass` refuses an agent that carries no
        # `structured_ordinary` rather than defaulting it, so a wrapper that swallowed the
        # declaration would silently change which shape of the ordinary flow runs behind it.
        # Reading it here means a wrapper around a non-conforming backend fails at
        # CONSTRUCTION, in the test that built it, instead of one queue delivery at a time.
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered

    def run(self, **kwargs):
        import dataclasses
        run = self.inner.run(**kwargs)
        if run.outcome is not None and run.outcome.decision == "file":
            run.outcome = dataclasses.replace(
                run.outcome,
                anchoring={"kind": "entity", "reason": "", "entities": ["Ghost Company Inc"]},
                links_created=("Ghost Company Inc",))
        return run


def test_a_claimed_entity_anchor_that_does_not_resolve_is_failed_never_filed(rig, clean_queue):
    """The second half of "nothing is filed ownerless", proven as a REAL refusal rather than a unit
    assertion about a helper (`gates.gate_anchoring` is never imported here): the gate is what
    stands between an unearned anchor claim and an ownerless page reaching `wiki/`, and it must
    refuse over two real agent attempts exactly like any other veto — never file with a
    company-wide fallback, never file the claimed-but-unresolved name as if it were real.

    OLD BEHAVIOUR: the destination was `triage`, a park on a steward with the name as its open
    question. There is no park: the brief offers the agent a third outcome that FILES — propose the
    entity — so an anchor still unresolved after the corrective pass is the librarian's own fault,
    and `failed` is the honest word for it. The submitter is told their material is fine.
    """
    env, base_deps = rig
    before = support.branch_sha(env.bare)
    import dataclasses
    deps = dataclasses.replace(base_deps, agent=_UnresolvableAnchorAgent(base_deps.agent))

    item, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FAILED
    assert result.result_ref == ""
    assert "not your capture" in result.report["summary"]
    assert support.branch_sha(env.bare) == before        # no commit, no page, at all

    row = _row(clean_queue, item["id"])
    assert row["status"] == schema.FAILED
    assert row["result_ref"] == ""


class _UnresolvableAndBinaryAgent(_UnresolvableAnchorAgent):
    """The same unearned anchor claim, plus a NUL byte in the page — two vetoes of DIFFERENT
    classes surviving the same refusal."""

    def run(self, **kwargs):
        run = super().run(**kwargs)
        if run.outcome is not None and run.outcome.decision == "file":
            full = os.path.join(kwargs["worktree"], run.outcome.page_path)
            with open(full, "a", encoding="utf-8") as f:
                f.write("\n\x00\n")
        return run


class _UnresolvableAnchorWithMatchingDeadLinkAgent(_UnresolvableAnchorAgent):
    """The ordinary case the librarian's own brief still instructs — the agent writes a wikilink to
    the entity it also declares, `[[Ghost Company Inc]]`, and the anchor does not resolve —
    reconstructed end to end through the REAL linter and the REAL gates, not a hand-built
    `Finding`. `_UnresolvableAnchorAgent`'s own docstring records why it no longer plants a
    wikilink itself; this is the positive case for the admission rule that replaced that, now that
    `_unanchorable` distinguishes "the same name" from "an unrelated name" instead of excluding
    every co-occurring dead link outright.
    """

    def run(self, **kwargs):
        run = super().run(**kwargs)
        if run.outcome is not None and run.outcome.decision == "file":
            full = os.path.join(kwargs["worktree"], run.outcome.page_path)
            with open(full, "a", encoding="utf-8") as f:
                f.write("\nMaterial about [[Ghost Company Inc]] as well.\n")
        return run


def test_an_unresolved_anchor_with_a_dead_link_naming_the_same_entity_still_fails(
        rig, clean_queue):
    """The shape the old park admitted specially — the agent wrote `[[Ghost Company Inc]]` AND
    declared it — is no special case: two vetoes, one `failed`, nothing committed."""
    env, base_deps = rig
    before = support.branch_sha(env.bare)
    import dataclasses
    deps = dataclasses.replace(
        base_deps, agent=_UnresolvableAnchorWithMatchingDeadLinkAgent(base_deps.agent))

    item, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FAILED
    assert result.result_ref == ""
    assert support.branch_sha(env.bare) == before
    assert _row(clean_queue, item["id"])["status"] == schema.FAILED


def test_an_anchoring_veto_beside_a_real_fault_still_fails(rig, clean_queue):
    """A page git treats as binary beside the unresolved anchor — a fault that turned four gates
    off at once before `gate_binary_page` existed. `failed`, like the anchoring veto alone, and
    pinned so that a future routing of anchoring vetoes cannot bury a second class of fault."""
    env, base_deps = rig
    before = support.branch_sha(env.bare)
    import dataclasses
    deps = dataclasses.replace(base_deps, agent=_UnresolvableAndBinaryAgent(base_deps.agent))

    item, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FAILED
    assert support.branch_sha(env.bare) == before
    assert _row(clean_queue, item["id"])["status"] == schema.FAILED


def test_both_roads_from_an_unknown_name_record_the_same_injection_finding(rig, clean_queue):
    """Two paths, and they must not disagree about what happened even though their DESTINATIONS
    differ: a capture whose material tried to steer the librarian records a finding when it FILES
    (introducing the unknown entity) and when it FAILS (claiming an anchor the gate refuses).

    Asserted on the REPORT rather than on `Result.findings`, because the report is what
    `queue.finish` persists and `brain_submissions` returns: a finding recorded only on the object
    the worker drops is not recorded at all.
    """
    import dataclasses
    _, base_deps = rig
    steered = f"{ACME_MATERIAL} {payloads.STEER_REVEAL_CREDENTIALS}"

    _, born = _file(clean_queue, base_deps, f"DOUBLE:propose=Globex Corp\n{steered}")
    deps = dataclasses.replace(base_deps, agent=_UnresolvableAnchorAgent(base_deps.agent))
    # Different opening words: the first road FILED its note, and the double titles a page from
    # the material's first words, so the same material again would collide with that page.
    _, vetoed = _file(clean_queue, deps, f"A second capture, same steering attempt. {steered}")

    for label, result, expected_status in (("born", born, schema.FILED),
                                           ("veto", vetoed, schema.FAILED)):
        assert result.status == expected_status, (label, result.report.get("summary"))
        assert any("reveal-credentials" in f for f in result.report["findings"]), label
        assert payloads.STEER_REVEAL_CREDENTIALS not in json.dumps(result.report), label


# ── only the whitelisted types can be created ───────────────────────────────────────────────────
@pytest.mark.parametrize("governed_type", ["entity", "meeting", "policy", "source"])
def test_a_governed_type_written_into_the_lane_is_the_librarians_fault_never_filed(
        rig, clean_queue, governed_type):
    """OLD BEHAVIOUR: the double declared the type cooperatively (`triage-type`) and the capture
    parked on a steward. The agent is told the three creatable types and how to INTRODUCE an entity
    for anything else, so a page of a governed type in the lane is the librarian's own fault:
    refused on both passes, `failed`, nothing committed."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    _, result = _file(clean_queue, deps, f"DOUBLE:type={governed_type}\n{ACME_MATERIAL}")

    assert result.status == schema.FAILED, result.report.get("summary")
    assert "not your capture" in result.report["summary"]
    assert support.branch_sha(env.bare) == before


# ── a capture cannot declare itself canonical, and forged frontmatter is inert ──────────────────
# **These live in `test_adversarial.py`** as permanent cat. 7 cases: the self-declared
# `status: canonical` case and the five-forged-fields case, both asserted on the FILED PAGE plus the
# commit trailer. Collected with `pytest -k adversarial_cat7`. Their benign twin — an ordinary
# `id`/`status` frontmatter that must NOT be accused of forgery — is in the benign-twin table below.


# ── every defense has its benign twin ───────────────────────────────────────────────────────────
BENIGN_CASES = [
    pytest.param("plain_prose", ACME_MATERIAL, id="plain_prose"),
    pytest.param("email_address",
                f"{ACME_MATERIAL} Contact jane.doe@example.com about the rollout.",
                id="email_address"),
    pytest.param("person_name", f"{ACME_MATERIAL} John Smith joined the call to review it.",
                id="person_name"),
    pytest.param("figure_from_its_own_source",
                f"{ACME_MATERIAL} Q3 revenue reached 512000 usd per the deck.",
                id="figure_from_its_own_source"),
    pytest.param("luhn_invalid_16_digits",
                f"{ACME_MATERIAL} Order reference {LUHN_INVALID_16_DIGITS} was cancelled.",
                id="luhn_invalid_16_digits"),
    pytest.param("ordinary_id_status_frontmatter",
                f"---\nid: my-existing-page\nstatus: developing\n---\n\n{ACME_MATERIAL}",
                id="ordinary_id_status_frontmatter"),
]


# Substrings a DEFENSE finding would carry. A freshly filed, deliberately isolated fixture page
# is expected to trip the contract linter's harmless "orphans" warning (nothing else in the same
# commit links to it yet) — that is a linter NOTE unrelated to any of the gates below, so the
# benign-twin assertion is "no defense fired", not "the report is empty".
_DEFENSE_SIGNALS = ("pii", "secret", "card", "iban", "dni", "private key", "canonical", "forg",
                   "injection", "instruct", "steer", "trace", "untraced")


@pytest.mark.parametrize("label,material", BENIGN_CASES)
def test_benign_material_files_with_no_veto_and_no_finding(rig, clean_queue, label, material):
    _, deps = rig
    _, result = _file(clean_queue, deps, material)
    assert result.status == schema.FILED, f"{label}: {result.report.get('summary')}"
    lowered = [f.lower() for f in result.findings]
    fired = [f for f in lowered if any(signal in f for signal in _DEFENSE_SIGNALS)]
    assert fired == [], f"{label}: a defense fired on benign material: {fired}"


# ── secrets and PII bounce the whole capture, never redacted ────────────────────────────────────
# **These live in `test_adversarial.py`** as permanent cat. 5 cases: the gitleaks case and the four
# PII patterns, each asserting no commit and that the value never reaches the report. Collected
# with `pytest -k adversarial_cat5`. Their benign twins — an email address, a person's name, a
# Luhn-INVALID 16-digit number — are in the benign-twin table above.


# ── the corrective retry runs exactly once, or not at all ───────────────────────────────────────
class _CountingAgent:
    """Wraps a real agent and records what each pass was handed — the seam that proves the
    corrective retry ran exactly once, or did not run at all (`gates.unrepairable`), and WHAT the
    agent was told between passes rather than merely that the outcome matched.

    `briefs` is one entry per pass, so the count and the text come from the same recording: a test
    asserting "one pass, and no brief was composed" would otherwise need two wrappers that could
    disagree about how many passes there were.
    """

    def __init__(self, inner):
        self.inner = inner
        # The declared port member, copied from what this wraps (ADR 033). Plain attribute
        # access with NO default: `processing._one_pass` refuses an agent that carries no
        # `structured_ordinary` rather than defaulting it, so a wrapper that swallowed the
        # declaration would silently change which shape of the ordinary flow runs behind it.
        # Reading it here means a wrapper around a non-conforming backend fails at
        # CONSTRUCTION, in the test that built it, instead of one queue delivery at a time.
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.briefs = []            # the `corrective` text each pass was given, in order

    @property
    def calls(self) -> int:
        return len(self.briefs)

    def run(self, **kwargs):
        self.briefs.append(kwargs.get("corrective", ""))
        return self.inner.run(**kwargs)




# ── the corrective retry, for the OUTCOME's shape ───────────────────────────────────────────────
# A malformed outcome used to raise `AgentError` out of the retry loop, so the agent was never told
# and both attempts were spent identically — which is what happened on the librarian's first real
# walk. A shape the boundary refuses now takes exactly the same road a gate veto takes.
def test_a_bad_outcome_shape_on_the_first_pass_is_corrected_on_the_retry_and_FILES(rig,
                                                                                  clean_queue):
    """The one the fix exists for: bad shape on pass 1, good on pass 2, page filed."""
    import dataclasses
    env, base_deps = rig
    counting = _CountingAgent(base_deps.agent)
    deps = dataclasses.replace(base_deps, agent=counting)
    before = support.branch_sha(env.bare)

    _, result = _file(clean_queue, deps, f"DOUBLE:bad-shape-once\n{ACME_MATERIAL}")

    assert result.status == schema.FILED
    assert counting.calls == 2                              # first pass + one corrective retry
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert support.branch_sha(env.bare) == sha != before
    assert support.read_filed_page(env.repo, sha, page_path).startswith("---")


def test_a_bad_outcome_shape_on_both_passes_fails_naming_the_outcome_and_not_a_gate(rig,
                                                                                   clean_queue):
    """When the retry does not fix it the item must still fail as it always did — but the report now
    names the stage that actually refused (`outcome`) and the honest agent-attempt count."""
    import dataclasses
    env, base_deps = rig
    counting = _CountingAgent(base_deps.agent)
    deps = dataclasses.replace(base_deps, agent=counting)
    before = support.branch_sha(env.bare)

    item, result = _file(clean_queue, deps, f"DOUBLE:bad-shape\n{ACME_MATERIAL}")

    assert result.status == schema.FAILED
    assert counting.calls == 2
    assert support.branch_sha(env.bare) == before           # nothing committed
    assert result.report["stage"] == "outcome"
    assert result.report["agent_attempts"] == 2             # the retry RAN, and is reported
    assert "decision" in result.report["summary"]           # what was wrong, in the sentence

    row = _row(clean_queue, item["id"])
    assert row["status"] == schema.FAILED and row["result_ref"] == ""


def test_the_corrective_brief_for_a_shape_refusal_names_the_outcome_file(rig, clean_queue):
    """Assert that the MECHANISM fired: the retry above could file for a second reason, and an
    outcome assertion with more than one possible cause proves nothing about the mechanism. This
    pins what the agent was actually told between the two passes."""
    import dataclasses
    _, base_deps = rig
    counting = _CountingAgent(base_deps.agent)

    deps = dataclasses.replace(base_deps, agent=counting)
    _, result = _file(clean_queue, deps, f"DOUBLE:bad-shape-once\n{ACME_MATERIAL}")

    assert result.status == schema.FILED
    assert counting.briefs[0] == ""                        # the first pass gets no brief
    assert ".librarian-outcome.json" in counting.briefs[1]  # the second is told where the fix is
    assert "[outcome]" in counting.briefs[1]


def test_a_summary_far_past_the_prose_ceiling_is_truncated_and_the_capture_FILES(rig, clean_queue):
    """The benign twin, end to end: the exact field that refused capture #3 on the first real walk,
    at ten times the length that refused it, must cost one agent pass and file."""
    import dataclasses
    env, base_deps = rig
    counting = _CountingAgent(base_deps.agent)
    deps = dataclasses.replace(base_deps, agent=counting)

    _, result = _file(clean_queue, deps, f"DOUBLE:long-summary\n{ACME_MATERIAL}")

    assert result.status == schema.FILED
    assert counting.calls == 1                             # no retry was needed at all
    page_path, sha = result.result_ref.rsplit("@", 1)
    assert support.read_filed_page(env.repo, sha, page_path).startswith("---")


# ── dedup, three levels ─────────────────────────────────────────────────────────────────────────
def test_retry_collapse_same_submitter_same_material_files_once_and_the_retry_points_at_it(
        rig, clean_queue):
    _, deps = rig
    submitter = "retry.tester@stigmergy.test"
    material = f"{ACME_MATERIAL} retry-collapse case."

    first_item, first_result = _file(clean_queue, deps, material, submitted_by=submitter)
    assert first_result.status == schema.FILED

    second_item, second_result = _file(clean_queue, deps, material, submitted_by=submitter)

    assert second_result.status == schema.FILED
    assert second_result.result_ref == first_result.result_ref
    assert second_result.report.get("retry_of") == first_item["id"]
    assert "retry" in second_result.report["summary"]


def test_already_filed_material_from_a_different_submitter_is_rejected_with_a_pointer(
        rig, clean_queue):
    env, deps = rig
    material = f"{ACME_MATERIAL} already-filed case."

    _, first_result = _file(clean_queue, deps, material, submitted_by="first@stigmergy.test")
    assert first_result.status == schema.FILED
    before = support.branch_sha(env.bare)

    _, second_result = _file(clean_queue, deps, material, submitted_by="second@stigmergy.test")

    assert second_result.status == schema.REJECTED
    first_page_path = first_result.result_ref.rsplit("@", 1)[0]
    assert first_page_path in second_result.report["summary"]
    assert support.branch_sha(env.bare) == before               # nothing new was committed


def test_near_duplicate_overlap_files_and_cross_links_both_pages(rig, clean_queue):
    """The third level of dedup — a near-duplicate overlap — end to end over real git, and through
    the DECLARATIVE mechanism: the agent writes only its new page and names the edit; `edits.apply`
    performs it; every gate, `gate_body_rewrite` included, judges the result."""
    env, deps = rig
    existing = "wiki/notes/Existing Note.md"
    before = support.read_filed_page(env.repo, "HEAD", existing)

    _, result = _file(clean_queue, deps, f"DOUBLE:overlap={existing}\n{ACME_MATERIAL}")

    assert result.status == schema.FILED
    assert result.report["overlaps_flagged"], "expected an overlap to be flagged"
    assert any(existing in o for o in result.report["overlaps_flagged"])

    page_path, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.repo, sha)
    assert page_path in changed
    assert existing in changed                                  # the OTHER side was also touched
    existing_after = support.read_filed_page(env.repo, sha, existing)
    assert "Overlaps with" in existing_after
    # BOTH halves the page contract asks for on the existing side: the callout AND `related:`
    title = page_path.rsplit("/", 1)[-1].removesuffix(".md")
    assert f"[[{title}]]" in page_policy.related_links(existing_after)
    # additive only — every line the human's page already had is still there, byte for byte
    for line in before.splitlines():
        assert line in existing_after.splitlines() or line.startswith("related:")
    assert page_policy.related_links(before)[0] in page_policy.related_links(existing_after)


def test_a_declared_backlink_adds_the_reciprocal_related_entry_and_commits_it(rig, clean_queue):
    env, deps = rig
    existing = "wiki/notes/Existing Note.md"

    _, result = _file(clean_queue, deps, f"DOUBLE:backlink={existing}\n{ACME_MATERIAL}")

    assert result.status == schema.FILED, result.report.get("summary")
    page_path, sha = result.result_ref.rsplit("@", 1)
    title = page_path.rsplit("/", 1)[-1].removesuffix(".md")
    existing_after = support.read_filed_page(env.repo, sha, existing)
    assert f"[[{title}]]" in page_policy.related_links(existing_after)
    assert "[!NOTE]" not in existing_after          # a backlink is not a callout


def test_a_declared_contradiction_places_a_warning_callout_on_the_other_page(rig, clean_queue):
    env, deps = rig
    existing = "wiki/notes/Existing Note.md"

    _, result = _file(clean_queue, deps, f"DOUBLE:contradict={existing}\n{ACME_MATERIAL}")

    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    existing_after = support.read_filed_page(env.repo, sha, existing)
    assert "> [!WARNING] Contradiction with" in existing_after


def test_the_bodyrewrite_gate_still_judges_codes_own_edits_and_lets_them_through(rig, clean_queue):
    """The gate that refused the agent twice is unchanged and now guards code. This asserts the
    specificity half: an edit code performed really does reach a commit, so "provably additive by
    construction" is a fact about the diff and not a claim in a comment."""
    env, deps = rig
    existing = "wiki/notes/Existing Note.md"
    _, result = _file(clean_queue, deps, f"DOUBLE:overlap={existing}\n{ACME_MATERIAL}")
    assert result.status == schema.FILED
    _, sha = result.result_ref.rsplit("@", 1)
    # the commit's own diff for the existing page removes exactly ONE line: its `related:` field
    diff = support.diff_of(env.repo, sha, existing)
    removed = [line for line in diff.splitlines()
               if line.startswith("-") and not line.startswith("---") and line[1:].strip()]
    assert len(removed) == 1
    assert removed[0].startswith("-related:")


class _RewritingEdits:
    """Wraps an agent and replaces the edits it declared. The seam for driving a declaration the
    double has no directive for, without teaching the double a case only one test wants."""

    def __init__(self, inner, edits):
        self.inner = inner
        # The declared port member, copied from what this wraps (ADR 033). Plain attribute
        # access with NO default: `processing._one_pass` refuses an agent that carries no
        # `structured_ordinary` rather than defaulting it, so a wrapper that swallowed the
        # declaration would silently change which shape of the ordinary flow runs behind it.
        # Reading it here means a wrapper around a non-conforming backend fails at
        # CONSTRUCTION, in the test that built it, instead of one queue delivery at a time.
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.edits = edits

    def run(self, **kwargs):
        import dataclasses
        run = self.inner.run(**kwargs)
        if run.outcome is not None:
            run.outcome = dataclasses.replace(run.outcome, edits=self.edits)
        return run



def test_a_declared_callout_with_no_figures_is_the_benign_twin_and_files(rig, clean_queue):
    """The specificity half: the gates that judge code's own edits must not refuse an ordinary
    overlap note. Its prose carries no digits at all, so nothing in it can be mistaken for a claim
    a gate has an opinion about — the callout reaches the other page and the capture files."""
    import dataclasses
    env, base_deps = rig
    existing = "wiki/notes/Existing Note.md"
    edits = ({"path": existing, "kind": "overlap", "link": ACME_TITLE,
              "note": "both describe the same renewal, from different angles"},)
    deps = dataclasses.replace(base_deps, agent=_RewritingEdits(base_deps.agent, edits))

    _, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FILED, result.report.get("summary")
    _, sha = result.result_ref.rsplit("@", 1)
    assert "different angles" in support.read_filed_page(env.repo, sha, existing)


@pytest.mark.parametrize("bad_target,label", [
    ("ops/acl.json", "outside the creatable folders"),
    ("wiki/notes/Does Not Exist.md", "a page that is not there"),
])
def test_a_declared_edit_code_refuses_produces_no_commit(rig, clean_queue, bad_target, label):
    """The adversarial twin. A declaration is untrusted input: the target has to exist and be in
    the lane, and a bad one refuses the whole capture rather than being silently skipped."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    _, result = _file(clean_queue, deps, f"DOUBLE:bad-edit={bad_target}\n{ACME_MATERIAL}")

    assert result.status in (schema.REJECTED, schema.FAILED), label
    assert result.result_ref == ""
    assert support.branch_sha(env.bare) == before


def test_a_declared_edit_never_reaches_a_page_this_capture_created(rig, clean_queue):
    """`own-page`: an edit declared against the page the agent just wrote is a confusion, not an
    edit — the link belongs in the new page itself, which the agent may write freely."""
    env, deps = rig
    before = support.branch_sha(env.bare)
    _, result = _file(clean_queue, deps, f"DOUBLE:bad-edit={ACME_PAGE}\n{ACME_MATERIAL}")
    assert result.status in (schema.REJECTED, schema.FAILED)
    assert support.branch_sha(env.bare) == before


def test_the_report_names_every_OTHER_page_the_commit_changed(rig, clean_queue):
    """`processing` used to compute the list of pages `edits.apply` actually changed and drop it on
    the floor, so the commit touched a colleague's page and no surface a human reads said so — not
    the submitter's report, not `capture_queue`, not the CLI's prose.

    `pages_edited` is what CODE wrote, which is not `overlaps_flagged` (the agent's judgment about
    which pages overlap): the two can differ, and the one that describes a write is this one."""
    env, deps = rig
    existing = "wiki/notes/Existing Note.md"

    item, result = _file(clean_queue, deps, f"DOUBLE:backlink={existing}\n{ACME_MATERIAL}")

    assert result.status == schema.FILED, result.report.get("summary")
    assert result.report["pages_edited"] == [existing]
    # the report and the commit agree about it
    _, sha = result.result_ref.rsplit("@", 1)
    assert existing in support.changed_paths(env.repo, sha)
    # and the CLI's own rendering carries it, since that is where a walk reads outcomes
    assert existing in report_module.render_prose(result.report)
    # the queue row too — an operator greps `capture_queue`, not a terminal that has scrolled away
    assert _row(clean_queue, item["id"])["report"]["pages_edited"] == [existing]


def test_a_capture_that_edited_nothing_says_so_rather_than_omitting_the_field(rig, clean_queue):
    """"Nothing is silently omitted" (`report.py`): an empty list renders as `(none)`, so a reader
    can tell "this capture changed no other page" from "this report predates the field"."""
    _, deps = rig
    _, result = _file(clean_queue, deps, ACME_MATERIAL)
    assert result.status == schema.FILED
    assert result.report["pages_edited"] == []
    assert "pages_edited     (none)" in report_module.render_prose(result.report)


class _TwoPageAgent:
    """Wraps the double and writes a SECOND new page beside the one it declared.

    Unreachable through any directive the double has, and that is the point: the double is
    well-behaved except where a directive says otherwise, so "the agent created two pages" — an
    ordinary way for a model to overshoot, no attack needed — was never exercised.
    """

    def __init__(self, inner):
        self.inner = inner
        # The declared port member, copied from what this wraps (ADR 033). Plain attribute
        # access with NO default: `processing._one_pass` refuses an agent that carries no
        # `structured_ordinary` rather than defaulting it, so a wrapper that swallowed the
        # declaration would silently change which shape of the ordinary flow runs behind it.
        # Reading it here means a wrapper around a non-conforming backend fails at
        # CONSTRUCTION, in the test that built it, instead of one queue delivery at a time.
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered

    def run(self, **kwargs):
        run = self.inner.run(**kwargs)
        if run.outcome is not None and run.outcome.decision == "file":
            extra = os.path.join(kwargs["worktree"], "wiki/notes/A Second Page.md")
            with open(os.path.join(kwargs["worktree"], run.outcome.page_path),
                      encoding="utf-8") as f:
                text = f.read()
            with open(extra, "w", encoding="utf-8") as f:
                f.write(text.replace("A short note", "A second note"))
        return run


def test_a_capture_that_creates_a_second_page_is_refused_rather_than_filing_it_unreported(
        rig, clean_queue):
    """`_file` takes `in_lane_new_pages()[0]` — the alphabetically first entry of `git diff --raw` —
    for `page_path`, `result_ref`, the commit subject, the dedup pointer and the whole report. A
    second page would be committed, stamped and pushed while appearing on no surface a human reads,
    and the anchoring check of the day unioned wikilinks across ALL new pages needing only one to
    resolve, so an unanchored second page rode in on the first's coat-tails.

    "One capture, one commit" was true of commits and 1:N in pages. Note the alphabetical detail is
    what makes it worse than a shrug: `A Second Page.md` sorts BEFORE the declared page, so the
    report would have named the page the agent never claimed to file.
    """
    import dataclasses
    env, base_deps = rig
    before = support.branch_sha(env.bare)
    deps = dataclasses.replace(base_deps, agent=_TwoPageAgent(base_deps.agent))

    item, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FAILED
    assert result.result_ref == ""
    assert "files exactly one page" in result.report["summary"]
    assert support.branch_sha(env.bare) == before          # neither page reached the remote
    assert _row(clean_queue, item["id"])["result_ref"] == ""


# ── the diff is the veto surface ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("directive,expect_path", [
    ("escape", "ops/acl.json"),
    ("delete", "wiki/"),
    ("rewrite", "wiki/"),
])
def test_a_diff_that_breaks_the_lane_produces_no_commit_and_names_the_path(
        rig, clean_queue, directive, expect_path):
    env, deps = rig
    before = support.branch_sha(env.bare)

    item, result = _file(clean_queue, deps, f"DOUBLE:{directive}\n{ACME_MATERIAL}")

    assert result.status in (schema.REJECTED, schema.FAILED)
    assert result.result_ref == ""
    # the test's own promise, checked rather than assumed: the offending path is actually named in
    # what the submitter/operator reads, not merely implied by the terminal state.
    assert expect_path in result.report["summary"], result.report["summary"]
    assert support.branch_sha(env.bare) == before
    row = _row(clean_queue, item["id"])
    assert row["result_ref"] == ""


# ── untrusted material cannot steer the librarian ───────────────────────────────────────────────
# **These live in `test_adversarial.py`** as permanent cat. 1 cases, including material asking in
# prose to be filed as canonical. One case per category in `gates.INJECTION_CATEGORIES`, plus the
# parametrized "no report ever quotes the instruction back" and the benign twin that ordinary prose
# raises no steering finding at all. Collected with `pytest -k adversarial_cat1`.


# ── the report is honest and complete ───────────────────────────────────────────────────────────
def test_filed_report_carries_every_promised_field(rig, clean_queue):
    _, deps = rig
    _, result = _file(clean_queue, deps, ACME_MATERIAL)
    report = result.report
    assert report["status"] == schema.FILED
    assert report["summary"].startswith(schema.FILED)
    for key in ("page_path", "commit", "anchored_to", "links_created", "overlaps_flagged",
               "findings"):
        assert key in report, key


def test_rejected_report_names_the_reason_and_the_corrective_action(rig, clean_queue):
    _, deps = rig
    _, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\nToken: {payloads.GITHUB_PAT}")
    assert result.report["summary"].startswith(schema.REJECTED)
    assert "resubmit" in result.report["summary"] or "Remove" in result.report["summary"]


# ── non-ASCII titles survive, everywhere (the walk's permanent damage) ──────────────────────────
# "Reunion" with an accent was filed as "Reuni n" — a space where the accented character stood — in
# the FILENAME, the H1, the `title` frontmatter AND the commit subject, on the real `main`, three
# times over. The body was byte-correct throughout, which is what identified the cause: a title
# sanitizer spelled as an ASCII whitelist, not an encoding problem anywhere in the pipeline.
ACCENTED_MATERIAL = ("Zürich Review with Meridian Partners about Acme Corp\n"
                     "We agreed to revisit the renewal contract next week.")


def _nfc(text: str) -> str:
    """macOS filesystems may hand back a decomposed spelling of the same characters; the question
    here is whether the CHARACTER survived, not which normal form git and APFS agreed on."""
    return unicodedata.normalize("NFC", text)


def test_an_accented_title_survives_into_the_filename_the_h1_the_title_field_and_the_subject(
        rig, clean_queue):
    env, deps = rig
    _, result = _file(clean_queue, deps, ACCENTED_MATERIAL)
    assert result.status == schema.FILED, result.report.get("summary")

    page_path, sha = result.result_ref.rsplit("@", 1)
    assert "ü" in _nfc(page_path), f"the filename lost its accent: {page_path!r}"
    assert "Z rich" not in _nfc(page_path)

    filed_page = _nfc(support.read_filed_page(env.repo, sha, page_path))
    assert "# Zürich" in filed_page
    assert 'title: "Zürich' in filed_page
    assert "Z rich" not in filed_page

    subject = _nfc(support.commit_subject(env.repo, sha))
    assert "Zürich" in subject
    assert "Z rich" not in subject


def test_the_double_is_refused_by_confined_write_when_its_title_collides_with_an_existing_page(
        rig, clean_queue, monkeypatch):
    """The reason a whole class of defect survived a green suite: `double.py` wrote with a bare
    `open(path, "w")` and never consulted `agent.confined_write`, so the rule the SDK backend's
    `PreToolUse` hook enforces was unreachable from the whole offline suite — every processing
    test, every adversarial case and the docker e2e exercised a write path production does not use.
    It is routed through the rule now, and this is the case that catches a byte-comparison defect
    the moment a fixture uses a re-spelled page name: the title resolves to an existing page's
    file, the rule denies it, and the run fails LOUDLY rather than silently clobbering
    `Existing Note.md` and reporting a filing.
    """
    env, deps = rig
    before = support.branch_sha(env.bare)
    monkeypatch.setattr(double_module.DoubleAgent, "_title",
                        staticmethod(lambda material: "existing note"))   # lower-cased on purpose

    _, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FAILED
    assert "confined_write denies" in result.report["summary"]
    assert support.branch_sha(env.bare) == before
    # the human's page is untouched, which is the property the rule exists for
    assert "A short note" not in support.read_filed_page(
        env.repo, "HEAD", "wiki/notes/Existing Note.md")


def test_the_double_files_normally_when_its_title_collides_with_nothing(rig, clean_queue):
    """The benign twin for the routing above: the ordinary case must still write, or every test in
    this file would be proving a refusal."""
    _, deps = rig
    _, result = _file(clean_queue, deps, ACME_MATERIAL)
    assert result.status == schema.FILED, result.report.get("summary")


def test_a_page_name_carrying_a_control_character_is_refused_rather_than_mangled(
        rig, clean_queue, monkeypatch):
    """The other half of the same rule: where a character genuinely cannot be in a filename, the
    NAME is refused and said so, never silently approximated. Driven by making the double produce a
    name no sanitizer would have emitted, so the ZONE GATE is what answers."""
    env, deps = rig
    before = support.branch_sha(env.bare)
    monkeypatch.setattr(double_module.DoubleAgent, "_title",
                        staticmethod(lambda material: "Bad\x0bName"))

    _, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FAILED
    assert support.branch_sha(env.bare) == before
    assert "cannot be filed under that name" in json.dumps(result.report)





# ── the orphan warning that always fired ────────────────────────────────────────────────────────
def test_a_freshly_filed_page_carries_no_orphan_warning_because_nothing_can_link_it_yet(
        rig, clean_queue):
    """A warning that fires on every filed page by construction teaches people to ignore warnings:
    nothing in the repo can link a page that has just been born."""
    _, deps = rig
    _, result = _file(clean_queue, deps, ACME_MATERIAL)
    assert result.status == schema.FILED
    assert "no inbound links" not in json.dumps(result.report)
    assert result.report["findings"] == []


# ── the failure report: both counters, no false determinism ──────────────────────────────────────
def test_a_zone_veto_report_names_the_delivery_and_the_agent_attempts_and_promises_nothing(
        rig, clean_queue):
    """The exact message the first real walk produced, corrected. `DOUBLE:rewrite` is the agent
    stepping out of its lane — which is what happened live, twice — so this is the same code path,
    asserted.

    **The agent counter says ONE.** It said two, and two passes really did run: `zone/body-rewrite`
    is `repairable=False`, so the retry is no longer spent on it. Both numbers are still present and
    each still says which one it is, which is the property this test was written for; what changed
    is that the second one is now true of a run that took the honest answer immediately instead of
    buying it an agent pass later.
    """
    _, deps = rig
    item, result = _file(clean_queue, deps, f"DOUBLE:rewrite\n{ACME_MATERIAL}")

    assert result.status == schema.FAILED
    summary = result.report["summary"]
    assert f"queue delivery {item['attempts']}" in summary
    assert "1 agent attempt inside it" in summary         # singular, and no retry was spent
    assert "will hit the same fault" not in summary
    assert "attempts (last problem" not in summary        # the old, ambiguous phrasing
    assert result.report["deliveries"] == item["attempts"]
    assert result.report["agent_attempts"] == 1


# ── a veto with no agent-side repair does not spend the retry ───────────────────────────────────
def test_a_body_rewrite_refuses_after_one_pass_and_the_agent_is_never_asked_to_repair_it(
        rig, clean_queue):
    """The control-flow half, proven against the double: same terminal state, one agent run instead
    of two, and — the part that matters most — the agent is never handed a brief.

    The measured cost of the old behaviour is what makes this worth a test of its own. Since the
    declarative-edits amendment the agent cannot write to an existing page at all, so *"you rewrote
    existing content in X"* named work it did not do; the second pass could only reproduce the same
    refusal, because `edits.apply_declared` applies the same declaration to the same base page. The
    run paid for an agent pass to arrive at the answer it already had.

    `DOUBLE:rewrite` is the one directive that still edits a committed page directly (the double's
    module docstring says why: a double that could not would leave this gate untested), which makes
    it the only offline way to reach the finding at all.
    """
    import dataclasses
    env, base_deps = rig
    counting = _CountingAgent(base_deps.agent)
    deps = dataclasses.replace(base_deps, agent=counting)
    before = support.branch_sha(env.bare)

    item, result = _file(clean_queue, deps, f"DOUBLE:rewrite\n{ACME_MATERIAL}")

    assert result.status == schema.FAILED                 # the destination is unchanged
    assert counting.calls == 1                            # and it cost ONE agent pass, not two
    assert counting.briefs == [""]                        # no brief was composed at all
    assert support.branch_sha(env.bare) == before         # nothing committed, as before
    assert _row(clean_queue, item["id"])["status"] == schema.FAILED



# ── the refused diff, preserved (a veto used to reap the only evidence) ──────────────────────────
def _with_diagnostics(base_deps, directory):
    import dataclasses
    return dataclasses.replace(
        base_deps,
        settings=dataclasses.replace(base_deps.settings, refused_diff_root=str(directory)))


def test_a_refused_body_rewrite_preserves_the_diff_it_refused(rig, clean_queue, tmp_path):
    """The report says THAT a body was rewritten; without the worktree, nothing said WHAT changed —
    a debugging dead end for a defect that will recur."""
    _, base_deps = rig
    deps = _with_diagnostics(base_deps, tmp_path / "refused")

    item, result = _file(clean_queue, deps, f"DOUBLE:rewrite\n{ACME_MATERIAL}")

    assert result.status == schema.FAILED
    assert result.diagnostics_path, "the refused diff was not preserved"
    text = pathlib.Path(result.diagnostics_path).read_text(encoding="utf-8")
    assert "zone/body-rewrite" in text
    assert "wiki/" in text
    assert f"submission: {item['id']}" in text
    # the removed line — the thing that was about to be destroyed — is what a reader needs
    assert any(line.strip().startswith("-") for line in text.splitlines())


def test_the_preserved_diff_never_carries_the_captured_material(rig, clean_queue, tmp_path):
    """The same discipline as a secret in a log: added lines are the librarian's draft of untrusted
    material, so they are withheld entirely and only counted."""
    _, base_deps = rig
    deps = _with_diagnostics(base_deps, tmp_path / "refused")
    payload = "SENSITIVE-CAPTURED-SENTENCE-DO-NOT-LEAK"

    _, result = _file(clean_queue, deps, f"DOUBLE:rewrite\n{ACME_MATERIAL} {payload}")

    assert result.diagnostics_path
    text = pathlib.Path(result.diagnostics_path).read_text(encoding="utf-8")
    assert payload not in text
    assert "withheld" in text                       # and it says so, rather than looking empty
    assert len(text) <= processing.REFUSED_DIFF_MAX_BYTES


def test_the_preserved_diff_path_is_never_in_the_submitter_facing_report(
        rig, clean_queue, tmp_path):
    """It names a local file for an operator. A path in `brain_submissions` is an operational detail
    crossing to somebody who cannot act on it."""
    _, base_deps = rig
    deps = _with_diagnostics(base_deps, tmp_path / "refused")
    item, result = _file(clean_queue, deps, f"DOUBLE:rewrite\n{ACME_MATERIAL}")
    assert result.diagnostics_path not in json.dumps(result.report)
    assert result.diagnostics_path not in json.dumps(_row(clean_queue, item["id"])["report"])


# ── retry collapse is reachable when the queue lags, which is what a walk does ───────────────────
def _backdate(conn, submission_id: int, seconds: int) -> None:
    """Move a row's `created_at` back, so the two submissions' PROXIMITY and the wall clock can be
    varied independently — the exact distinction the defect collapsed."""
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET created_at = now() - make_interval(secs => %s) "
                    "WHERE id = %s", (seconds, submission_id))


def test_a_retry_is_recognised_even_when_the_rows_are_processed_long_after_they_arrived(
        rig, clean_queue):
    """The window used to be measured from `now()` — PROCESSING time — so it closed as the queue
    lagged, and level 2 (which has no window at all) answered both rows with "rejected, this
    matches a page already in the graph". A walk drains with `stigmergy-librarian once`, by hand,
    minutes apart, which is exactly the condition that made level 1 unreachable.

    Here the two rows are five seconds apart and both an hour old: `now()` is far outside a
    ten-minute window while the two SUBMISSIONS are five seconds apart — a retry by any reading of
    the definition.
    """
    _, deps = rig
    submitter = "lagged.retry@stigmergy.test"
    material = f"{ACME_MATERIAL} lagged retry case."

    first = support.submit(clean_queue, deps, material, submitted_by=submitter)
    second = support.submit(clean_queue, deps, material, submitted_by=submitter)
    _backdate(clean_queue, first["id"], 3605)
    _backdate(clean_queue, second["id"], 3600)

    _, first_result = worker.process_next(clean_queue, deps)
    assert first_result.status == schema.FILED

    _, second_result = worker.process_next(clean_queue, deps)
    assert second_result.status == schema.FILED, second_result.report.get("summary")
    assert second_result.result_ref == first_result.result_ref
    assert second_result.report.get("retry_of") == first["id"]
    assert schema.REJECTED not in second_result.report["summary"]


def test_a_resubmission_outside_the_window_is_still_the_level_two_rejection(rig, clean_queue):
    """The twin: anchoring the window on the submission must not turn every later re-filing into a
    retry. Deliberately re-filing the same material tomorrow is a new capture, and level 2 is the
    honest answer for it."""
    _, deps = rig
    submitter = "deliberate.refile@stigmergy.test"
    material = f"{ACME_MATERIAL} deliberate re-filing case."

    first = support.submit(clean_queue, deps, material, submitted_by=submitter)
    support.submit(clean_queue, deps, material, submitted_by=submitter)
    _backdate(clean_queue, first["id"], 7200)

    _, first_result = worker.process_next(clean_queue, deps)
    assert first_result.status == schema.FILED
    _, second_result = worker.process_next(clean_queue, deps)

    assert second_result.status == schema.REJECTED
    assert "already in the graph" in second_result.report["summary"]
    assert second_result.report.get("retry_of") is None

# ── removed with ingest-time figure verification, and named rather than dropped in silence ──────
# A check that stops running must be impossible to miss, so what left is listed here instead of
# vanishing from the file. The tests below drove a HALLUCINATED FIGURE through the fast lane and
# asserted that a figure-verification gate vetoed it, that one corrective retry recovered it, or
# that the resulting report carried the right verdict. That gate is gone
# ([ADR 026](../../docs/decisions/026-the-purge.md) D2): ingest-time figure verification went with
# the trust layer, deliberately, and the accepted consequence is stated there — **an invented
# figure CAN sit on a page.** The reader's protection is the verbatim source one click away, the
# gardener, and `answer.verify_answer` at query time.
#
# So these are removed, not repaired: their subject no longer exists, and a test rewritten to
# assert the opposite would be measuring a decision, not a mechanism. What they ALSO covered
# incidentally — atomicity, the once-directive, the steering veto — is covered by the remaining
# tests in this file, which reach the same refusal shape through vetoes that still exist: the
# page-collision precheck, the second-meeting-page arity veto, and the additive-edit refusal.
# (An earlier version of this note said "zone, anchoring, secrets"; those are not the ones that
# actually carry it. Named correctly here rather than left approximately right.)
#
# Removed:
#   `test_a_figure_inside_a_declared_callout_is_traced_like_any_other_claim`,
#   `test_a_filed_page_that_really_carries_a_figure_reports_the_verdict`,
#   `test_a_filed_page_with_no_figures_reports_no_figures_not_verified`,
#   `test_a_repairable_veto_still_spends_the_retry_and_can_still_recover`,
#   `test_hallucinate_every_attempt_is_rejected_and_files_no_page`,
#   `test_hallucinate_once_succeeds_on_the_corrective_retry_and_is_filed`,
#   `test_the_page_itself_still_carries_the_verdict_vocabulary_the_linter_validates`


# ── a registration (ADR 042/044): the capture births the entity CONFIRMED by its submitter ──────
def _registration_hints(name="Globex Corp", entity_type="organization", aliases=("Globex",)):
    return schema.registration_hints(name=name, entity_type=entity_type, aliases=aliases,
                                     source="admin")


def test_a_registration_births_the_entity_confirmed_by_the_person_who_asked(rig, clean_queue):
    """OLD BEHAVIOUR (twice over): a script copied the entity template with the name filled in, and
    then — briefly — only a registration was born confirmed while every other identity waited on a
    steward. Now the account is a capture like any other: the agent writes the page from it, the
    page lands in the same commit as the note with `approved_by` naming the SUBMITTER, the registry
    entry carries the same name, and the report says what their capture introduced."""
    env, deps = rig
    submitter = "steward@example.com"

    item, result = _file(clean_queue, deps, "DOUBLE:propose=Globex Corp\n" + ACME_MATERIAL,
                         submitted_by=submitter, hints=_registration_hints())

    assert result.status == schema.FILED, result.report.get("summary")
    page_path, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.bare, sha)
    assert "wiki/entities/Globex Corp.md" in changed and "ops/entity-registry.json" in changed
    entity_page = support.read_filed_page(env.bare, sha, "wiki/entities/Globex Corp.md")
    assert f'approved_by: "{submitter}"' in entity_page
    assert "proposed by the offline double" in entity_page        # written, never a stub
    registry = json.loads(support.read_filed_page(env.bare, sha, "ops/entity-registry.json"))
    entry = registry["entities"]["globex-corp"]
    assert entry["approved_by"] == submitter
    assert "proposed" not in entry and "proposed_aliases" not in entry
    assert result.report["entities_born"] == [
        {"id": "globex-corp", "name": "Globex Corp", "type": "organization",
         "confirmed_by": submitter}]
    assert "It introduces 1 new entity: Globex Corp (`globex-corp`)" in result.report["summary"]
    assert "confirmed by you" in result.report["summary"]
    assert _row(clean_queue, item["id"])["status"] == schema.FILED


def test_a_registration_the_agent_ignores_fails_by_name_and_commits_nothing(rig, clean_queue):
    """The capture asked for Globex Corp and the account introduced nothing, twice (the double
    introduces only on its directive, so the corrective retry changes nothing): the row ends
    `failed` naming the entity and the remote is untouched."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    item, result = _file(clean_queue, deps, ACME_MATERIAL, submitted_by="steward@example.com",
                         hints=_registration_hints())

    assert result.status == schema.FAILED, result.report.get("summary")
    assert "registration-missing" in result.report["summary"] or "Globex Corp" in result.report["summary"]
    assert support.branch_sha(env.bare) == before


# ── the spine accretes (ADR 042): a filing adds what it established to a registered entity ───────
def test_a_capture_that_establishes_something_about_a_registered_entity_appends_it_to_the_page(
        rig, clean_queue):
    """OLD BEHAVIOUR: the entity page was written at birth and never again; what later captures
    established went to notes and to the synthesized view, and the spine stayed as thin as the
    day it was born. The account now declares `entity_updates`; code appends the lines under the
    page's own sections, proves the bytes, and the same commit carries the note and the grown
    page. The report says what was added."""
    env, deps = rig
    page = "wiki/entities/Acme Corp.md"
    before = support.read_filed_page(env.bare, support.branch_sha(env.bare), page)

    item, result = _file(clean_queue, deps, "DOUBLE:update=acme-corp\n" + ACME_MATERIAL)

    assert result.status == schema.FILED, result.report.get("summary")
    page_path, sha = result.result_ref.rsplit("@", 1)
    changed = support.changed_paths(env.bare, sha)
    assert page_path in changed and page in changed
    after = support.read_filed_page(env.bare, sha, page)
    assert after.startswith(before.split("updated:")[0])          # appended, never rewritten
    assert "- Established by the capture filed as" in after
    assert f"- [[{ACME_TITLE}]] — the note that established it" in after
    assert result.report["entities_updated"] == [{"entity": "acme-corp", "facts": 2, "connections": 1}]
    assert "It adds 2 facts and 1 connection to the page of `acme-corp`." in result.report["summary"]
    assert result.report["entities_born"] == []
    assert _row(clean_queue, item["id"])["status"] == schema.FILED
