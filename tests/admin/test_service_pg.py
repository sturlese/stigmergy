"""AdminService over the real queue/tables (stigmergy_test), and over a real bare knowledge repo
for the decisions: every mutation lands through the SAME library seams the CLIs and the review
lane use, so what these prove is parity, not a parallel implementation."""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

from stigmergy.admin import schema as admin_schema
from stigmergy.admin import service as admin_service
from stigmergy.admin.service import (
    INDEX_REBUILD_COMMAND,
    NIGHT_SHIFT,
    PURGE_DRY_RUN_JOB,
    AdminBadRequest,
    AdminNotFound,
    AdminRefused,
    AdminService,
    worker_visibility_timeout_s,
)
from stigmergy.capture import ops, queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.errors import CaptureError
from stigmergy.gardener.schema import JOB_NAME as GARDENER_JOB
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.gardener.store import insert_findings
from stigmergy.index import store as index_store
from stigmergy.librarian import config as librarian_config
from stigmergy.repair import schema as repair_schema
from stigmergy.server.settings import Settings
from tests.admin.conftest import (
    LANDED_COMMIT,
    finish_one,
    landed_delete,
    landed_entity_body,
    landed_repair,
    refused_repair,
    register_entity,
    submit_one,
)


@pytest.fixture()
def service(conn, server_settings, admin_settings):
    return AdminService(conn, server_settings=server_settings, admin_settings=admin_settings)


@pytest.fixture()
def gh_service(conn, server_settings, admin_settings):
    return AdminService(conn, server_settings=server_settings, admin_settings=admin_settings)


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
    finish_one(conn, ack["id"], status=capture_schema.FILED,
               report={"status": "filed", "summary": "filed — a page"})
    filed = service.queue_list()["submissions"][0]
    assert "hola desde la consola" in filed["excerpt"], "a filed row's material IS readable"
    assert filed["payload_purged"] is False and "waiting_on" not in filed


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
    finish_one(conn, ack["id"], status=capture_schema.FILED)   # pending withholds; filed shows
    row = service.queue_list()["submissions"][0]
    assert row["id"] == ack["id"]
    assert "\x1b" not in row["excerpt"]
    assert "<script>" in row["excerpt"]


# ── the two acts on the whole queue ───────────────────────────────────────────────────────────
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
    the worker's own derived lease (900 at the class default). Every capture held between those
    two numbers — the long
    agent items the derived lease exists for — was requeued out from under a RUNNING worker by the
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
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1400 seconds' "
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
    `$STIGMERGY_LIBRARIAN_TIMEOUT_S`, so a capture aged 1400s — inside the 1500s lease staging's
    worker actually holds — gets swept anyway. A wasteful redelivery of an item a healthy worker
    still holds."""
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    ack = submit_one(conn)
    queue.claim_next(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1400 seconds' "
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
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1400 seconds' "
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
    queue.claim_next(conn, visibility_timeout_s=0)
    monkeypatch.setattr(admin_schema, "_INSERT", "INSERT INTO no_such_table VALUES (1)")
    result = service.queue_reclaim(actor="steward", visibility_timeout_s=0)
    assert result["released"] == 1, "the work must land even when its bookkeeping cannot"
    assert queue.current_status(conn, ack["id"]) == capture_schema.QUEUED


def test_a_blank_actor_falls_back_to_the_configured_default(conn, service):
    service.queue_reclaim(actor="   ", visibility_timeout_s=0)
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
    """The meter's THIRD reader: a capture claimed 1400s ago is inside the 1500s lease staging's
    worker actually holds even though it has already outlived the 900s class default — the exact
    false "lease expired" the issue reports."""
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")
    ack = submit_one(conn)
    claimed = queue.claim_next(conn)
    assert claimed["attempts"] == 1
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1400 seconds' "
                    "WHERE id = %s", (ack["id"],))

    row = service.worker_status()["in_flight"][0]

    assert row["lease_expired"] is False, (
        "a 1400s-old claim reads as expired against the 900s class default even though the "
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
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '1400 seconds' "
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
        {"check": "orphan-page", "severity": "info", "subject": "wiki/notes/loose.md",
         "detail": "nothing links here", "suggested_action": ""},
    ])
    state = service.gardener_state()
    assert state["run"]["id"] == run_id
    severities = {f["severity"] for f in state["findings"]}
    assert severities == {"warn", "info"}, "the gardener's own vocabulary: info/warn"
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


def test_index_state_answers_freshness_for_every_cached_ops_file(service, conn):
    """The operator question issues #74 and #79 were found through — "is what I am serving fresh,
    and from which sha?" — has no surface but this one, and until now nothing asserted the panel
    at all. One file's snapshot present, the other two absent: the state names all three, with
    the snapshot's source and a `None` that the console renders as "no snapshot"."""
    for relpath in index_store.OPS_FILE_RELPATHS:
        index_store.clear_ops_file(conn, relpath)   # arrange, never inherit a leftover snapshot
    index_store.write_ops_file(conn, index_store.IDENTITIES_RELPATH,
                               '{"ana@example.com": ["finance"]}', "abc123def")
    try:
        state = service.index_state()

        assert set(state["ops_files"]) == set(index_store.OPS_FILE_RELPATHS)
        identities = state["ops_files"][index_store.IDENTITIES_RELPATH]
        assert identities["source"] == "abc123def"
        assert identities["refreshed_at"]            # ISO string, rendered as an age
        assert state["ops_files"][index_store.SLACK_CHANNELS_RELPATH] is None
        assert state["entity_registry"] == state["ops_files"][index_store.ENTITY_REGISTRY_RELPATH]
    finally:
        for relpath in index_store.OPS_FILE_RELPATHS:
            index_store.clear_ops_file(conn, relpath)


def test_a_registry_the_loader_refuses_reads_as_a_refusal_not_a_500(conn, admin_settings,
                                                                    tmp_path):
    """OLD BEHAVIOUR: a 500 with the class name and nothing else. `index_substrate_check` caught
    `StigmergyIndexError` only, while the registry it points at is read through
    `kernel.registry.load_registry`, which raises a bare `ValueError` for a nameless entity — the
    very case `index.check.registry_ids` exists to stop blessing. The operator got "the operation
    failed (ValueError)" for a file the loader could describe precisely.

    It is a refusal, not a fault: the substrate the console was pointed at is broken, the loader's
    own sentence names the file and the entity, and `AdminRefused` is what carries an
    operator-actionable sentence to the console (409)."""
    # ARRANGE the precondition rather than inherit it. Since #74 the console lints the copy the
    # SERVER serves, and a snapshot left in the shared singleton by any earlier module would be
    # that copy — so this test would lint a perfectly good registry and never reach the refusal it
    # is about. Whether it passes must not depend on collection order.
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    registry = tmp_path / "entity-registry.json"
    registry.write_text(json.dumps({"entities": {"acme": {"aliases": []}}}))   # no 'name'
    broken = AdminService(conn, server_settings=Settings(entity_registry_path=str(registry)),
                          admin_settings=admin_settings)

    with pytest.raises(AdminRefused) as caught:
        broken.index_substrate_check()

    assert "'acme'" in str(caught.value) and "name" in str(caught.value), (
        "the loader's own sentence is the point — it says which file and which entity")


# ── entities: read ────────────────────────────────────────────────────────────────────────────
# ── registering an entity: the console commissions a capture, the librarian writes the page ──
def test_entity_create_commissions_a_capture_the_librarian_writes_the_page_from(conn, admin_settings):
    """ADR 042. OLD BEHAVIOUR: the console's Register minted the template with the name filled in,
    pushed it and wrote the ledger row — an entity page with nothing said about the entity. Now it
    QUEUES a capture: the steward's account is the material, the registration rides the hints,
    the row is attributed to the steward the page will be born confirmed by, and an admin action
    records the act. Git and the ledger stay untouched until the librarian has written the page."""
    from stigmergy.capture.evidence import MemoryEvidenceStore
    service = AdminService(conn, server_settings=Settings(), admin_settings=admin_settings,
                           evidence=MemoryEvidenceStore())

    result = service.entity_create(actor="steward@example.com", name="Stark Industries",
                                   entity_type="organization", aliases="Stark, SI",
                                   about="Stark Industries is the client whose reporting we automate.")

    assert result["status"] == "queued" and result["entity_id"] == "stark-industries"
    with conn.cursor() as cur:
        cur.execute("SELECT submitted_by, hints, payload FROM capture_queue WHERE id = %s",
                    (result["id"],))
        by, hints, payload = cur.fetchone()
    assert by == "steward@example.com"
    registration = capture_schema.registration_from_hints(hints)
    assert registration.name == "Stark Industries" and set(registration.aliases) == {"Stark", "SI"}
    assert registration.source == "admin"
    assert payload["text"].startswith("Stark Industries is the client")
    action = _actions(conn)[0]
    assert action["action"] == "entities.create" and action["args"]["about_chars"] > 0


def test_entity_create_missing_name_type_or_account_is_a_bad_request_before_anything_is_queued(
        conn, service):
    with pytest.raises(AdminBadRequest, match="name and entity_type and about"):
        service.entity_create(actor="marc", name="", entity_type="", about="")
    with pytest.raises(AdminBadRequest, match="about"):
        service.entity_create(actor="marc", name="X", entity_type="organization", about="  ")
    with pytest.raises(AdminBadRequest, match="not one of"):
        service.entity_create(actor="marc", name="X", entity_type="galaxy", about="A thing.")
    with pytest.raises(AdminBadRequest, match="not the slug"):
        service.entity_create(actor="marc", name="X Corp", entity_type="organization",
                              about="A thing.", entity_id="y-corp")
    assert _actions(conn) == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


def test_entity_create_refuses_a_name_the_served_registry_already_resolves(conn, admin_settings,
                                                                          entity_mint_repo):
    """The entity exists, so there is nothing to register: the steward is told which entity the
    name resolves to, and that capturing about it is the thing to do. Refused before a row is
    queued — the librarian would only have proposed a spelling."""
    from stigmergy.capture.evidence import MemoryEvidenceStore
    register_entity(entity_mint_repo, conn, "Acme Corp", aliases=["Acme"])
    service = AdminService(conn, server_settings=Settings(), admin_settings=admin_settings,
                           evidence=MemoryEvidenceStore())
    with pytest.raises(AdminBadRequest, match="already resolves to the registered entity 'acme-corp'"):
        service.entity_create(actor="marc", name="Acme", entity_type="organization",
                              about="Our oldest client.")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


def test_entity_create_without_an_evidence_store_is_a_refusal_naming_it(conn, service):
    with pytest.raises(AdminRefused, match="evidence store"):
        service.entity_create(actor="marc", name="Stark Industries", entity_type="organization",
                              about="A client.")


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
def test_the_jobs_page_reports_every_night_shift_pass_from_the_database(conn, service):
    """The page is a pure database read: every row's truth is a `job_runs` row the pass wrote
    itself, or the index's own `built_at`. Nothing is fetched from another service, which is why
    this page has no degraded state to render."""
    state = service.jobs_state()
    by_file = {job["file"]: job for job in state["jobs"]}
    assert set(by_file) == {"gardener", "retention-purge", "index-rebuild"}
    assert by_file["gardener"]["latest_run"] is None      # nothing has run in this fresh database
    assert by_file["index-rebuild"]["latest_run"] is None  # the rebuild writes no job row, ever
    ops.record_job_run(conn, GARDENER_JOB, status="ok", stats={"findings": 3})
    assert service.jobs_state()["jobs"][0]["latest_run"]["stats"] == {"findings": 3}


def test_the_purge_row_reads_the_dry_run_job_too(conn, service):
    """A dry run IS a run of the retention pass, and an operator who previewed at 04:42 and sees
    "no run recorded" would reasonably conclude the night shift is dead. `_truth_jobs` folds the
    two names, and this is the twin that keeps it folded."""
    ops.record_job_run(conn, PURGE_DRY_RUN_JOB, status="ok", stats={"purged": 0, "dry_run": True})
    row = {job["file"]: job for job in service.jobs_state()["jobs"]}["retention-purge"]
    assert row["latest_run"]["job"] == PURGE_DRY_RUN_JOB


def test_the_console_names_the_setting_that_actually_schedules_each_worker_pass():
    """The drift guard that replaced the cron-YAML one: the Jobs page tells an operator which
    variable moves a pass, and a renamed variable would leave the page naming a setting that does
    nothing. Pinned against `librarian.config`'s own constants, and against `Settings` actually
    having a field the name resolves to — so a variable that stopped being read fails here."""
    settings = librarian_config.Settings.from_args(argparse.Namespace())
    for job in NIGHT_SHIFT:
        if job["runs_in"] != "worker":
            assert not job["at_setting"], f"{job['file']} is not a worker pass but names a setting"
            continue
        assert job["at_setting"].startswith("STIGMERGY_")
        field = job["at_setting"].removeprefix("STIGMERGY_LIBRARIAN_").lower()
        assert getattr(settings, field) == job["at_default"], (
            f"the console says {job['file']} runs at {job['at_default']} via "
            f"${job['at_setting']}, which the librarian's own settings do not agree with")


def test_the_pass_the_console_cannot_run_names_a_command_that_exists():
    """**A message containing a command is an executable promise.** The Jobs page tells an
    operator to rebuild the index by hand — because the deployed worker has no embedding key by
    design and cannot — so this runs the binary that sentence names. Its `--help` is enough: the
    promise being kept is that the command EXISTS and takes the flag, not that a rebuild succeeds
    without a key (it would not, which is the whole reason the pass is not in the night shift)."""
    binary, flag = INDEX_REBUILD_COMMAND.split()[0], INDEX_REBUILD_COMMAND.split()[1]
    # Beside this interpreter first, then PATH: an editable checkout's console scripts live in the
    # venv's bin, which `python -m pytest` does not put on PATH.
    beside = os.path.join(os.path.dirname(sys.executable), binary)
    resolved = beside if os.path.exists(beside) else shutil.which(binary)
    assert resolved, f"the console tells an operator to run {binary!r}, which is not installed"
    completed = subprocess.run([resolved, "--help"], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert flag in completed.stdout, (
        f"{binary} --help does not mention {flag} — the console's rebuild sentence names a flag "
        f"the command does not take")


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


# ── repairs: what the worker did, read-only (ADR 044) ─────────────────────────────────────────
# Nothing on this page decides anything any more. What it is FOR is the reading nobody gave a
# repair before it landed: every applied row carries the diff that reached `main`, and every failed
# one carries the sentence that refused it. So what these pin is this package's own half — the
# sanitizing, the per-kind op shapes, and the counts a chart may draw a part-to-whole from.
#
# The console's ONE surviving mutation is `pages.delete`, and it is the console's most consequential
# button. Since ADR 044 D3 it QUEUES: this process writes nothing to the corpus and holds no git
# credential, and the worker performs the removal (`tests/librarian/test_delete_processing_pg.py`
# proves that half against a real remote). So what is asserted here is the row this door lands, the
# `admin_actions` bookkeeping and the error mapping.


@pytest.fixture()
def deletion_service(conn, admin_settings):
    """`AdminService` with the capture queue wired. An evidence store is the whole of what this
    door needs now — a removal is a `delete` row, and the row's material is the reason. It used to
    take a knowledge-repo URL, back when the console cloned and pushed for itself."""
    from stigmergy.capture.evidence import MemoryEvidenceStore
    return AdminService(conn, server_settings=Settings(), admin_settings=admin_settings,
                        evidence=MemoryEvidenceStore())


def test_repairs_list_carries_every_outcome_and_the_whole_tables_counts(conn, service):
    """Both halves, and the second is not decoration: `recent` is a bounded PAGE of a table that
    only grows, and `counts` is the whole of it. A surface drawing a part-to-whole from the page
    would understate history the moment the page fills — which is exactly what an operator asking
    "how much has this loop done" would be reading."""
    applied_id = landed_repair(conn, path="wiki/notes/Renewals.md")
    failed_id = refused_repair(conn)

    listed = service.repairs_list()

    assert [row["id"] for row in listed["recent"]] == [failed_id, applied_id], "newest first"
    assert listed["recent"][1]["target_paths"] == ["wiki/notes/Renewals.md"]
    assert listed["recent"][1]["ops"][0]["op"] == "backlink"
    assert listed["counts"] == {repair_schema.STATUS_APPLIED: 1, repair_schema.STATUS_FAILED: 1,
                                repair_schema.STATUS_SKIPPED: 0}
    assert listed["recent_limit"] == admin_service.REPAIR_RECENT_LIMIT
    assert isinstance(listed["recent"][0]["created_at"], str), (
        "datetimes cross the wire as ISO strings")


def test_an_applied_repair_reaches_the_console_carrying_the_diff_nobody_read_first(conn, service):
    """The column this page exists for. Nobody read the change before it was pushed, so the stored
    diff IS the reading — a console that listed paths alone would be offering a summary of prose a
    model wrote, which is `entity-body`'s own mistake made at the level of the whole table."""
    repair_id = landed_repair(conn)

    row = service.repair_show(repair_id)

    assert row["status"] == repair_schema.STATUS_APPLIED
    assert row["applied_commit"] == LANDED_COMMIT
    assert row["diff"].startswith("diff --git")
    assert "\n" in row["diff"], (
        "a diff flattened to one line is not a diff anybody can read")


def test_a_failed_repair_reaches_the_console_carrying_the_sentence_that_refused_it(conn, service):
    """The other outcome, and the only place it is ever explained: a `failed` row is never retried,
    so `error` is the whole of what anybody will ever know about why that finding stopped being
    answered."""
    repair_id = refused_repair(conn)

    row = service.repair_show(repair_id)

    assert row["status"] == repair_schema.STATUS_FAILED
    assert "the gates refused this repair" in row["error"]
    assert (row["applied_commit"], row["diff"]) == ("", "")


def test_repair_show_sanitizes_every_untrusted_string_and_404s_on_nothing(conn, service):
    """A rationale and a note were written by a model that had just read pages somebody else wrote,
    a path is a filename somebody chose, and a DIFF is page bytes. Control characters die at the
    server; HTML inertness is the client's half.

    `\\x07`/`\\x1b`, not `\\x00`: Postgres refuses a NUL in a text column outright, so the byte this
    console has to strip is the one that CAN be stored — an escape sequence a terminal would act on
    and a browser would render as nothing."""
    repair_id = landed_repair(conn, kind="overlap", note="covers the same\x07 ground",
                              rationale="the two pages\x1b[2J overlap")

    row = service.repair_show(repair_id)

    assert row["rationale"] == "the two pages[2J overlap"
    assert row["ops"][0]["note"] == "covers the same ground"
    with pytest.raises(AdminNotFound):
        service.repair_show(999_999)


def test_a_failed_repairs_sentence_is_sanitized_like_everything_else(conn, service):
    """`error` is composed from gate codes and repo-relative paths and then STORED, so it reaches
    this page by exactly the same road as a model's rationale: it is text nobody vetted, rendered
    in a browser."""
    repair_id = refused_repair(conn, error="the gates refused it\x1b[2J (zone/outside-lane)")

    assert service.repair_show(repair_id)["error"] == (
        "the gates refused it[2J (zone/outside-lane)")


def test_a_body_draft_reaches_the_console_readable_and_whole(conn, service):
    """OLD BEHAVIOUR: `_repair` reshaped every op into `{op, path, link, note}`, so an
    `entity-body` op arrived at the console with its `body_markdown` and `role` DROPPED — the
    drafted prose is the only thing there is to read for this kind, and the console showed a row
    with an empty `link` where the draft should have been.

    Newlines survive `_clean` by design (control characters die, structure does not): a body
    flattened to one line is a body nobody can read as the page it became."""
    repair_id = landed_entity_body(conn, body="## What / Who\n\nA freight\x07 broker.\n",
                                   role="A freight broker in the north-west.")

    row = service.repair_show(repair_id)

    assert row["kind"] == repair_schema.KIND_ENTITY_BODY
    assert row["ops"][0]["body_markdown"] == "## What / Who\n\nA freight broker.\n"
    assert row["ops"][0]["role"] == "A freight broker in the north-west."


def test_an_additive_op_keeps_exactly_the_fields_it_had(conn, service):
    """The benign twin for the reshaping change: a second kind's fields must not appear on the
    first kind's ops, where a console table would render an empty column for every repair."""
    repair_id = landed_repair(conn, kind="overlap", note="the same ground")

    (op,) = service.repair_show(repair_id)["ops"]

    assert sorted(op) == ["link", "note", "op", "path"]


def test_a_deletion_reaches_the_console_with_the_prose_that_landed(conn, service):
    """The third kind's shape. A DELETE op is a path and nothing else — which page stopped existing
    is the whole of it — and a SCRUB op carries its `planned_after` through, because those bytes are
    a MODEL's prose (ADR 043) and this page is the only reading they get.

    Red before that: the console showed two path lists, so model-written bodies were invisible —
    `entity-body`'s own mistake, which that kind's renderer exists to avoid."""
    repair_id = landed_delete(conn)

    row = service.repair_show(repair_id)

    assert row["kind"] == repair_schema.KIND_DELETE
    assert [op["op"] for op in row["ops"]] == [repair_schema.DELETE_OP_NAME,
                                               repair_schema.SCRUB_OP_NAME]
    assert sorted(row["ops"][0]) == ["op", "path"]
    assert sorted(row["ops"][1]) == ["op", "path", "planned_after"]
    assert "No link any more" in row["ops"][1]["planned_after"]
    assert "\n" in row["ops"][1]["planned_after"], (
        "a page flattened to one line is a page nobody can read as the page it became")


# ── the console's one surviving mutation: a person's own deletion ─────────────────────────────
def test_pages_delete_queues_through_the_shared_seam_and_records_the_console_as_the_door(
        conn, deletion_service):
    """The console's deletion door (ADR 043 D2, ADR 044 D3). It calls
    `server.review.queue_deletion` — the SAME seam MCP's `brain_delete` calls — and hands in NO
    authorization: its token is the authorization, and this is the one door where that is the whole
    of it. Asserted against the REAL queue rather than a replaced sequence: the row is what the
    worker will act on, so a double here would prove nothing about what gets removed."""
    result = deletion_service.pages_delete(actor="ops@example.com",
                                           paths=["wiki/notes/Old Memo.md"], why="superseded")

    assert result["status"] == capture_schema.QUEUED
    with conn.cursor() as cur:
        cur.execute("SELECT kind, submitted_by, hints, payload FROM capture_queue WHERE id = %s",
                    (result["id"],))
        kind, by, hints, payload = cur.fetchone()
    assert kind == capture_schema.DELETE
    assert by == "ops@example.com"
    assert capture_schema.delete_paths(hints) == ["wiki/notes/Old Memo.md"]
    assert hints["client"]["delete_source"] == "admin", (
        "which door a person removed from changes this field and nothing else")
    assert payload["text"] == "superseded"
    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == (
        "ops@example.com", "pages.delete", "ok")
    assert "superseded" not in str(recorded["args"]), (
        "the reason is free text a person wrote: `admin_actions` keeps its LENGTH, not the words")


def test_a_refused_deletion_records_the_real_class_before_it_becomes_AdminRefused(
        conn, deletion_service):
    """`_mutate`'s ordering, on this door too: the console's own log keeps the library's exception
    class, and the caller gets the sentence as a refusal rather than a 500. Driven by a REAL
    refusal — an entity page, which the queueing seam refuses by name — so the class recorded is
    the one a real operator's mistake would record."""
    with pytest.raises(AdminRefused, match="identity is retired"):
        deletion_service.pages_delete(actor="ops@example.com",
                                      paths=["wiki/entities/Acme Corp.md"], why="stale")

    recorded = _actions(conn)[0]
    assert (recorded["action"], recorded["outcome"], recorded["error_class"]) == (
        "pages.delete", "error", "SubmissionRejected")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0, "refused at the seam means no row and no blob"


def test_pages_delete_without_an_evidence_store_is_a_refusal_naming_it(conn, service):
    """The plain `service` fixture has no evidence store, which is the deployment shape a console
    served by a process whose object store is unreachable is in. The refusal is a `ReviewError`, so
    `_mutate`'s own `CaptureError` branch maps it to the routes' 409 with the library's own
    sentence — never a 500 naming a class."""
    with pytest.raises(AdminRefused, match="evidence store"):
        service.pages_delete(actor="ops@example.com", paths=["wiki/notes/Old Memo.md"],
                             why="superseded")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0
