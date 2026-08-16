"""AdminService over the real queue/tables (stigmergy_test). The dispositions land through the
SAME library seams the CLIs use, so what these prove is parity, not a parallel implementation."""
import asyncio
import os

import pytest

from stigmergy.admin import schema as admin_schema
from stigmergy.admin.service import (
    CRON_WORKFLOWS,
    AdminBadRequest,
    AdminNotFound,
    AdminRefused,
    AdminService,
    worker_visibility_timeout_s,
)
from stigmergy.capture import dispositions, ops, queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.gardener.schema import JOB_NAME as GARDENER_JOB
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.gardener.store import insert_findings
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian import gitcmd
from stigmergy.server.settings import Settings
from tests.admin.conftest import park, submit_one, unresolved_entity_report


@pytest.fixture()
def service(conn, server_settings, admin_settings):
    return AdminService(conn, server_settings=server_settings, admin_settings=admin_settings)


@pytest.fixture()
def gh_service(conn, server_settings, admin_settings, fake_gateway):
    return AdminService(conn, server_settings=server_settings, admin_settings=admin_settings,
                        gateway=fake_gateway)


def _actions(conn):
    return admin_schema.recent_actions(conn, limit=20)


# ── queue reads ───────────────────────────────────────────────────────────────────────────────
def test_queue_list_carries_the_cli_facts(conn, service):
    ack = submit_one(conn, material="hola desde la consola " + os.urandom(4).hex())
    queued = service.queue_list()
    assert queued["counts"]["queued"] == 1
    pending = queued["submissions"][0]
    assert pending["id"] == ack["id"] and pending["status"] == "queued"
    assert pending["submitted_by"] == "steward@example.com"
    # A PENDING row's material is withheld by design — the secrets/PII gate has not run yet, so
    # the queue explains the empty excerpt with its own sentence (`schema.withheld_reason`).
    assert pending["excerpt"] == "" and pending["withheld_reason"]
    park(conn, ack["id"])
    parked = service.queue_list()["submissions"][0]
    assert "hola desde la consola" in parked["excerpt"], "a parked row's material IS readable"
    assert parked["waiting_on"] == "a steward" and parked["payload_purged"] is False


def test_a_needs_input_row_carries_the_reply_invocation(conn, service):
    ack = submit_one(conn)
    park(conn, ack["id"], status=capture_schema.NEEDS_INPUT,
         error="which Acme is this?\nanswer with brain_reply(...)")
    row = service.queue_list()["submissions"][0]
    assert row["status"] == "needs_input"
    assert row["reply_invocation"] == capture_schema.reply_invocation(ack["id"])
    assert row["waiting_on"] == "steward@example.com"


def test_queue_show_returns_the_whole_trace_and_404s_on_nothing(conn, service):
    ack = submit_one(conn)
    trace = service.queue_show(ack["id"])
    assert trace["id"] == ack["id"] and trace["events"] == []
    assert trace["queue_wait_ms"] is None
    with pytest.raises(AdminNotFound):
        service.queue_show(999_999)


def test_a_report_that_is_not_an_object_is_served_unshaped_not_a_500(conn, service):
    """OLD BEHAVIOUR: `AttributeError` out of `_traced_fields`, which the routes turn into a 500 —
    the whole detail view lost, for one column's shape.

    `report` is JSONB with no CHECK constraint behind it, so "an object" is a convention this
    codebase's writers keep, never something the column enforces: a hand-run `UPDATE`, a migration
    or any other writer can leave a scalar there. `_traced_fields` dict-comprehended `report`
    unconditionally while its sibling `hints` line right below already guarded with `isinstance` —
    the same tolerance, one line apart, applied to only one of them. Passed through UNSHAPED is
    deliberate: sanitizing runs per key over an object, and there are no keys here; the console
    renders what is there rather than hiding the row.
    """
    ack = submit_one(conn)
    with conn.cursor() as cur:   # the shape no writer here produces, and nothing stops arriving
        cur.execute("UPDATE capture_queue SET report = %s::jsonb WHERE id = %s", ('"oops"', ack["id"]))

    trace = service.queue_show(ack["id"])

    assert trace["id"] == ack["id"]
    assert trace["report"] == "oops"
    assert service.queue_list()["submissions"][0]["id"] == ack["id"], "the list view survives too"


def test_untrusted_text_reaches_the_wire_without_control_characters(conn, service):
    """The server half: ANSI escapes die here; the literal `<script>` SURVIVES as text — HTML
    inertness is the client's job (textContent), not server-side mangling."""
    ack = submit_one(conn, material="\x1b[31mred\x1b[0m <script>alert(1)</script> body")
    park(conn, ack["id"])   # a pending row withholds its material; a parked one shows it
    row = service.queue_list()["submissions"][0]
    assert row["id"] == ack["id"]
    assert "\x1b" not in row["excerpt"]
    assert "<script>" in row["excerpt"]


# ── the drain ─────────────────────────────────────────────────────────────────────────────────
def test_requeue_leaves_attempts_alone_and_records_the_action(conn, service):
    ack = submit_one(conn)
    park(conn, ack["id"])
    result = service.queue_requeue(ack["id"], actor="steward", note="try again")
    assert result["attempts"] == 1, "requeue must never touch attempts"
    assert queue.current_status(conn, ack["id"]) == capture_schema.QUEUED
    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == \
        ("steward", "queue.requeue", "ok")


def test_resolve_without_a_pointer_warns_and_with_one_does_not(conn, service):
    first = submit_one(conn)
    park(conn, first["id"])
    warned = service.queue_resolve(first["id"], actor="steward", note="handled by hand")
    assert "no pointer to where the material went" in warned["warning"]
    second = submit_one(conn)
    park(conn, second["id"])
    clean = service.queue_resolve(second["id"], actor="steward", note="filed it",
                                  page="wiki/acme.md", commit="abc123")
    assert clean["warning"] == ""


def test_a_disposition_on_an_unparked_row_is_the_librarys_own_refusal(conn, service):
    ack = submit_one(conn)   # queued, never parked
    with pytest.raises(AdminRefused):
        service.queue_reject(ack["id"], actor="steward", reason="nope")
    recorded = _actions(conn)[0]
    assert recorded["outcome"] == "error" and recorded["error_class"]


def test_the_submitter_visible_note_is_cleaned_below_the_console(conn, service):
    """The seam is `dispositions.clean`, below every surface — the console must inherit it, not
    re-remember it."""
    ack = submit_one(conn)
    park(conn, ack["id"])
    service.queue_reject(ack["id"], actor="steward", reason="\x1b[31mdeclined\x1b[0m for cause")
    trace = service.queue_show(ack["id"])
    # The reason reaches the submitter through the report/`error` sentence `rejected_report`
    # composes — `dispositions.clean` ran below the console, so the ESC control byte is gone and
    # what remains of the escape sequence is inert text (`[31m`), not a terminal instruction.
    assert "for cause" in trace["error"]
    assert "\x1b" not in trace["error"]


def test_reclaim_releases_an_expired_claim(conn, service):
    ack = submit_one(conn)
    queue.claim_next(conn, visibility_timeout_s=0)   # a lease that is expired at birth
    result = service.queue_reclaim(actor="steward", visibility_timeout_s=0)
    assert result["released"] == 1 and result["failed"] == 0
    assert queue.current_status(conn, ack["id"]) == capture_schema.QUEUED


def test_the_flagless_reclaim_leaves_a_claim_that_is_still_inside_the_workers_lease(conn, service, monkeypatch):
    """The benign twin the console's destructive path never had.

    OLD BEHAVIOUR: `queue_reclaim` with no explicit horizon fell back to
    `queue.DEFAULT_VISIBILITY_TIMEOUT_S` (300) while the worker's real lease is
    the worker's own lease (900 at the class default). Every capture held between those two numbers — the long
    agent items the 900s lease exists for — was requeued out from under a RUNNING worker by the
    ordinary Reclaim button, or failed outright once its attempts were spent. The read path in the
    same file already used the worker's number; only the write path guessed.

    The one test this had passed `visibility_timeout_s=0` explicitly, so the default it was
    measuring was never the default anyone clicks.
    """
    # A check must not depend on ambient state: this one compares against the DERIVED
    # lease, so a developer or CI runner exporting the deployment's own budget turned it
    # red (batch audit S3). The default-env case is the one this test is about.
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    ack = submit_one(conn)
    queue.claim_next(conn, visibility_timeout_s=worker_visibility_timeout_s())
    # Age the claim past the queue CLI's 300s default but well inside the worker's 900s lease.
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '400 seconds' "
                    "WHERE id = %s", (ack["id"],))

    result = service.queue_reclaim(actor="steward")

    assert result == {"released": 0, "failed": 0}, (
        "the console reclaimed a capture whose worker is still inside its lease")
    assert queue.current_status(conn, ack["id"]) == capture_schema.CLAIMED


def test_reclaim_still_releases_a_claim_that_outlived_the_workers_lease(conn, service, monkeypatch):
    """The other edge of the same boundary: past 900s the worker really is presumed dead, and the
    flagless console action must still recover the row. Moving the horizon must not turn Reclaim
    into a no-op."""
    # A check must not depend on ambient state: this one compares against the DERIVED
    # lease, so a developer or CI runner exporting the deployment's own budget turned it
    # red (batch audit S3). The default-env case is the one this test is about.
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    ack = submit_one(conn)
    queue.claim_next(conn, visibility_timeout_s=worker_visibility_timeout_s())
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1000 seconds' "
                    "WHERE id = %s", (ack["id"],))

    result = service.queue_reclaim(actor="steward")

    assert result["released"] == 1 and result["failed"] == 0
    assert queue.current_status(conn, ack["id"]) == capture_schema.QUEUED


# ── the flagless reclaim horizon must DERIVE from the env, like the worker's real lease ────────────
# The console USED to read `librarian_config.DEFAULT_VISIBILITY_TIMEOUT_S` — the librarian's
# CLASS default (900s), frozen at import time. The deployed worker's real lease
# derives from `$STIGMERGY_LIBRARIAN_TIMEOUT_S` (`librarian.config.Settings.from_args`; staging's
# 600s agent budget -> 1500s). The two tests above never set that env var, so they cannot tell the
# two numbers apart — both pass whether the console reads 900 or the derived value, as long as
# nobody has STIGMERGY_LIBRARIAN_TIMEOUT_S exported. These do set it, explicitly, to prove the
# horizon moves with it.
def test_reclaim_default_horizon_derives_from_the_env_var_and_does_not_release_a_capture_still_within_it(
        conn, service, monkeypatch):
    """OLD BEHAVIOUR: the flagless reclaim swept against the CLASS default (900s) regardless of
    `$STIGMERGY_LIBRARIAN_TIMEOUT_S`, so a capture aged 1000s — inside the 1500s lease staging's
    worker actually holds — gets swept anyway. A wasteful redelivery of an item a healthy worker
    still holds."""
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    ack = submit_one(conn)
    queue.claim_next(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1000 seconds' "
                    "WHERE id = %s", (ack["id"],))

    result = service.queue_reclaim(actor="steward")

    assert result == {"released": 0, "failed": 0}, (
        "the flagless reclaim swept a capture still inside the worker's real, env-derived 1500s "
        "lease — it used the 900s class default instead")
    assert queue.current_status(conn, ack["id"]) == capture_schema.CLAIMED
    recorded = _actions(conn)[0]
    assert recorded["args"]["visibility_timeout_s"] == 1500, (
        "admin_actions must record the horizon actually swept against, not the class default")


def test_reclaim_default_horizon_falls_back_to_the_class_default_with_no_env_var(
        conn, service, monkeypatch):
    """Benign twin: with no env var, the flagless reclaim must still release a capture that has
    genuinely outlived the class-default 900s lease — deriving the horizon must not turn Reclaim
    into a no-op for the ordinary, unconfigured case. Complements
    `test_reclaim_still_releases_a_claim_that_outlived_the_workers_lease` above (which relies on
    the ambient environment simply never having set the var) with an explicit `delenv` and a check
    on the horizon `admin_actions` records, so this cannot pass by accident of whatever a
    developer's shell happens to export."""
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    ack = submit_one(conn)
    queue.claim_next(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1000 seconds' "
                    "WHERE id = %s", (ack["id"],))

    result = service.queue_reclaim(actor="steward")

    assert result == {"released": 1, "failed": 0}
    assert queue.current_status(conn, ack["id"]) == capture_schema.QUEUED
    recorded = _actions(conn)[0]
    assert recorded["args"]["visibility_timeout_s"] == 900


def test_purge_dry_run_changes_nothing_and_the_real_run_purges(conn, service):
    ack = submit_one(conn)
    item = queue.claim_next(conn)
    queue.finish(conn, ack["id"], status=capture_schema.REJECTED,
                 expected_attempts=item["attempts"])
    preview = service.queue_purge(actor="steward", older_than_days=0, dry_run=True)
    assert preview["purged"] == 1 and ack["id"] in preview["ids"]
    assert service.queue_show(ack["id"])["payload_purged"] is False
    assert all(a["action"] != "queue.purge" for a in _actions(conn)), \
        "a dry run is a preview, not a mutation — no admin_actions row"
    real = service.queue_purge(actor="steward", older_than_days=0, dry_run=False)
    assert real["purged"] == 1
    assert service.queue_show(ack["id"])["payload_purged"] is True
    assert _actions(conn)[0]["action"] == "queue.purge"


def test_a_bookkeeping_failure_never_fails_the_mutation(conn, service, monkeypatch):
    ack = submit_one(conn)
    park(conn, ack["id"])
    monkeypatch.setattr(admin_schema, "_INSERT", "INSERT INTO no_such_table VALUES (1)")
    result = service.queue_requeue(ack["id"], actor="steward", note="")
    assert result["id"] == ack["id"], "the work must land even when its bookkeeping cannot"
    assert queue.current_status(conn, ack["id"]) == capture_schema.QUEUED


def test_a_blank_actor_falls_back_to_the_configured_default(conn, service):
    ack = submit_one(conn)
    park(conn, ack["id"])
    service.queue_requeue(ack["id"], actor="   ", note="")
    assert _actions(conn)[0]["actor"] == "suite-default-actor"


def test_an_unknown_status_filter_is_a_bad_request(service):
    with pytest.raises(AdminBadRequest, match="unknown status"):
        service.queue_list(statuses=["bogus"])


# ── worker / overview ─────────────────────────────────────────────────────────────────────────
def test_worker_status_reads_the_lease_against_the_workers_own_numbers(conn, service):
    submit_one(conn)
    queue.claim_next(conn)
    status = service.worker_status()
    assert status["visibility_timeout_s"] == worker_visibility_timeout_s()
    row = status["in_flight"][0]
    assert row["lease_expired"] is False
    assert "within its lease" in row["verdict"]


# ── the console meter must DERIVE its lease, not default to the librarian's class
# constant ─────────────────────────────────────────────────────────────────────────────────────
# The console USED to resolve its lease ONCE, at import time, from the librarian's CLASS
# default (900s) — never from `$STIGMERGY_LIBRARIAN_TIMEOUT_S`. The deployed worker's REAL lease derives from that
# env var (`librarian.config.Settings.from_args`: staging's 600s agent budget -> 1500s). An item
# legitimately in flight between 900s and 1500s therefore reads "lease expired" on every one of
# these three readers, and `queue_reclaim`'s default horizon (tested separately, below the drain
# section) sweeps it — a wasteful redelivery of an item a healthy worker still holds.
def test_worker_status_visibility_timeout_derives_from_the_env_var(service, monkeypatch):
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    status = service.worker_status()
    assert status["visibility_timeout_s"] == 1500, (
        "worker_status() still reports the CLASS default (900) instead of the lease the deployed "
        "worker actually holds under STIGMERGY_LIBRARIAN_TIMEOUT_S=600 (2*600 + 120 + 180 = 1500)")


def test_meta_worker_visibility_timeout_derives_from_the_env_var(service, monkeypatch):
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    assert service.meta()["worker"]["visibility_timeout_s"] == 1500


def test_in_flight_verdict_honors_the_derived_lease_not_the_class_default(conn, service,
                                                                          monkeypatch):
    """The meter's THIRD reader: a capture claimed 1000s ago is inside the 1500s lease staging's
    worker actually holds even though it has already outlived the 900s class default — the exact
    false "lease expired" the issue reports."""
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    ack = submit_one(conn)
    claimed = queue.claim_next(conn)
    assert claimed["attempts"] == 1
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1000 seconds' "
                    "WHERE id = %s", (ack["id"],))

    row = service.worker_status()["in_flight"][0]

    assert row["lease_expired"] is False, (
        "a 1000s-old claim reads as expired against the 900s class default even though the "
        "worker's real, env-derived lease is 1500s")
    assert "within its lease" in row["verdict"]


def test_in_flight_verdict_still_reads_expired_at_the_class_default_with_no_env_var(
        conn, service, monkeypatch):
    """Benign twin: where the environment says nothing, the meter must still read a 1000s-old
    claim as expired against the 900s class default — deriving the number for staging must not
    weaken the verdict for the ordinary, unconfigured case."""
    monkeypatch.delenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", raising=False)
    ack = submit_one(conn)
    claimed = queue.claim_next(conn)
    assert claimed["attempts"] == 1
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1000 seconds' "
                    "WHERE id = %s", (ack["id"],))

    row = service.worker_status()["in_flight"][0]

    assert row["lease_expired"] is True
    assert "lease expired" in row["verdict"]


def test_overview_aggregates_without_erroring_on_an_empty_world(service):
    data = service.overview()
    assert data["queue"]["counts"]["queued"] == 0
    assert data["ingest_errors"]["unresolved"] == 0
    assert data["gardener"]["run"] is None


# ── gardener / digest / index ─────────────────────────────────────────────────────────────────
def test_gardener_state_reads_the_latest_completed_run_and_finds_partial_honest(conn, service):
    ensure_gardener_schema(conn)
    run_id = ops.record_job_run(conn, GARDENER_JOB, status="partial",
                                stats={"sweep": {"error": "SweepModelError"}})
    insert_findings(conn, run_id, [
        {"check": "anchor-concentration", "severity": "warn", "subject": "acme-corp",
         "detail": "14/18 pages anchor here", "suggested_action": "consider splitting"},
        {"check": "view-staleness", "severity": "sla", "subject": "views/acme.md",
         "detail": "stale past the SLA window", "suggested_action": ""},
    ])
    state = service.gardener_state()
    assert state["run"]["id"] == run_id
    severities = {f["severity"] for f in state["findings"]}
    assert severities == {"warn", "sla"}, "the gardener's own vocabulary: info/warn/sla"
    assert state["history"][0]["status"] == "partial"


def test_digest_preview_builds_a_body_and_post_refuses_without_its_pieces(conn, service,
                                                                          monkeypatch):
    monkeypatch.delenv("STIGMERGY_DIGEST_CHANNEL_ID", raising=False)
    preview = asyncio.run(service.digest_preview())
    assert preview["body"], "a dry run must render the would-post body"
    assert service.digest_state()["history"][0]["job"] == "digest-dry-run"
    with pytest.raises(AdminRefused, match="STIGMERGY_DIGEST_CHANNEL_ID"):
        asyncio.run(service.digest_post(actor="steward"))


# ── the async mutation seam must refuse the way its sync twin does ────────────────────────────────
# `digest_post` is `_mutate_async`'s only caller, and its `_post` closure reaches `digest.run` ->
# the queue and the Slack Web API — so the CaptureError branch is exercised at the seam itself
# rather than by manufacturing a library failure three packages down through a real Slack post.
def test_an_async_mutation_refused_by_a_library_is_the_same_409_its_sync_twin_gives(conn, service):
    """OLD BEHAVIOUR: a 500. `_mutate` maps `CaptureError` to `AdminRefused` — the routes' 409,
    carrying the library's own operator-facing sentence — and `admin/index.md` promises that
    mapping for `_mutate`/`_mutate_async` alike. `_mutate_async` had only `except Exception`, so the
    identical refusal came back through the routes' last-resort arm as an opaque server error, and
    the operator lost the sentence telling them what to do about it."""
    async def _refuse(_by):
        raise CaptureError("nothing to post — the window is empty")

    with pytest.raises(AdminRefused, match="the window is empty"):
        asyncio.run(service._mutate_async("digest.post", "steward", {}, _refuse))

    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == \
        ("steward", "digest.post", "error")
    assert recorded["error_class"] == "CaptureError", (
        "admin_actions must keep the library's OWN exception class, not the AdminRefused it was "
        "renamed to on the way out — the same rule `entity_approve` already holds")


def test_an_async_mutation_failing_for_any_other_reason_still_propagates_unrenamed(conn, service):
    """The benign twin of the branch above: only a domain refusal becomes `AdminRefused`. Anything
    else stays itself all the way to the routes' 500, class name only — a genuine fault must never
    be dressed up as an operator-actionable refusal."""
    async def _boom(_by):
        raise RuntimeError("psycopg fell over mid-post")

    with pytest.raises(RuntimeError, match="psycopg fell over"):
        asyncio.run(service._mutate_async("digest.post", "steward", {}, _boom))

    assert _actions(conn)[0]["error_class"] == "RuntimeError"


def test_index_state_and_substrate_check_run_over_the_real_store(service):
    state = service.index_state()
    assert state["meta"]["model"] == "fake-hashed-bow-256"   # the fake embedder's own signature
    assert state["zones"] == {}
    check = service.index_substrate_check()
    assert check["errors"] == 0 and isinstance(check["findings"], list)


# ── entities: read ────────────────────────────────────────────────────────────────────────────
def test_entities_list_and_show_cover_multiple_situations_and_404_on_nothing(conn, service):
    """The read-side coverage `test_a_safe_entity_name_gets_a_filled_command_and_an_unsafe_one_
    stays_inert` used to pin alongside the (now-deleted, ADR 030) command template:
    `entities_list` aggregates every pending situation and `entities_show` 404s on a nonexistent
    id, regardless of whether the underlying name would ever have been safe to paste into a shell
    command — that concern no longer exists on this surface at all."""
    safe = submit_one(conn)
    park(conn, safe["id"], report=unresolved_entity_report("Acme Corp"))
    unsafe = submit_one(conn)
    park(conn, unsafe["id"], report=unresolved_entity_report('Acme" --aliases "Trusted'))

    listed = service.entities_list()
    assert {row["id"] for row in listed} == {safe["id"], unsafe["id"]}
    assert service.entities_show(safe["id"])["subject"] == "Acme Corp"
    with pytest.raises(AdminNotFound):
        service.entities_show(999_999)


def test_a_multi_name_situation_reaches_the_console_as_a_per_name_list_not_only_the_joined_string(
        conn, service):
    """The backend half of the mint-prefill contract, unpinned until now on this surface.

    A park can name SEVERAL unresolved entities (`SITUATION_NAMES_KEY`). `subject` is one display
    string — the names joined with `", "` — and it is the only thing a single-string consumer can
    render, but it is not a value anything may act on: minting it produces one entity called
    "Jack, Acme Capital". `subjects` is the per-name list the console's Approve form has to read
    to tell one unresolved name from several, so it must survive `_situation`'s sanitizing pass
    on BOTH read paths a steward reaches (the list and the detail), not just exist in
    `entities.situations`."""
    row = submit_one(conn)
    park(conn, row["id"], report={
        capture_schema.SITUATION_KEY: capture_schema.SITUATION_UNRESOLVED_ENTITY,
        capture_schema.SITUATION_NAMES_KEY: ["Jack", "Acme Capital"]})

    shown = service.entities_show(row["id"])
    assert shown["subjects"] == ["Jack", "Acme Capital"], (
        "the detail read must carry every unresolved name separately — the form that mints reads "
        "this, and nothing can recover two names from the joined string without guessing whether "
        "a comma is a separator or part of a name")
    assert shown["subject"] == "Jack, Acme Capital", (
        "the joined display string stays too — it is what the read-only context renders")
    listed = {r["id"]: r for r in service.entities_list()}[row["id"]]
    assert listed["subjects"] == ["Jack", "Acme Capital"], (
        "the list read must carry it as well: the console navigates list → detail, and a key "
        "present on one path only is a key the next caller will find missing")


# ── the shaper is a pass-through, not a second decider ────────────────────────────────────────
# The two below hand `_situation` a row that CANNOT come out of `entities.situations`: its
# `mint_name_prefill` disagrees with its own `subjects`. That is the point — on any real row the
# decided field and a re-derivation agree, so every test above stays green if this shaper starts
# computing the prefill itself, and the duplicate policy the consolidation removed is back with no
# test able to see it. Fabricating the disagreement is the only instrument that can tell "passed
# through" from "recomputed", and it needs the private `_situation` because the two public doors
# both read the row out of Postgres, where the disagreement is unconstructible.
def test_the_situation_shaper_passes_a_decided_prefill_through_even_when_it_contradicts_subjects(
        service):
    """The prefill arrives DECIDED and leaves only sanitized. This row says "several names" in
    `subjects` and still carries a default no re-derivation could produce: every recomputation
    shape — over the raw report, over the raw `subjects`, over the cleaned `subjects` — answers
    `""` for a two-name park, so the decided string surviving is the proof the shaper never
    recomputed. The control character proves the ONE transformation that is allowed still runs."""
    row = {"id": 41, "status": capture_schema.TRIAGE, "situation": "unresolved-entity",
           "subject": "Jack, Acme Capital", "subjects": ["Jack", "Acme Capital"],
           "mint_name_prefill": "Nadia Okonk\x01wo"}

    shaped = service._situation(row)

    assert shaped["mint_name_prefill"] == "Nadia Okonkwo", (
        "the console re-derived the prefill from the row it was handed instead of forwarding the "
        "one `entities.situations.mint_name_prefill` decided — a second policy over the same "
        "irreversible mint, which is what makes two doors offer two default names for one park")
    assert shaped["subjects"] == ["Jack", "Acme Capital"], (
        "the per-name listing must survive the same pass — it is what the Approve form shows when "
        "no default is offered")


def test_the_situation_shaper_never_fills_in_a_prefill_the_rule_declined_to_offer(service):
    """The other direction, and the dangerous one: `mint_name_prefill == ""` is an INSTRUCTION —
    leave the field empty — not a missing value waiting to be helpfully filled. A single-name
    `subjects` beside an empty decision is exactly the shape an `or`-fallback
    (`out.get("mint_name_prefill") or mint_name_prefill(row)`) would rewrite, and the test above
    cannot see that fallback because its own prefill is truthy. A default the rule refused is a
    name a steward submits unchanged into a signed commit."""
    row = {"id": 42, "status": capture_schema.TRIAGE, "situation": "unresolved-entity",
           "subject": "Solo Corp", "subjects": ["Solo Corp"], "mint_name_prefill": ""}

    shaped = service._situation(row)

    assert shaped["mint_name_prefill"] == "", (
        "the console offered a default for a row whose decision was 'offer none' — an empty "
        "prefill is the rule's answer, and treating it as an absent value puts a name into the "
        "mint form that `entities.situations` deliberately withheld")
    assert "mint_name_prefill" in shaped, "and it stays PRESENT: absent and empty are not the same"


def test_entities_show_no_longer_carries_a_command_template(conn, service):
    """OLD BEHAVIOUR (ADR 029): `entities_show` returned a `commands` key — the exact
    `stigmergy-entities approve`/`reject` command, filled only when the name passed the shared
    shell-safety predicate (`entities.cli.suggestable_entity_name`), otherwise the name as inert
    text beside a template. ADR 030 deletes it, not polishes it: the Entities tab mints for real
    through a form now, so there is no command left to print."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    view = service.entities_show(ack["id"])

    assert "commands" not in view


# ── entities: approve — mints through the governed door directly (ADR 030) ─────────────────────
@pytest.fixture()
def entity_service(conn, admin_settings, entity_mint_repo):
    """`AdminService` pointed at a real, throwaway bare knowledge repo — for the tests below that
    actually mint. The validation-only tests use the plain `service` fixture instead (no repo
    configured): they never reach git at all, and proving that is part of what they pin."""
    return AdminService(conn, server_settings=Settings(librarian_repo_url=entity_mint_repo),
                        admin_settings=admin_settings)


def test_entity_approve_mints_for_real_and_records_both_ledgers(
        conn, entity_service, entity_mint_repo, require_gitleaks):
    """The end-to-end proof, admin's own: ONE commit lands on the real bare remote, the append-
    only `review_decisions` ledger records the SAME `extra` shape `server.review`'s own mint does
    (`entity_id` + `commit`), and `admin_actions` records the attempt under the actor's name — the
    two ledgers ADR 030 requires of a server-driven mint, whichever door it came through."""
    ack = submit_one(conn, submitted_by="steward@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    result = entity_service.entity_approve(
        ack["id"], actor="steward@example.com", name="Globex Robotics",
        entity_type="organization", aliases="Globex, Globex Robotics Inc",
        role="a robotics manufacturer")

    assert result["entity_id"] == "globex-robotics"
    assert result["name"] == "Globex Robotics"
    assert result["aliases"] == ["Globex", "Globex Robotics Inc"]
    assert len(result["commit"]) == 40
    assert result["requeued"] is True                        # the service's own default

    with conn.cursor() as cur:
        cur.execute("SELECT item_kind, item_id, verdict, actor, extra FROM review_decisions"
                    " WHERE item_id = %s", (str(ack["id"]),))
        kind, item_id, verdict, actor, extra = cur.fetchone()
    assert (kind, item_id, verdict, actor) == (
        "entity-proposal", str(ack["id"]), "approve", "steward@example.com")
    assert extra == {"entity_id": "globex-robotics", "commit": result["commit"]}

    # The same door-parity proof `tests/server/test_review.py`'s own mint-for-real test makes for
    # MCP (ADR 030 D1): the App authors the commit, and the `Approved-by:` trailer carries the
    # console's free-text `actor` — attribution, not a resolved identity (D2) — but it still has to
    # actually reach the commit git log answers "who approved this identity" with. Nothing before
    # this change checked it on the console's OWN door.
    author = gitcmd.run("log", "-1", "--format=%an <%ae>", result["commit"],
                        cwd=entity_mint_repo).stdout.strip()
    assert author == "stigmergy-librarian <stigmergy-librarian@users.noreply.github.com>"
    message = gitcmd.run("log", "-1", "--format=%B", result["commit"], cwd=entity_mint_repo).stdout
    assert "Approved-by: steward@example.com" in message

    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == (
        "steward@example.com", "entities.approve", "ok")

    assert queue.current_status(conn, ack["id"]) == capture_schema.QUEUED


def test_entity_approve_requeue_false_leaves_the_capture_parked(
        conn, entity_service, require_gitleaks):
    """`requeue` defaults to `True` (the console form's checkbox is pre-checked), but an operator
    who unchecks it gets exactly the CLI's own un-requeued shape: the entity is minted, and the
    capture stays right where it was — the same "approved but not yet re-filed" state
    `stigmergy-entities approve` (no `--requeue`) leaves behind."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    result = entity_service.entity_approve(ack["id"], actor="steward", name="Acme Corp",
                                           entity_type="organization", requeue=False)

    assert result["requeued"] is False
    assert queue.current_status(conn, ack["id"]) == capture_schema.TRIAGE


def test_entity_approve_defaults_the_entity_id_to_the_slug(conn, entity_service, require_gitleaks):
    """`entity_id` is deliberately not a console form field (ADR 030 D5, "one less field to
    mistype") — the console never sends one, so the server-side default has to actually take:
    `generator.canonical_id_for`, the same function `--id` is verified against."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    result = entity_service.entity_approve(ack["id"], actor="steward", name="Acme Corp",
                                           entity_type="organization")

    assert result["entity_id"] == "acme-corp"


def test_entity_approve_missing_name_and_type_is_a_bad_request_before_anything_is_attempted(
        conn, service):
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    with pytest.raises(AdminBadRequest, match="missing name and entity_type"):
        service.entity_approve(ack["id"], actor="steward", name="", entity_type="")

    assert _actions(conn) == [], "an actionable refusal on shape must record nothing"
    assert queue.current_status(conn, ack["id"]) == capture_schema.TRIAGE


def test_entity_approve_unknown_type_is_a_bad_request(conn, service):
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    with pytest.raises(AdminBadRequest, match="is not one of"):
        service.entity_approve(ack["id"], actor="steward", name="Acme Corp", entity_type="alien")


def test_entity_approve_without_a_repo_url_is_refused_with_the_capability_sentence(conn, service):
    """OLD BEHAVIOUR (ADR 029): the Entities tab was read-only — there was no capability to be
    missing. Since ADR 030 the console mints for real, and a deployment with no
    `$STIGMERGY_LIBRARIAN_REPO_URL` refuses cleanly instead of minting nowhere or crashing —
    `entities.errors.CapabilityUnavailableError` mapped to `AdminRefused`, the SAME posture
    `server.review` maps it to, one door over."""
    ack = submit_one(conn, submitted_by="steward@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    with pytest.raises(AdminRefused, match="STIGMERGY_LIBRARIAN_REPO_URL"):
        service.entity_approve(ack["id"], actor="steward@example.com", name="Globex Robotics",
                               entity_type="organization")

    recorded = _actions(conn)[0]
    assert recorded["outcome"] == "error"
    assert recorded["error_class"] == "CapabilityUnavailableError", (
        "admin_actions must keep the library's OWN exception class, not the AdminRefused it was "
        "translated to for the caller")
    assert queue.current_status(conn, ack["id"]) == capture_schema.TRIAGE


def test_entity_approve_refuses_a_row_that_is_no_longer_parked(conn, service):
    """`situations.require_situation`'s own write guard, reached through this door: a row that
    was never parked as an identity question refuses before any git work is attempted (and before
    the missing-capability check above even gets a chance to fire)."""
    ack = submit_one(conn)   # queued, never parked

    with pytest.raises(AdminRefused, match="triage"):
        service.entity_approve(ack["id"], actor="steward", name="Acme Corp",
                               entity_type="organization")


def test_entity_approve_refuses_a_collision_with_the_librarys_own_sentence(
        conn, entity_service, entity_mint_repo, require_gitleaks):
    """Drift/collision refusals surface through the console door too, carrying the library's own
    sentence (`entities.birth._refuse_collisions`, reached through `entities.mint.mint` ->
    `entities.remote.mint_via_clone` — the SAME gate `tests/server/test_review.py::
    test_review_decide_entity_proposal_approve_surfaces_drift_refusal` proves for MCP), with real
    git: every OTHER `entity_approve` refusal this suite proves is either a shape error (before any
    git work) or the missing-capability posture — nothing here proved the collision gate itself
    fires on THIS door."""
    first = submit_one(conn, submitted_by="steward@example.com")
    park(conn, first["id"], report=unresolved_entity_report("Acme Corp"))
    # `requeue=False`: `park()`'s own fixture helper claims the single oldest QUEUED row, and the
    # default `requeue=True` would hand capture #1 right back to `queued` — stealing the claim the
    # second `park()` call below needs for capture #2. Irrelevant to what this test proves either
    # way (the collision gate, not the requeue behaviour, which has its own dedicated tests).
    entity_service.entity_approve(first["id"], actor="steward@example.com", name="Acme Corp",
                                  entity_type="organization", requeue=False)

    second = submit_one(conn, submitted_by="steward@example.com")
    park(conn, second["id"], report=unresolved_entity_report("Acme Corp"))
    before = gitcmd.run("rev-parse", "main", cwd=entity_mint_repo).stdout

    with pytest.raises(AdminRefused, match="already resolves to the registered entity"):
        entity_service.entity_approve(second["id"], actor="steward@example.com", name="Acme Corp",
                                      entity_type="organization")

    after = gitcmd.run("rev-parse", "main", cwd=entity_mint_repo).stdout
    assert before == after, "a refused collision must leave git exactly where it was"
    recorded = _actions(conn)[0]
    assert recorded["outcome"] == "error" and recorded["error_class"] == "CollisionError", (
        "admin_actions must keep the library's OWN exception class, same posture as the "
        "missing-capability case above")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s",
                    (str(second["id"]),))
        assert cur.fetchone()[0] == 0, "a refused mint must record nothing in the governance ledger"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# CHARACTERIZATION — this door's half of the mint sequence, pinned as it behaves TODAY.
#
# The console and `server.review` run the SAME five mechanical steps (`require_situation` ->
# `mint_via_clone` -> `record_decision` -> conditional `requeue` after the push). What each door
# proved about that sequence was different, so a property could hold on one and be merely assumed
# on the other. These three have twins in `tests/server/test_review.py` under the same names minus
# the door, except the self-approval one — that asymmetry is the WHOLE point of ADR 030 D2 and has
# no twin by design.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_characterization_the_console_door_requeues_strictly_after_the_push(
        conn, entity_service, entity_mint_repo, monkeypatch, require_gitleaks):
    """Pins the ORDER: at the instant `dispositions.requeue` is entered, the bare remote's `main`
    ALREADY points at the mint commit.

    `test_entity_approve_mints_for_real_and_records_both_ledgers` asserts the two END STATES (a
    commit came back, the row is `queued`), which a requeue that ran FIRST satisfies just as well —
    the note's `entity_id` is `resolved_id`, known before the mint is attempted. The failure a
    reordering causes is invisible until a real run: the librarian fetches a remote that does not
    carry the entity yet and parks the capture a SECOND time.

    A spy, not a double — it records what git actually says and then delegates to the real
    `requeue`. Real git, real Postgres, real disposition; the patch exists only because ordering is
    unobservable from the end state.
    """
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))
    real_requeue = dispositions.requeue
    observed_heads = []

    def spy(*args, **kwargs):
        observed_heads.append(
            gitcmd.run("rev-parse", "main", cwd=entity_mint_repo).stdout.strip())
        return real_requeue(*args, **kwargs)

    monkeypatch.setattr(dispositions, "requeue", spy)
    head_before = gitcmd.run("rev-parse", "main", cwd=entity_mint_repo).stdout.strip()

    result = entity_service.entity_approve(ack["id"], actor="steward", name="Globex Robotics",
                                           entity_type="organization", requeue=True)

    assert observed_heads == [result["commit"]], (
        "exactly ONE requeue, and the remote it ran against already carried the pushed commit — "
        f"observed {observed_heads}, mint pushed {result['commit']}")
    # Non-vacuity: the two candidate values are actually DIFFERENT, so the assertion above
    # discriminates. Without this the test would still pass on a remote that never moved.
    assert observed_heads[0] != head_before, (
        "the probe cannot tell before from after — the mint did not move the remote")


def test_characterization_one_console_mint_writes_exactly_one_ledger_row_with_an_empty_note(
        conn, entity_service, require_gitleaks):
    """Pins the ledger WRITE COUNT and the full row shape this door produces.

    Every existing ledger assertion on both doors reads with `fetchone()`, which a second,
    duplicate `record_decision` would pass unnoticed. `notes` is the shape difference the door
    parity claim has never actually stated: the console form has no note field, so this door
    writes `''` while `server.review` writes the steward's cleaned note (its twin pins that). A
    `NULL` here instead of `''` would be a new spelling in an append-only table.
    """
    ack = submit_one(conn, submitted_by="filer@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    result = entity_service.entity_approve(
        ack["id"], actor="steward@example.com", name="Globex Robotics",
        entity_type="organization")

    with conn.cursor() as cur:
        cur.execute("SELECT item_kind, item_id, verdict, actor, notes, extra FROM "
                    "review_decisions WHERE item_id = %s", (str(ack["id"]),))
        rows = cur.fetchall()
    assert len(rows) == 1, f"one mint, one governance row — got {len(rows)}"
    assert rows[0] == ("entity-proposal", str(ack["id"]), "approve", "steward@example.com", "",
                       {"entity_id": "globex-robotics", "commit": result["commit"]})


def test_characterization_the_console_does_not_enforce_self_approval_adr_030_d2(
        conn, entity_service, entity_mint_repo, require_gitleaks):
    """Pins the DELIBERATE ASYMMETRY, so no one can remove it by accident.

    ADR 030 D2: MCP and Slack resolve a real identity, check `ops/stewards.json` and enforce
    `SELF_APPROVAL_REFUSED` (`tests/server/test_review.py::test_review_decide_entity_proposal_
    approve_self_approval_still_refused` and its Slack twin). The console mints under the ADMIN
    TOKEN with `actor` as ATTRIBUTION, exactly like the CLI it replaced — the ADR calls enforcing a
    second-human rule against one shared credential "theatre" and refuses to pretend.

    So the SAME person filing and approving mints for real here, and the commit still names them.
    Until this test the property was only exercised by ACCIDENT — the mint-for-real test above
    happens to pass `actor="steward@example.com"` for a row `submit_one` defaults to the same
    address — coverage that would evaporate the day someone changed that fixture default, and that
    said nothing about WHY it must hold. A refactor that "helpfully" unified the two doors'
    authorization would overturn a decision, not remove duplication; this is what goes red.
    """
    ack = submit_one(conn, submitted_by="same-person@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    result = entity_service.entity_approve(
        ack["id"], actor="same-person@example.com", name="Globex Robotics",
        entity_type="organization")

    assert result["entity_id"] == "globex-robotics"
    assert len(result["commit"]) == 40
    message = gitcmd.run("log", "-1", "--format=%B", result["commit"],
                         cwd=entity_mint_repo).stdout
    assert "Approved-by: same-person@example.com" in message, (
        "attribution, not authorization — the filer's own name reaches the commit (D2)")
    recorded = _actions(conn)[0]
    assert (recorded["action"], recorded["outcome"]) == ("entities.approve", "ok")


def test_entity_approve_blank_actor_falls_back_to_the_configured_default(
        conn, entity_service, require_gitleaks):
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    entity_service.entity_approve(ack["id"], actor="   ", name="Acme Corp",
                                  entity_type="organization")

    assert _actions(conn)[0]["actor"] == "suite-default-actor"


# ── queue.reject of an entity situation ALSO records review_decisions (audit fix S1) ────────────
# The Entities tab deliberately does not grow its own Reject button — `entities/index.md`: "the
# situation shares its id with the queue row, and the queue tab already rejects it" — so
# `queue_reject` is the one place a console rejection of an entity-proposal situation can be
# recorded, and it used to write `admin_actions` only: the append-only governance ledger answered
# "who decided this identity" from ONE table for approve and a DIFFERENT one for reject, on the
# one console door that has both. Placed here, beside `entity_approve`'s own ledger tests, rather
# than beside the plain `queue_reject` tests above, so the PARALLEL is visible: approve writes
# both ledgers, and reject now does too.
def test_queue_reject_of_an_entity_situation_also_records_review_decisions(conn, service):
    ack = submit_one(conn, submitted_by="steward@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    service.queue_reject(ack["id"], actor="steward@example.com", reason="not a real org")

    with conn.cursor() as cur:
        cur.execute("SELECT item_kind, item_id, verdict, actor, notes FROM review_decisions"
                    " WHERE item_id = %s", (str(ack["id"]),))
        row = cur.fetchone()
    assert row is not None, "an entity-situation reject through the console must reach the ledger"
    kind, item_id, verdict, actor, notes = row
    assert (kind, item_id, verdict, actor) == (
        "entity-proposal", str(ack["id"]), "reject", "steward@example.com")
    assert "not a real org" in notes
    # BOTH ledgers, like every other governed door — `admin_actions` via `_mutate`, unchanged.
    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == (
        "steward@example.com", "queue.reject", "ok")


def test_queue_reject_of_an_ordinary_parked_capture_does_not_touch_review_decisions(conn, service):
    """The benign twin: the new ledger write is scoped to entity situations ONLY, through the SAME
    `situations.classify` predicate the review inbox uses to tell the two kinds apart
    (`server.review._collect_open_items`) — an ordinary parked capture (no `situation` key at all)
    must not grow a `review_decisions` row nobody asked for."""
    ack = submit_one(conn)
    park(conn, ack["id"])   # no `situation` key — an ordinary parked capture, not an identity ask

    service.queue_reject(ack["id"], actor="steward", reason="not useful")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s", (str(ack["id"]),))
        assert cur.fetchone()[0] == 0
    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == (
        "steward", "queue.reject", "ok"), "admin_actions must still record the ordinary reject"


# ── activity ──────────────────────────────────────────────────────────────────────────────────
def test_activity_reads_the_audit_trail_and_never_a_submission_payload(conn, service):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (identity, tool, args, duration_ms, outcome, error_class)"
            " VALUES ('steward@example.com', 'ask', '{\"question\": \"what is our MRR?\"}', 120,"
            " 'ok', ''),"
            " ('ana@example.com', 'ask', '{}', 5, 'error', 'RateLimitError')")
    data = service.activity()
    assert data["ask_questions"] == ["what is our MRR?"]
    assert data["rate_limited"][0]["identity"] == "ana@example.com"
    tools = {(r["identity"], r["tool"]) for r in data["by_identity_tool"]}
    assert ("steward@example.com", "ask") in tools


# ── crons ─────────────────────────────────────────────────────────────────────────────────────
def test_crons_without_a_gateway_is_the_database_truth_only(conn, service):
    ops.record_job_run(conn, GARDENER_JOB, status="ok", stats={})
    state = service.crons_state()
    assert state["configured"] is False
    by_file = {w["file"]: w for w in state["workflows"]}
    assert by_file["gardener.yml"]["latest_run"]["status"] == "ok"
    assert by_file["index-rebuild.yml"]["latest_run"] is None
    assert by_file["index-rebuild.yml"]["index_built_at"], "built_at is that cron's truth source"


def test_crons_with_a_gateway_carries_state_and_runs(gh_service):
    state = gh_service.crons_state()
    by_file = {w["file"]: w for w in state["workflows"]}
    assert by_file["gardener.yml"]["state"] == "disabled_manually"
    assert by_file["index-rebuild.yml"]["runs"][0]["conclusion"] == "success"


def test_dispatch_enforces_the_allowlist_before_any_github_call(gh_service, fake_gateway):
    with pytest.raises(AdminBadRequest, match="not a console-drivable workflow"):
        gh_service.cron_dispatch("deploy-anything.yml", actor="steward")
    assert fake_gateway.calls == []


def test_dispatch_converts_declared_inputs_and_records_the_action(conn, gh_service,
                                                                  fake_gateway):
    result = gh_service.cron_dispatch("retention-purge.yml", actor="steward",
                                      inputs={"dry_run": True})
    assert result["inputs"] == {"dry_run": "true"}
    assert ("dispatch", "retention-purge.yml", "main", {"dry_run": "true"}) in fake_gateway.calls
    assert _actions(conn)[0]["action"] == "cron.dispatch:retention-purge.yml"


def test_an_undeclared_input_is_refused_by_name(gh_service, fake_gateway):
    with pytest.raises(AdminBadRequest, match="declares no 'dry_run' input"):
        gh_service.cron_dispatch("gardener.yml", actor="steward", inputs={"dry_run": True})
    assert fake_gateway.calls == []


def test_without_a_gateway_a_dispatch_is_refused_with_the_degradation_sentence(service):
    with pytest.raises(AdminRefused, match="GitHub is not configured"):
        service.cron_dispatch("gardener.yml", actor="steward")


def test_the_console_schedule_table_matches_the_workflow_files():
    """The pin: `CRON_WORKFLOWS` cannot drift from the YAML files it describes."""
    import pathlib

    import yaml

    # The three cron files are TEMPLATES an operator copies into their knowledge repo, so they
    # live outside `.github/workflows/` (a file there is registered by GitHub whether enabled or
    # not, and three "Disabled" rows on a public Actions tab read as a broken project). The
    # console still dispatches them by the same file NAME, in whichever repo they were copied to.
    workflows_dir = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "workflows"
    for row in CRON_WORKFLOWS:
        with open(workflows_dir / row["file"], encoding="utf-8") as f:
            config = yaml.safe_load(f)
        triggers = config.get("on") or config.get(True)
        crons = [entry["cron"] for entry in triggers["schedule"]]
        assert row["schedule_utc"] in crons, (
            f"{row['file']} schedules {crons} but the console table says "
            f"{row['schedule_utc']!r} — update CRON_WORKFLOWS")


# ── the flagless horizon is clamped, whatever the env says ─────────────────────────────────────
# The caller-supplied branch was always clamped; the DEFAULT branch was not, and after #38 it is
# the branch carrying operator-controlled data. `release_expired`'s predicate is
# `claimed_at < now() - make_interval(secs => %s)`, so a negative horizon reads as `now() + N` and
# every claimed row — including one claimed a millisecond ago — is expired. That would make the
# ordinary Reclaim button strictly more destructive than the deliberate "release everything now"
# checkbox.
def test_a_negative_agent_budget_cannot_turn_the_flagless_reclaim_into_a_release_everything(
        conn, service, monkeypatch):
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "-1000")
    submit_one(conn)
    queue.claim_next(conn)
    result = service.queue_reclaim(actor="tester")
    assert result == {"released": 0, "failed": 0}
    recorded = admin_schema.recent_actions(conn, limit=1)[0]
    assert recorded["args"]["visibility_timeout_s"] >= 0


def test_a_malformed_agent_budget_leaves_the_console_serving_its_own_boot_call(
        conn, service, monkeypatch):
    """`meta()` is what the console calls to boot. Raising here would answer a config typo with
    the login screen — the one screen that reads as "your token is wrong" — on the exact tool an
    operator would use to diagnose it. The fallback is honest because the same value stops the
    WORKER from booting at all, so there is no live lease to misreport."""
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "")
    assert service.meta()["worker"]["visibility_timeout_s"] == (
        librarian_config.DEFAULT_VISIBILITY_TIMEOUT_S)
    assert service.worker_status()["visibility_timeout_s"] == (
        librarian_config.DEFAULT_VISIBILITY_TIMEOUT_S)
