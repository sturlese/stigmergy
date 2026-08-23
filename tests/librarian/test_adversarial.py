"""The librarian's permanent adversarial cases.

The suite carries adversarial cases across seven categories, and the collection convention
(`tests/answer/test_adversarial_cat1.py`, `tests/capture/test_adversarial_cat7.py`) is a **naming**
one: one plain test function per case, prefixed `test_adversarial_cat{N}_`, so
`pytest -k adversarial_cat5` finds every cat. 5 case wherever it lives. Three of them are the
librarian's:

- **cat. 5 (PII/sensitive)**: the secrets gate and each of the four PII patterns.
- **cat. 7 (forged frontmatter)**, asserted on the FILED PAGE — cat. 7 also has seven cases at the
  queue level (`tests/capture/test_adversarial_cat7.py`), which prove the declaration is recorded
  and never trusted. This is the other half: what the committed page says.
- **cat. 1: "the capture tries to steer the librarian"**, one case per category in
  `gates.INJECTION_CATEGORIES`.

**One file for three categories**, unlike the two single-category ones, because all three share a
rig that is expensive to build (real Postgres, a real git repo + bare remote, a real gitleaks) and
splitting them by category would triple that for no gain. The collection convention is the prefix,
not the filename, so `-k adversarial_cat5` still works.

These cases live here rather than being duplicated into `test_processing_pg.py`'s own sections —
duplicated assertions across two files are two things to keep in step — and those sections carry a
pointer here so the map from rule to test stays navigable. Their benign twins stay there: the
benign-twin test is one parametrized case covering every gate at once, which belongs beside the
rule it twins rather than in an adversarial suite.
"""
import json
import os

import pytest

from stigmergy.capture import queue, schema
from stigmergy.librarian import gates, gitcmd, worker
from stigmergy.librarian import report as report_module
from tests import adversarial_payloads as payloads
from tests.librarian import support

ACME_MATERIAL = "A short note about how the Acme Corp renewal is going."


def _file(conn, deps, material, **kw):
    support.submit(conn, deps, material, **kw)
    return worker.process_next(conn, deps)


# ── cat. 5: PII / sensitive — the whole capture bounces, and the value never travels ──────────────
def test_adversarial_cat5_a_real_secret_shape_bounces_the_whole_capture(rig, clean_queue):
    """gitleaks is the independent ground truth: a document can talk an agent out of a finding and
    cannot talk gitleaks out of one. Bounced WHOLE, never redacted — a wrong redaction leaves the
    secret in place while looking reviewed."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    item, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\nToken: {payloads.GITHUB_PAT}")

    assert result.status == schema.REJECTED
    assert result.result_ref == ""
    assert support.branch_sha(env.bare) == before          # no commit, no partial file
    report_text = json.dumps(result.report)
    assert payloads.GITHUB_PAT not in report_text          # never the value
    assert "github-pat" in report_text                    # the scanner really fired, named

    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == schema.REJECTED
    assert payloads.GITHUB_PAT not in json.dumps(row)


@pytest.mark.parametrize("label,fragment", [
    ("private_key", payloads.BARE_PRIVATE_KEY_HEADER),
    ("luhn_valid_card", f"Card on file: {payloads.LUHN_VALID_CARD} for the deposit."),
    ("iban", f"Wire to IBAN {payloads.IBAN} before Friday."),
    ("dni", f"His DNI is {payloads.DNI} on the contract."),
])
def test_adversarial_cat5_each_pii_pattern_bounces_the_whole_capture(rig, clean_queue, label,
                                                                    fragment):
    """The four patterns the PII scanner deliberately limits itself to. Emails and names are NOT
    here and must not bounce — that half is the benign twin in `test_processing_pg.py`, and the
    pairing is what measures specificity rather than sensitivity."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    _, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\n{fragment}")

    assert result.status == schema.REJECTED, label
    assert support.branch_sha(env.bare) == before
    assert fragment not in json.dumps(result.report)       # never the value


def test_adversarial_cat5_the_refusal_names_a_locator_and_a_kind_and_nothing_else(rig, clean_queue):
    """A secret in an error message is a secret in a log. The report may name WHERE and WHAT KIND —
    that is what makes it actionable — and nothing that could be pasted back into a service."""
    _, deps = rig
    _, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\nIBAN {payloads.IBAN}")

    summary = result.report["summary"]
    assert "near line" in summary
    assert "IBAN" in summary
    assert payloads.IBAN not in summary
    assert payloads.IBAN.replace(" ", "") not in summary   # nor with the spacing normalized away


def test_adversarial_cat5_a_secrets_refusal_is_not_served_back_by_the_submissions_listing(
        rig, clean_queue):
    """The refusal's own promise, checked on the surface that reports it.

    `report.rejected_secret` says the value is not repeated in any read-back of this capture, and
    `brain_submissions`/`stigmergy-queue list` were serving a 500-character excerpt of exactly that
    material in the same object as the sentence — to the submitter on every call, and to any
    unrestricted identity for EVERYBODY's captures, until retention purged the payload 30 days
    later. Both read paths go through `queue.query_submissions`, so both are asserted here.

    Scanned over the WHOLE rendered row rather than over `excerpt` alone — the same shape the
    no-echo rule takes below: the planted value must be absent from every field, including the hint
    the submitter attached, which carries the same string and is never scanned by anything.
    """
    _, deps = rig
    item, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\nToken: {payloads.GITHUB_PAT}",
                         hints={"title": f"rotate {payloads.GITHUB_PAT}"})
    assert result.status == schema.REJECTED

    own = queue.list_own_submissions(clean_queue, support.DEFAULT_SUBMITTER)
    everybody = queue.list_all_submissions(clean_queue)          # the steward's management scope
    for scope, rows in (("own", own), ("all", everybody)):
        row = next(r for r in rows if r["id"] == item["id"])
        assert payloads.GITHUB_PAT not in json.dumps(row), scope
        assert row["excerpt"] == "", scope
        assert row["hints"] == {}, scope
        assert row["withheld_reason"], scope                     # and it SAYS why, not silently


def test_adversarial_cat5_a_pii_refusal_is_not_served_back_either(rig, clean_queue):
    """The same guarantee for the other half of `schema.WITHHELD_REASONS`. Both refusals bounce a
    capture BECAUSE of what its material carries, so both owe the same silence on the way out."""
    _, deps = rig
    item, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\nWire to IBAN {payloads.IBAN}.")
    assert result.status == schema.REJECTED

    row = next(r for r in queue.list_all_submissions(clean_queue) if r["id"] == item["id"])
    rendered = json.dumps(row)
    assert payloads.IBAN not in rendered
    assert payloads.IBAN.replace(" ", "") not in rendered
    assert row["excerpt"] == ""


# ── a secret/PII rejection purges its payload IMMEDIATELY, not in 30 days ─────────────────────────
def test_adversarial_cat5_a_secret_rejection_purges_its_payload_immediately(rig, clean_queue):
    """The ordinary 30-day retention window is the wrong clock for material the system has already
    declared unsafe to keep sitting around. `worker._finish` calls
    `retention.purge_secret_capture_immediately` right after the row lands `rejected` with a
    `secret` reason code — asserted here by reading the columns back as NULL, never by the CLI's
    own output, which is a second story about the same rows."""
    _, deps = rig
    item, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\nToken: {payloads.GITHUB_PAT}")
    assert result.status == schema.REJECTED
    assert result.report.get(schema.REASON_CODE_KEY) == schema.REASON_SECRET

    with clean_queue.cursor() as cur:
        cur.execute("SELECT payload, hints FROM capture_queue WHERE id = %s", (item["id"],))
        payload, hints = cur.fetchone()
    assert payload is None
    assert hints is None

    with clean_queue.cursor() as cur:
        cur.execute("SELECT stats FROM job_runs WHERE job = 'capture-purge-immediate' "
                    "ORDER BY id DESC LIMIT 1")
        stats = cur.fetchone()[0]
    assert stats["submission_id"] == item["id"]
    assert stats["reason_code"] == schema.REASON_SECRET
    assert stats["purged"] is True


def test_adversarial_cat5_a_pii_rejection_purges_its_payload_immediately_too(rig, clean_queue):
    """The other half of `schema.WITHHELD_REASONS` gets the same immediate purge."""
    _, deps = rig
    item, result = _file(clean_queue, deps, f"{ACME_MATERIAL}\nWire to IBAN {payloads.IBAN}.")
    assert result.status == schema.REJECTED
    assert result.report.get(schema.REASON_CODE_KEY) == schema.REASON_PII

    with clean_queue.cursor() as cur:
        cur.execute("SELECT payload, hints FROM capture_queue WHERE id = %s", (item["id"],))
        payload, hints = cur.fetchone()
    assert payload is None
    assert hints is None


def test_adversarial_cat5_an_ordinary_rejection_keeps_its_payload_for_the_ordinary_window(
        rig, clean_queue):
    """The negative case: a rejection for a reason OTHER than secret/PII — a duplicate, say — is
    unaffected and still waits on the ordinary 30-day retention job. This is what proves the
    immediate purge is keyed on the reason, not on `status = rejected`."""
    _, deps = rig
    item, result = _file(clean_queue, deps, ACME_MATERIAL)          # files clean the first time
    assert result.status == schema.FILED
    # a second, identical capture is an exact duplicate of the page just filed
    item2, result2 = _file(clean_queue, deps, ACME_MATERIAL, submitted_by="second-submitter")
    assert result2.status == schema.REJECTED
    assert result2.report.get(schema.REASON_CODE_KEY) == schema.REASON_DUPLICATE

    with clean_queue.cursor() as cur:
        cur.execute("SELECT payload, hints FROM capture_queue WHERE id = %s", (item2["id"],))
        payload, hints = cur.fetchone()
    assert payload is not None
    assert hints is not None


def test_adversarial_cat5_benign_twin_an_ordinary_capture_still_shows_its_excerpt(rig,
                                                                                 clean_queue):
    """The specificity half, and the reason the suppression is keyed on a reason code rather than
    on `status = rejected` alone going forward: withholding must fire on the capture that carried
    a secret and on nothing else. A listing that stopped echoing every row would pass both tests
    above while destroying the surface they are about."""
    _, deps = rig
    item, result = _file(clean_queue, deps, ACME_MATERIAL)
    assert result.status == schema.FILED

    row = next(r for r in queue.list_all_submissions(clean_queue) if r["id"] == item["id"])
    assert ACME_MATERIAL[:40] in row["excerpt"]
    assert row["withheld_reason"] == ""


# ── cat. 7: forged frontmatter, asserted on the FILED PAGE ────────────────────────────────────────
def test_adversarial_cat7_a_capture_cannot_declare_itself_canonical_on_the_filed_page(rig,
                                                                                     clean_queue):
    """`status` is server-computed metadata, and canon is reached only through a reviewed PR — so a
    pre-drafted `status: canonical` is not an error to refuse, it is a claim to overwrite. Asserted
    on the committed page, because the queue row was never the thing at risk."""
    env, deps = rig
    _, result = _file(clean_queue, deps, f"DOUBLE:canonical\n{ACME_MATERIAL}")
    assert result.status == schema.FILED

    page_path, sha = result.result_ref.rsplit("@", 1)
    filed_page = support.read_filed_page(env.repo, sha, page_path)
    assert "status: developing" in filed_page
    assert "canonical" not in filed_page


def test_adversarial_cat7_forged_server_owned_fields_are_replaced_on_the_filed_page(rig,
                                                                                   clean_queue):
    """The queue-level cases prove the declaration is FLAGGED on arrival, which says nothing about
    what the page ends up carrying. This is the other end.

    All five fields at once, which is also the shape an attacker would send.
    """
    env, deps = rig
    submitter = "real.submitter@stigmergy.test"
    _, result = _file(clean_queue, deps, f"DOUBLE:forge\n{ACME_MATERIAL}", submitted_by=submitter)
    assert result.status == schema.FILED

    page_path, sha = result.result_ref.rsplit("@", 1)
    filed_page = support.read_filed_page(env.repo, sha, page_path)
    # every forged value the double drafted is GONE
    assert "someone.else@example.com" not in filed_page
    assert "leadership" not in filed_page
    assert "deadbeef" not in filed_page
    assert "owner:" not in filed_page                      # stripped entirely, never rewritten
    # ... and replaced by the server's own values
    assert f"submitted_by: {submitter}" in filed_page
    assert "status: developing" in filed_page
    # The forged `verification: verified` is STRIPPED like every other server-owned key, and
    # nothing re-stamps it: no verdict is computed anywhere, so the page carries no `verification`
    # line at all and the absence can be asserted exactly.
    assert "verification:" not in filed_page


class _QuotedKeyForgeAgent:
    """Wraps the double's `DOUBLE:forge` directive and re-spells the `owner` key it drafts with a
    QUOTED key (`"owner": "..."`) rather than the bare one the double itself always writes.

    This is the exact shape `gates.gate_frontmatter`'s own docstring names: `stamp_server_fields`
    used to rewrite server-owned fields with a line-based matcher that read bare keys only, so a
    quoted key survived the strip untouched and PyYAML then read it as a real `owner` —
    "precisely the thing the module docstring claims is impossible" until `gate_frontmatter` was
    added as a post-condition to catch it downstream. The hole is now closed at its actual source
    as well: `page._match_key` (and therefore `_strip_keys`) reads all three key spellings a page
    can declare, so the quoted line is stripped during the STAMP itself, the same as the bare one
    always was — `gate_frontmatter`'s parser-based check remains as a second, independent
    post-condition (see its own docstring) but no longer has anything to catch here. Nothing drives
    this shape through the double's own directives, so without this wrapper the fix has no
    regression test at all.
    """

    def __init__(self, inner):
        self.inner = inner
        # The declared port member, copied from what this wraps. Plain attribute
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
            full = os.path.join(kwargs["worktree"], run.outcome.page_path)
            with open(full, encoding="utf-8") as f:
                text = f.read()
            assert "owner: someone.else" in text, "the double's forge shape moved; update this test"
            with open(full, "w", encoding="utf-8") as f:
                f.write(text.replace("owner: someone.else", '"owner": "someone.else"'))
        return run


def test_adversarial_cat7_a_quoted_key_owner_forgery_never_reaches_a_filed_page(rig, clean_queue):
    """The quoted-key spelling is stripped by `stamp_server_fields` itself, exactly like the bare
    spelling — so the page files SUCCESSFULLY, without any `owner` field at all, on the FIRST pass,
    rather than surviving to a gate veto and a `failed` result. The property this test's name
    promises ("never reaches a filed page") is asserted the same way the bare-key sibling above
    does: on the committed page's actual bytes."""
    import dataclasses
    env, base_deps = rig
    deps = dataclasses.replace(base_deps, agent=_QuotedKeyForgeAgent(base_deps.agent))

    item, result = _file(clean_queue, deps, f"DOUBLE:forge\n{ACME_MATERIAL}")

    assert result.status == schema.FILED
    page_path, sha = result.result_ref.rsplit("@", 1)
    filed_page = support.read_filed_page(env.repo, sha, page_path)
    assert "owner:" not in filed_page
    assert "someone.else" not in filed_page

    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == schema.FILED


class _QuotedKeyEntityForgeAgent:
    """Wraps the double's `DOUBLE:forge` directive and INSERTS a quoted `"entity": ["evil"]` line
    into the drafted page — `forge` does not draft an `entity:` line at all today, so this drives
    the shape the quoted-key defect needs: a capture-declared `entity:` under a quoted key, landing
    beside the server's own line.
    """

    def __init__(self, inner):
        self.inner = inner
        # The declared port member, copied from what this wraps. Plain attribute
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
            full = os.path.join(kwargs["worktree"], run.outcome.page_path)
            with open(full, encoding="utf-8") as f:
                text = f.read()
            assert "sources: []\n" in text, "the double's draft shape moved; update this test"
            with open(full, "w", encoding="utf-8") as f:
                f.write(text.replace("sources: []\n", 'sources: []\n"entity": ["evil"]\n', 1))
        return run


def test_adversarial_cat7_a_quoted_key_entity_forgery_never_reaches_a_filed_page(rig, clean_queue):
    """The quoted-key variant: a capture-declared `entity:` under ANY of the three key spellings
    must have that value deleted and rewritten, and the declared value ("evil") must appear nowhere
    on the page — never merely be overridden by a same-spelling duplicate the way an incidental
    last-key-wins reading would allow."""
    import dataclasses
    env, base_deps = rig
    deps = dataclasses.replace(base_deps, agent=_QuotedKeyEntityForgeAgent(base_deps.agent))

    item, result = _file(clean_queue, deps, f"DOUBLE:forge\n{ACME_MATERIAL}")

    assert result.status == schema.FILED
    page_path, sha = result.result_ref.rsplit("@", 1)
    filed_page = support.read_filed_page(env.repo, sha, page_path)
    assert "evil" not in filed_page
    assert filed_page.count("entity:") == 1                # one declaration survives, not two

    row = queue.get_submission_trace(clean_queue, item["id"])
    assert row["status"] == schema.FILED


def test_adversarial_cat7_a_forged_submitted_by_never_reaches_the_commit_trailer_either(
        rig, clean_queue):
    """The page is not the only surface attribution lands on: `git log` alone must answer who asked
    for a page, so the `Submitted-by:` trailer is the server's value too. A forged trailer would make
    the git substrate — the whole point of which is that the audit is already done — lie."""
    env, deps = rig
    submitter = "real.submitter@stigmergy.test"
    _, result = _file(clean_queue, deps, f"DOUBLE:forge\n{ACME_MATERIAL}", submitted_by=submitter)

    _, sha = result.result_ref.rsplit("@", 1)
    message = gitcmd.run("log", "-1", "--format=%B", sha, cwd=env.repo).stdout
    assert f"Submitted-by: {submitter}" in message
    assert "someone.else@example.com" not in message


# ── cat. 1: the capture tries to steer the librarian ─────────────────────────────────────────────
# The assertion that matters most here is the NEGATIVE one: the finding names a category from a
# fixed set and never quotes the instruction back, because a report that reproduces the payload is
# a second copy of the injection delivered to a human.
def test_adversarial_cat1_material_instructing_it_to_file_as_canonical_is_not_obeyed(rig,
                                                                                    clean_queue):
    """The material *asks in prose* to be filed as canonical — no frontmatter, no directive — and
    the page still comes out `developing`, with the attempt recorded as a category. This is the
    pair of the cat. 7 case above: same goal, one through the document's metadata and one through
    its prose."""
    env, deps = rig

    _, result = _file(clean_queue, deps,
                      f"{ACME_MATERIAL} {payloads.STEER_DECLARE_CANONICAL}")

    assert result.status == schema.FILED
    assert any("declare-canonical" in f for f in result.findings)

    page_path, sha = result.result_ref.rsplit("@", 1)
    filed_page = support.read_filed_page(env.repo, sha, page_path)
    assert "status: developing" in filed_page
    assert "status: canonical" not in filed_page


def test_adversarial_cat1_a_credential_request_files_ordinary_with_a_recorded_finding(rig,
                                                                                     clean_queue):
    """Nothing in the material tripped a veto, so refusing would punish a submitter for a sentence.
    Filed as ordinary content, with the attempt on the record."""
    _, deps = rig

    _, result = _file(clean_queue, deps, f"{ACME_MATERIAL} {payloads.STEER_REVEAL_CREDENTIALS}")

    assert result.status == schema.FILED
    assert any("reveal-credentials" in f for f in result.findings)


def test_adversarial_cat1_steering_that_also_trips_a_veto_is_rejected_never_obeyed(rig, clean_queue):
    """When the attempt actually reaches a write outside the lane, the diff refuses it and the
    submitter is told what in their capture caused it — content-actionable, because something in the
    material really did."""
    env, deps = rig
    before = support.branch_sha(env.bare)

    _, result = _file(clean_queue, deps,
                      f"DOUBLE:escape\n{ACME_MATERIAL} {payloads.STEER_WRITE_OUTSIDE_LANE}")

    assert result.status == schema.REJECTED
    assert "write-outside-lane" in json.dumps(result.report)
    assert support.branch_sha(env.bare) == before           # no commit at all
    # The SABOTAGE'S OWN target, asserted rather than a neighbour's: `double._write_unconfined`
    # writes the identity ROSTER for this payload — the sharpest thing an out-of-lane write could
    # name, since an agent that could edit it could grant itself an audience — and the property is
    # that the write never reached the repository's history — asserted over EVERY commit the bare
    # remote holds, not only the tip, so a landed-then-reverted write would still fail it. The
    # fixture repo carries no roster of its own, which is what makes "not in history" a clean
    # assertion rather than a comparison of two blobs of a file the sabotage never touches.
    assert "ops/identities.json" not in support.all_ever_committed_paths(env.bare)


@pytest.mark.parametrize("payload", payloads.STEERING_PAYLOADS,
                         ids=["declare_canonical", "write_outside_lane", "reveal_credentials"])
def test_adversarial_cat1_no_report_ever_quotes_the_planted_instruction_back(rig, clean_queue,
                                                                            payload):
    """Asserted the only way the property can be: by scanning the rendered report for a substring
    of the planted text. Every category, whatever the terminal state, checked against BOTH
    renderings — the structured report a client reads and the prose a terminal prints."""
    _, deps = rig

    _, result = _file(clean_queue, deps, f"{ACME_MATERIAL} {payload}")

    rendered = json.dumps(result.report) + report_module.render_prose(result.report)
    assert payload not in rendered
    # and no distinctive fragment of it either — a report that quoted half the instruction would be
    # half a second copy
    for fragment in payload.split(" and "):
        assert fragment.strip() not in rendered


def test_adversarial_cat1_the_finding_names_a_category_from_the_fixed_set(rig, clean_queue):
    """A category the agent invented is dropped rather than echoed (`processing._injection_
    categories`), so the report's vocabulary is closed — which is what makes "never quotes the
    payload" a property rather than a hope."""
    _, deps = rig
    _, result = _file(clean_queue, deps, f"{ACME_MATERIAL} {payloads.STEER_REVEAL_CREDENTIALS}")

    named = [c for c in gates.INJECTION_CATEGORIES
             if any(c in finding for finding in result.findings)]
    assert named == ["reveal-credentials"]


def test_adversarial_cat1_ordinary_material_raises_no_steering_finding(rig, clean_queue):
    """The benign twin for this category specifically. An injection detector that fires on normal
    prose would attach a "tried to instruct the librarian" finding to somebody's honest capture — a
    small accusation, made automatically, in a report a colleague reads."""
    _, deps = rig
    _, result = _file(clean_queue, deps, ACME_MATERIAL)

    assert result.status == schema.FILED
    for category in gates.INJECTION_CATEGORIES:
        assert not any(category in finding for finding in result.findings)


# ── cat. 1, second mechanism: PAGE CONTENT shaped like git's own diff metadata ────────────────────
# A distinct adversarial family rather than a variant of the ones above: the payload does not
# address the model at all, it addresses the PARSER. git prefixes every content line with a single
# `+`/`-`, so a page line can be spelled to render as diff metadata, and the gates that classified
# diff lines by prefix believed it. Three gates were disabled at once by one line.
#
# This is the same failure CLASS as the NUL-byte blindness (a rendering assumption turning several
# gates off together), reached through a different mechanism — which is why these are permanent
# cases and not a note in a docstring. Each drives a real git diff over a page committed once and
# then modified, never a fabricated diff object.
_HUMAN_PAGE = ('---\ntype: note\ntitle: "Existing Note"\n'
               'related: ["[[Acme Corp]]", "[[Other Page]]"]\ntags: [note]\n---\n\n'
               '# Existing Note\n\n'
               'A paragraph a human wrote.\n'
               '--repo /srv/knowledge --branch main\n'
               '---\n'
               'related: ["[[Acme Corp]]"]\n'
               'The last human paragraph.\n')

_PAGE_REL = "wiki/notes/Existing Note.md"


def _diff_ctx(repo, rel: str, after: str):
    """Commit-then-modify one page and build the `GateContext` the gates really see."""
    path = os.path.join(repo, rel)
    with open(path, "w", encoding="utf-8") as f:
        f.write(after)
    return gates.GateContext(
        worktree=repo, entries=gitcmd.diff_entries(repo), added=gitcmd.added_lines(repo),
        material="", outcome=None, registry=None)


def _seeded_repo(tmp_path, rel: str, text: str) -> str:
    repo = str(tmp_path / "repo")
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    full = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "seed", cwd=repo,
               env={"GIT_AUTHOR_NAME": "h", "GIT_AUTHOR_EMAIL": "h@t.test",
                    "GIT_COMMITTER_NAME": "h", "GIT_COMMITTER_EMAIL": "h@t.test"})
    return repo


def test_adversarial_cat1_body_lines_shaped_like_diff_metadata_cannot_hide_a_deletion(tmp_path):
    """Both halves of the payload at once. The page loses three human-authored lines:
    `--repo …` (which renders as `---repo …` and was skipped as a header), a `---` thematic rule (same
    shape), and a body line spelled `related: [...]` (which was handed to the superset proof, which
    read the FRONTMATTER's list and found it a superset — the proof was position-blind).

    `gate_body_rewrite` compares blobs now, so no rendering decision of git's is involved."""
    repo = _seeded_repo(tmp_path, _PAGE_REL, _HUMAN_PAGE)
    ctx = _diff_ctx(repo, _PAGE_REL,
                    '---\ntype: note\ntitle: "Existing Note"\n'
                    'related: ["[[Acme Corp]]", "[[Other Page]]"]\ntags: [note]\n---\n\n'
                    '# Existing Note\n\n'
                    'A paragraph a human wrote.\n'
                    'The last human paragraph.\n')

    findings = gates.gate_body_rewrite(ctx)

    assert [f.code for f in findings] == ["body-rewrite"]
    assert findings[0].locator == _PAGE_REL


def test_adversarial_cat1_a_duplicate_related_key_cannot_lose_its_second_declaration(tmp_path):
    """The remaining hole in the superset proof: `page.related_links` reads the FIRST top-level
    `related:` block, so when a page declares the key twice and the first is a strict superset of the
    second, deleting the second line proved out as growth. A human's declaration disappeared."""
    before = ('---\ntype: note\ntitle: "Existing Note"\n'
              'related: ["[[Acme Corp]]", "[[Other Page]]"]\n'
              'related: ["[[Acme Corp]]"]\ntags: [note]\n---\n\n'
              '# Existing Note\n\nA paragraph a human wrote.\n')
    repo = _seeded_repo(tmp_path, _PAGE_REL, before)
    ctx = _diff_ctx(repo, _PAGE_REL,
                    '---\ntype: note\ntitle: "Existing Note"\n'
                    'related: ["[[Acme Corp]]", "[[Other Page]]"]\ntags: [note]\n---\n\n'
                    '# Existing Note\n\nA paragraph a human wrote.\n')

    assert [f.code for f in gates.gate_body_rewrite(ctx)] == ["body-rewrite"]


def test_adversarial_cat1_a_declared_rewrite_is_the_benign_twin_and_passes_clean(tmp_path):
    """The benign twin for the gate the two attacks above drive.

    OLD BEHAVIOUR: the shape that passed here was an ADDITIVE edit — the `related:` line replaced
    with a longer list plus an appended callout — which a filing account declared and code
    performed. That mechanism is gone, and with it the shape: a modification nobody declared is now
    refused with no shape admitted at all. What a capture may still do to a page that exists is
    DECLARE that it brings it up to date, and this is that road — the same page, the same gate, the
    caller naming the path in `rewrites_allowed`, and no veto.
    """
    repo = _seeded_repo(tmp_path, _PAGE_REL, _HUMAN_PAGE)
    ctx = _diff_ctx(repo, _PAGE_REL,
                    _HUMAN_PAGE.replace("A paragraph a human wrote.",
                                        "A paragraph a later capture brought up to date.", 1))
    ctx.rewrites_allowed = frozenset({_PAGE_REL})

    assert gates.gate_body_rewrite(ctx) == []



def test_adversarial_cat1_added_lines_counts_hunks_rather_than_matching_prefixes(tmp_path):
    """The parser's own contract, driven by a diff carrying every shape that used to confuse it:
    `++ b/…`, a bare `+++`, `--flag` and `---`. Each must come back as CONTENT, with its real path
    and its real new-file line number."""
    repo = _seeded_repo(tmp_path, _PAGE_REL, _HUMAN_PAGE)
    payload = ["++ b/wiki/notes/Nowhere.md", "+++", "--flag value", "---", "@@ not a hunk"]
    with open(os.path.join(repo, _PAGE_REL), "w", encoding="utf-8") as f:
        f.write(_HUMAN_PAGE.rstrip("\n") + "\n" + "\n".join(payload) + "\n")

    added = gitcmd.added_lines(repo)

    assert [text for _, _, text in added] == payload
    assert {path for path, _, _ in added} == {_PAGE_REL}
    # contiguous new-file line numbers, so a refusal names a location a human can open
    numbers = [n for _, n, _ in added]
    assert numbers == list(range(numbers[0], numbers[0] + len(payload)))

# ── removed with ingest-time figure verification, and named rather than dropped in silence ──────
# A check that stops running must be impossible to miss, so what left is listed here instead of
# vanishing from the file. The tests below drove a HALLUCINATED FIGURE through the fast lane and
# asserted that a figure-verification gate vetoed it, that one corrective retry recovered it, or
# that the resulting report carried the right verdict. That gate is gone:
# ingest-time figure verification went with
# the trust layer, deliberately, and the accepted consequence is stated there — **an invented
# figure CAN sit on a page.** The reader's protection is the verbatim source one click away, the
# gardener, and `answer.verify_answer` at query time.
#
# So these are removed, not repaired: their subject no longer exists, and a test rewritten to
# assert the opposite would be measuring a decision, not a mechanism. What they ALSO covered
# incidentally — atomicity, the once-directive, the steering veto — is covered by the remaining
# tests in this file, which use a veto that still exists (zone, anchoring, secrets) to produce
# the same refusal shape.
#
# Removed: `test_adversarial_cat1_a_forged_file_header_in_page_content_cannot_move_the_content_gates`
