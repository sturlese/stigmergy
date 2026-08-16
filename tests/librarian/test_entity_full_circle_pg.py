"""The entity full circle, keyless, with the double declaring the newborn entity: an unregistered
entity is asked about, the steward approves it through `stigmergy-entities` from a SEPARATE clone
(their own laptop, never the worker's checkout), and the ORIGINATING CAPTURE re-files anchored to
the newborn entity — all
against the local bare remote, with no restart of the worker (`deps`/`rig` built once), which is
what actually exercises fetch-before-claim rather than merely asserting it by name.

Lives beside `test_human_loop_pg.py` rather than under `tests/entities/`, for the same reason that
file does: the fixtures this needs (`rig`, `clean_queue`, a real bare remote + double agent) are
`tests/librarian/conftest.py`'s, and the flow under test spans `capture` + `server.service` +
`librarian` + `entities` — a seam only this directory's fixtures can build directly. `tests/
entities/` owns everything BELOW the queue: `birth`,
`generator`, `clone`, `situations`, `cli`, `errors` in isolation. This is the one integration test
that proves they compose.

**Why the double resolves through a REPLY, not by re-reading the material.** A reply can only
resolve to an *existing* entity — "it's new" always escalates; the submitter never mints. So the
real flow is: ask -> the submitter's reply names the
(still unregistered) entity -> one ask spent, parks in `triage` -> the steward approves it -> the
capture is requeued -> the SAME reply, now resolvable against the freshly-fetched, newly-approved
registry, is what the next pass anchors on (`double.DoubleAgent._resolve_reply` reads `item["reply"]`
on every delivery, never only the one right after it was given — `processing.py`'s own
`reply=item.get("reply") or ""`). No second reply, no engineer touching the database.
"""
import subprocess

from stigmergy.capture import decisions, queue, schema
from stigmergy.entities import cli as entities_cli
from stigmergy.entities import generator
from stigmergy.librarian import worker
from stigmergy.review_kinds import KIND_ENTITY_PROPOSAL
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.entities import conftest as fx
from tests.librarian import support

SUBMITTER = support.DEFAULT_SUBMITTER
STEWARD_LABEL = "steward <steward@stigmergy.test>"

UNREGISTERED_NAME = "Globex Corp"
MATERIAL = f"DOUBLE:triage-entity={UNREGISTERED_NAME}\nA memo about the Globex Corp partnership.\n"


def _service(conn, identity, *, audiences=None) -> BrainService:
    settings = Settings(identity=identity, identities_path="x")
    return BrainService(settings, conn, embedder=None, audiences=audiences, identity=identity)


def _row(conn, submission_id) -> dict:
    return queue.get_submission_trace(conn, submission_id)


def _fix_preexisting_fixture_drift(env) -> None:
    """`tests/librarian/fixtures/repo/` predates the registry-consistency rule: its `Acme Corp`
    entry is hand-authored under the curated id `acme`, while the id a
    derived-view generator computes from the PAGE TITLE is `acme-corp` (`slugify` keeps "corp";
    only `normalize`, the ALIAS matcher, strips it) — the alias `Acme` is what resolves to `acme`,
    not the entity's own name. That is invisible to every OTHER librarian test (`Registry.
    canonical_id` resolves through `by_alias` regardless of which id owns the entry) and entirely
    real to `entities.cli._refuse_drift`, which — correctly — refuses to mint ANY new entity into
    a repo whose pages and registry already disagree, naming exactly this.

    So the fixture is not wrong for the suite it was built for, and this is not a production
    defect: it is a repo that has never been run through the generator, fixed here the same
    way a real steward would (`generator.regenerate`, reviewed and committed) before this test's
    own scenario (approving a DIFFERENT, new entity) begins. Landed on `env.bare` directly rather
    than through a steward clone: it is fixture upkeep, not part of the flow under test, and
    it never touches `tests/librarian/fixtures/repo/` on disk — only this test's own throwaway
    `tmp_path` checkout and its own throwaway bare remote.
    """
    outcome = generator.regenerate(env.repo)
    assert outcome.changed, "the fixture's own legacy drift is gone — this shim is no longer needed"
    subprocess.run(["git", "add", "-A"], cwd=env.repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--quiet", "-m",
                   "chore(registry): regenerate the derived registry view"],
                  cwd=env.repo, check=True, capture_output=True,
                  env={**__import__("os").environ, **support._COMMIT_ENV})
    subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=env.repo, check=True,
                  capture_output=True)


def test_full_circle_approve_requeue_refile_anchored_no_restart(rig, clean_queue, tmp_path):
    env, deps = rig     # the WORKER's own checkout — never where the steward approves from
    _fix_preexisting_fixture_drift(env)

    # ── phase 1: ask ─────────────────────────────────────────────────────────────────────────
    ack = support.submit(clean_queue, deps, MATERIAL)
    item, asked = worker.process_next(clean_queue, deps)
    assert asked.status == schema.NEEDS_INPUT
    assert UNREGISTERED_NAME in asked.report["summary"]

    # ── phase 2: the submitter's reply names the (still unregistered) entity — one ask spent ───
    svc = _service(clean_queue, SUBMITTER, audiences=set())
    reply_result = svc.reply(ack["id"], UNREGISTERED_NAME)
    assert reply_result["status"] == schema.QUEUED

    _, parked = worker.process_next(clean_queue, deps)
    assert parked.status == schema.TRIAGE, parked.report.get("summary")
    row = _row(clean_queue, ack["id"])
    assert row["asked_at"] is not None

    # ── phase 3: the steward — from their OWN clone of the SAME bare remote, never the worker's
    # checkout — sees it as a pending entity situation and approves it. ────────────────────────
    steward_repo = fx.clone_of(env.bare, str(tmp_path / "steward-clone"),
                               name="Jordan Reyes", email="steward@stigmergy.test")
    canonical_id = generator.canonical_id_for(UNREGISTERED_NAME)

    rc = entities_cli.main([
        "--repo", steward_repo, "--branch", "main", "--dsn", _dsn(clean_queue),
        "approve", str(ack["id"]), "--id", canonical_id, "--name", UNREGISTERED_NAME,
        "--type", "organization", "--requeue", "--by", STEWARD_LABEL,
        "--today", "2026-07-27"])
    assert rc == 0

    # one commit, on the remote, signed by the STEWARD (never the App, never the worker).
    import subprocess
    trailer = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=env.bare,
                             capture_output=True, text=True, check=True).stdout.strip()
    assert trailer == "Jordan Reyes <steward@stigmergy.test>"
    page_on_remote = subprocess.run(
        ["git", "show", f"main:wiki/entities/{UNREGISTERED_NAME}.md"], cwd=env.bare,
        capture_output=True, text=True, check=True).stdout
    assert f'title: "{UNREGISTERED_NAME}"' in page_on_remote
    registry_on_remote = subprocess.run(
        ["git", "show", "main:ops/entity-registry.json"], cwd=env.bare,
        capture_output=True, text=True, check=True).stdout
    assert canonical_id in registry_on_remote

    # `--requeue` landed AFTER the push (module docstring's own correctness property): the row is
    # claimable again right now.
    row_after_approve = _row(clean_queue, ack["id"])
    assert row_after_approve["status"] == schema.QUEUED

    # ── and the governance ledger saw it. ───────────────────────────────────────────────────────
    # OLD BEHAVIOUR: this door wrote no `review_decisions` row, because `stigmergy.entities` may
    # not import `stigmergy.server`, where the writer lived. So a CLI approval was invisible to the
    # two surfaces that read this table as if it were complete — the console's Activity view and
    # the digest's governance count — and neither could tell "no CLI approvals happened" from
    # "CLI approvals are not counted" (issue #51). The writer moved BELOW both packages instead.
    #
    # Asserted HERE, on the one test that drives the real CLI against a real database and a real
    # remote, rather than against a stub: the whole claim is that a row lands beside a push that
    # actually happened.
    with clean_queue.cursor() as cur:
        cur.execute("SELECT item_kind, verdict, actor, extra FROM review_decisions "
                    "WHERE item_id = %s", (str(ack["id"]),))
        ledger = cur.fetchall()
    assert len(ledger) == 1, "the CLI approve wrote no ledger row, or wrote more than one"
    item_kind, verdict, actor, extra = ledger[0]
    assert (item_kind, verdict) == (KIND_ENTITY_PROPOSAL, decisions.APPROVE)
    # The same identity the commit is authored with — one approval, two records, one person.
    assert actor == STEWARD_LABEL
    assert extra["entity_id"] == canonical_id
    assert extra["source"] == "cli", "the row does not say which door decided it"
    assert extra["commit"] == subprocess.run(
        ["git", "rev-parse", "main"], cwd=env.bare, capture_output=True, text=True,
        check=True).stdout.strip(), "the recorded commit is not the one that landed on the remote"

    # ── phase 4: the SAME worker (no restart — `deps`/`rig` were built once, at the top of this
    # test), re-files the ORIGINATING capture, anchored to the newborn entity. This is what
    # actually exercises fetch-before-claim: `deps.repo` never saw the steward's push directly —
    # only `gitcmd.base_ref`'s own fetch, run again on this claim, does. ──────────────────────
    _, refiled = worker.process_next(clean_queue, deps)
    assert refiled.status == schema.FILED, refiled.report.get("summary")
    assert UNREGISTERED_NAME in refiled.report["anchored_to"]
    page_path, sha = refiled.result_ref.rsplit("@", 1)
    filed_text = support.read_filed_page(env.repo, sha, page_path)
    assert f"[[{UNREGISTERED_NAME}]]" in filed_text

    # `brain_submissions` — the submitter's own view — shows the whole journey: question, reply,
    # and now a filed report, all on the one row.
    view = svc.submissions(limit=50)
    final_row = next(r for r in view["submissions"] if r["id"] == ack["id"])
    assert final_row["reply"] == UNREGISTERED_NAME
    assert final_row["report"]["status"] == schema.FILED


def test_a_cli_reject_is_recorded_in_the_ledger_too(rig, clean_queue, tmp_path):
    """The other verdict on the same door. Recording only `approve` would close half the gap and
    leave "who decided this identity" answering from different tables depending on the answer —
    the admin console already records its own Reject for exactly that reason.

    Driven through the real CLI against the real queue, like the approve above; the mint itself is
    what a reject does NOT do, so no clone is involved.
    """
    env, deps = rig
    _fix_preexisting_fixture_drift(env)

    ack = support.submit(clean_queue, deps, MATERIAL)
    worker.process_next(clean_queue, deps)                      # asks
    _service(clean_queue, SUBMITTER, audiences=set()).reply(ack["id"], UNREGISTERED_NAME)
    _, parked = worker.process_next(clean_queue, deps)          # parks in triage
    assert parked.status == schema.TRIAGE, parked.report.get("summary")

    rc = entities_cli.main([
        "--repo", env.repo, "--dsn", _dsn(clean_queue),
        "reject", str(ack["id"]), "--reason", "we already track this under Acme Corp",
        "--by", STEWARD_LABEL])
    assert rc == 0

    with clean_queue.cursor() as cur:
        cur.execute("SELECT item_kind, verdict, actor, notes, extra FROM review_decisions "
                    "WHERE item_id = %s", (str(ack["id"]),))
        ledger = cur.fetchall()
    assert len(ledger) == 1
    item_kind, verdict, actor, notes, extra = ledger[0]
    assert (item_kind, verdict, actor) == (KIND_ENTITY_PROPOSAL, decisions.REJECT, STEWARD_LABEL)
    assert notes == "we already track this under Acme Corp"
    assert extra["source"] == "cli"
    # And the capture itself really was rejected — the ledger row is a record OF something, not a
    # substitute for it.
    assert _row(clean_queue, ack["id"])["status"] == schema.REJECTED


def _dsn(conn) -> str:
    """The DSN this test's own connection is open against — `entities.cli.main` opens its OWN
    connection (it is a separate process in reality), so it needs the same test database named
    explicitly rather than inheriting `conn`."""
    from tests import testdb
    return testdb.dsn()
