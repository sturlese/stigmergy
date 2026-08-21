"""AdminService over the real queue/tables (stigmergy_test), and over a real bare knowledge repo
for the decisions: every mutation lands through the SAME library seams the CLIs and the review
lane use, so what these prove is parity, not a parallel implementation."""
import asyncio
import json
import os
import subprocess

import pytest

from stigmergy.admin import schema as admin_schema
from stigmergy.admin import service as admin_service
from stigmergy.admin.service import (
    CRON_WORKFLOWS,
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
from stigmergy.librarian import gitcmd
from stigmergy.repair import remote as repair_remote
from stigmergy.repair import schema as repair_schema
from stigmergy.repair import store as repair_store
from stigmergy.repair.errors import RepairError
from stigmergy.review_kinds import KIND_IDENTITY_PROPOSAL
from stigmergy.server import review as server_review
from stigmergy.server.settings import Settings
from tests.admin.conftest import (
    finish_one,
    propose_delete,
    propose_entity_body,
    propose_identity,
    propose_repair,
    publish_registry,
    register_entity,
    remote_files,
    remote_registry,
    submit_one,
)


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
# ── the proposals: list, detail and the three decisions, through the governed door ───────────
@pytest.fixture()
def entity_service(conn, admin_settings, entity_mint_repo):
    """`AdminService` pointed at a real, throwaway bare knowledge repo — for the tests that land a
    decision for real. The validation-only tests use the plain `service` fixture instead (no repo
    configured): they never reach git at all, and proving that is part of what they pin."""
    return AdminService(conn, server_settings=Settings(librarian_repo_url=entity_mint_repo),
                        admin_settings=admin_settings)


def test_entities_list_carries_the_proposals_and_their_registry_verdict(conn, entity_service,
                                                                        entity_mint_repo):
    """The list is the inbox's own read of the two proposal kinds, each identity checked against
    the REST of the registry — a proposal always resolves to itself, which says nothing, so the
    check leaves it out. `Acme Corporation`, a spelling `Acme Corp` already lists, comes back
    REGISTERED: the Merge picker's strongest hint."""
    register_entity(entity_mint_repo, conn, "Acme Corp", aliases=["Acme Corporation"])
    propose_identity(entity_mint_repo, conn, "Acme Corporation")
    propose_identity(entity_mint_repo, conn, "Vandelay Imports")
    register_entity(entity_mint_repo, conn, "Initech", proposed_aliases=["Initech Ltd"])

    listed = entity_service.entities_list()

    by_id = {p["id"]: p for p in listed["proposals"]}
    assert set(by_id) == {"acme-corporation", "vandelay-imports"}
    assert by_id["acme-corporation"]["check"]["verdict"] == admin_service.VERDICT_REGISTERED
    assert by_id["acme-corporation"]["check"]["match"]["id"] == "acme-corp"
    assert by_id["acme-corporation"]["merge_candidates"] == [{"id": "acme-corp", "name": "Acme Corp"}]
    assert by_id["vandelay-imports"]["check"]["verdict"] == admin_service.VERDICT_CLEAR
    assert [(a["entity_id"], a["alias"]) for a in listed["aliases"]] == [("initech", "Initech Ltd")]
    assert listed["registry_check"]["road"] == "snapshot"


def test_the_inbox_reads_the_registry_file_when_the_index_holds_no_snapshot(conn, admin_settings,
                                                                           entity_mint_repo, tmp_path):
    """The console's inbox derives its proposals from the registry the console SERVES — the
    snapshot where the index has one, the `--entity-registry` file where it does not (the local
    recipe, and any stack before its first webhook). Before this test the inbox read the snapshot
    alone while the Entities desk read either: on a stack with no snapshot the browser listed every
    entity and the inbox listed no proposal, and the two pages disagreed about what was waiting."""
    propose_identity(entity_mint_repo, conn, "Vandelay Imports")
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    registry_file = tmp_path / "entity-registry.json"
    registry_file.write_text(subprocess.run(["git", "show", "main:ops/entity-registry.json"],
                                            cwd=entity_mint_repo, capture_output=True, text=True,
                                            check=True).stdout, encoding="utf-8")
    file_road = AdminService(conn, server_settings=Settings(entity_registry_path=str(registry_file)),
                             admin_settings=admin_settings)

    inbox = file_road.inbox()
    listed = file_road.entities_list()

    assert [i["name"] for i in inbox["items"] if i["kind"] == KIND_IDENTITY_PROPOSAL] == ["Vandelay Imports"]
    assert [p["name"] for p in listed["proposals"]] == ["Vandelay Imports"]
    assert listed["registry_check"]["road"] == "file"


def test_entities_show_returns_the_proposal_and_404s_on_a_name_nobody_proposed(conn, entity_service,
                                                                              entity_mint_repo):
    propose_identity(entity_mint_repo, conn, "Globex Robotics")
    shown = entity_service.entities_show("globex-robotics")
    assert shown["name"] == "Globex Robotics" and shown["kind"] == "identity-proposal"
    with pytest.raises(AdminNotFound):
        entity_service.entities_show("ghost")


def test_entity_decide_approve_lands_for_real_and_records_both_ledgers(
        conn, entity_service, entity_mint_repo, require_gitleaks):
    """The end-to-end proof, admin's own: ONE commit lands on the real bare remote, the append-only
    `review_decisions` ledger records the decision under the SAME kind and id the librarian reads,
    and `admin_actions` records the attempt under the actor's name. `extra.source` names this
    door; the App authors the commit and the `Decided-by:` trailer carries the console's free-text
    actor — attribution, not a resolved identity (ADR 030 D2)."""
    entity_id = propose_identity(entity_mint_repo, conn, "Globex Robotics")

    result = entity_service.entity_decide("identity-proposal", entity_id,
                                         actor="steward@example.com", verdict="approve")

    assert result["recorded"] == "approve" and len(result["commit"]) == 40
    entry = remote_registry(entity_mint_repo)[entity_id]
    assert entry["proposed"] is False and entry["approved_by"] == "steward@example.com"
    with conn.cursor() as cur:
        cur.execute("SELECT item_kind, item_id, verdict, actor, extra FROM review_decisions")
        [(kind, item_id, verdict, actor, extra)] = cur.fetchall()
    assert (kind, item_id, verdict, actor) == ("identity-proposal", entity_id, "approve",
                                              "steward@example.com")
    assert extra == {"source": "admin", "commit": result["commit"]}
    author = gitcmd.run("log", "-1", "--format=%an <%ae>", result["commit"],
                        cwd=entity_mint_repo).stdout.strip()
    assert author == "stigmergy-librarian <stigmergy-librarian@users.noreply.github.com>"
    message = gitcmd.run("log", "-1", "--format=%B", result["commit"], cwd=entity_mint_repo).stdout
    assert "Decided-by: steward@example.com" in message
    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == (
        "steward@example.com", "entities.decide", "ok")


def test_entity_decide_merge_folds_the_proposal_and_records_where_it_went(
        conn, entity_service, entity_mint_repo, require_gitleaks):
    register_entity(entity_mint_repo, conn, "Acme Corp")
    entity_id = propose_identity(entity_mint_repo, conn, "Acme Corporation", aliases=["ACME Co"])

    result = entity_service.entity_decide("identity-proposal", entity_id, actor="marc",
                                         verdict="merge", into="acme-corp")

    assert result["recorded"] == "merge" and result["into"] == "acme-corp"
    assert result["reanchored"] == ["wiki/notes/Acme Corporation kickoff.md"]
    registry = remote_registry(entity_mint_repo)
    assert entity_id not in registry
    assert {"Acme Corporation", "ACME Co"} <= set(registry["acme-corp"]["aliases"])
    with conn.cursor() as cur:
        cur.execute("SELECT verdict, extra FROM review_decisions WHERE item_id = %s", (entity_id,))
        verdict, extra = cur.fetchone()
    assert verdict == "merge" and extra["into"] == "acme-corp"


def test_entity_decide_decline_removes_the_page_and_records_the_reject_the_librarian_reads(
        conn, entity_service, entity_mint_repo, require_gitleaks):
    entity_id = propose_identity(entity_mint_repo, conn, "Globex Robotics")

    result = entity_service.entity_decide("identity-proposal", entity_id, actor="marc",
                                         verdict="decline", notes="a typo\x1b[31m for Globex")

    assert result["recorded"] == "reject"
    assert "wiki/entities/Globex Robotics.md" not in remote_files(entity_mint_repo)
    assert entity_id not in remote_registry(entity_mint_repo)
    with conn.cursor() as cur:
        cur.execute("SELECT verdict, notes FROM review_decisions WHERE item_id = %s", (entity_id,))
        verdict, notes = cur.fetchone()
    assert verdict == "reject"
    assert "\x1b" not in notes and "for Globex" in notes, "the note is cleaned below the console"


def test_entity_decide_on_a_proposed_spelling(conn, entity_service, entity_mint_repo,
                                              require_gitleaks):
    register_entity(entity_mint_repo, conn, "Initech", proposed_aliases=["Initech Ltd", "ITC"])

    approved = entity_service.entity_decide("alias-proposal", "initech:Initech Ltd", actor="marc",
                                            verdict="approve")
    publish_registry(entity_mint_repo, conn)
    declined = entity_service.entity_decide("alias-proposal", "initech:ITC", actor="marc",
                                            verdict="decline")

    assert approved["recorded"] == "approve" and declined["recorded"] == "reject"
    entry = remote_registry(entity_mint_repo)["initech"]
    assert entry["aliases"] == ["Initech Ltd"] and entry["proposed_aliases"] == []


def test_entity_decide_bad_requests_are_refused_before_anything_is_attempted(conn, service):
    with pytest.raises(AdminBadRequest, match="item_kind"):
        service.entity_decide("parked-capture", "7", actor="marc", verdict="requeue")
    with pytest.raises(AdminBadRequest, match="verdict for identity-proposal"):
        service.entity_decide("identity-proposal", "x", actor="marc", verdict="requeue")
    with pytest.raises(AdminBadRequest, match="merge needs `into`"):
        service.entity_decide("identity-proposal", "x", actor="marc", verdict="merge")
    assert _actions(conn) == [], "a bad request is refused before the action is even recorded"


def test_entity_decide_without_a_repo_url_is_refused_with_the_capability_sentence(conn, service):
    """`service` carries a default `Settings()` — no `librarian_repo_url` — so the door refuses
    by name, after recording the attempt, and no ledger row is written."""
    with pytest.raises(AdminRefused, match="STIGMERGY_LIBRARIAN_REPO_URL"):
        service.entity_decide("identity-proposal", "globex-robotics", actor="marc",
                              verdict="approve")
    recorded = _actions(conn)[0]
    assert recorded["outcome"] == "error"
    assert recorded["error_class"] == "CapabilityUnavailableError"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0


def test_entity_decide_on_an_unknown_proposal_is_the_librarys_own_refusal(
        conn, entity_service, entity_mint_repo, require_gitleaks):
    with pytest.raises(AdminRefused, match="no entity 'ghost'"):
        entity_service.entity_decide("identity-proposal", "ghost", actor="marc", verdict="approve")
    assert _actions(conn)[0]["error_class"] == "EntityError"


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
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0, "the ledger row is the worker's to write, after the push"
    assert by == "steward@example.com"
    registration = capture_schema.registration_from_hints(hints)
    assert registration.name == "Stark Industries" and set(registration.aliases) == {"Stark", "SI"}
    assert registration.source == server_review.SOURCE_ADMIN
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

    # The cron files are TEMPLATES an operator copies into their knowledge repo, so they
    # live outside `.github/workflows/` (a file there is registered by GitHub whether enabled or
    # not, and a column of "Disabled" rows on a public Actions tab reads as a broken project). The
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


# ── repairs: read, approve, decline (ADR 039) ─────────────────────────────────────────────────
# The console is the SECOND door onto the same governed apply the review lane drives. What these
# pin is the half that is this package's own — the `admin_actions` bookkeeping, the error mapping,
# the sanitizing — while the ordering itself belongs to `server.review.apply_repair_and_record`
# and is proven there against real state.
FAKE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _apply_records(monkeypatch, paths=("wiki/notes/Renewals.md",)):
    """`apply_via_clone` replaced by a recorder, patched as a MODULE ATTRIBUTE — the seam
    `repair.remote.apply_approved` keeps by calling it under that name. Everything around it
    (`mark_decided`, `mark_applied`, the ledger row) is the real thing."""
    calls = []

    def fake(repo_url, branch, credential, *, proposal, approved_by, on_output=None,
             prepared=None):
        calls.append({"repo_url": repo_url, "approved_by": approved_by, "proposal": proposal})
        return {"commit": FAKE_COMMIT, "paths": list(paths)}

    monkeypatch.setattr(repair_remote, "apply_via_clone", fake)
    return calls


@pytest.fixture()
def repair_service(conn, admin_settings):
    """`AdminService` with a knowledge-repo URL configured. The URL is never dialled in these
    tests — `apply_via_clone` is the recorder above — but it has to be non-empty, because the
    shared sequence refuses an unconfigured deployment BEFORE the proposal moves out of pending."""
    return AdminService(conn, server_settings=Settings(librarian_repo_url="/tmp/not-dialled.git"),
                        admin_settings=admin_settings)


def test_repairs_list_carries_the_pending_and_the_decided_halves(conn, service, monkeypatch):
    """Both halves, and the second is not decoration: a rejected row is the dismissal memory the
    proposer skips against, so "why does the nightly run not propose this any more" is only
    answerable from the decided list."""
    pending_id = propose_repair(conn, path="wiki/notes/Renewals.md")
    declined_id = propose_repair(conn, path="wiki/decisions/Refunds.md")
    assert repair_store.mark_decided(conn, declined_id, status=repair_schema.STATUS_REJECTED,
                                     decided_by="steward@example.com", notes="already linked")

    listed = service.repairs_list()

    assert [row["id"] for row in listed["pending"]] == [pending_id]
    assert listed["pending"][0]["target_paths"] == ["wiki/notes/Renewals.md"]
    assert listed["pending"][0]["ops"][0]["op"] == "backlink"
    decided = listed["recent"][0]
    assert (decided["id"], decided["status"], decided["notes"]) == (
        declined_id, repair_schema.STATUS_REJECTED, "already linked")
    assert isinstance(decided["decided_at"], str), "datetimes cross the wire as ISO strings"


def test_repair_show_sanitizes_every_untrusted_string_and_404s_on_nothing(conn, service):
    """A rationale and a note were written by a model that had just read pages somebody else wrote,
    and a path is a filename somebody chose. Control characters die at the server; HTML inertness
    is the client's half.

    `\\x07`/`\\x1b`, not `\\x00`: Postgres refuses a NUL in a text column outright, so the byte this
    console has to strip is the one that CAN be stored — an escape sequence a terminal would act on
    and a browser would render as nothing."""
    proposal_id = propose_repair(conn, kind="overlap", note="covers the same\x07 ground",
                                 rationale="the two pages\x1b[2J overlap")

    row = service.repair_show(proposal_id)

    assert row["rationale"] == "the two pages[2J overlap"
    assert row["ops"][0]["note"] == "covers the same ground"
    with pytest.raises(AdminNotFound):
        service.repair_show(999_999)


def test_a_body_draft_reaches_the_console_readable_and_whole(conn, service):
    """OLD BEHAVIOUR: `_proposal` reshaped every op into `{op, path, link, note}`, so an
    `entity-body` op arrived at the console with its `body_markdown` and `role` DROPPED — the
    steward reading the draft is the check for this kind, and the console showed them a row with
    an empty `link` where the draft should have been.

    Newlines survive `_clean` by design (control characters die, structure does not): a body
    flattened to one line is a body nobody can read as the page it would become."""
    proposal_id = propose_entity_body(conn, body="## What / Who\n\nA freight\x07 broker.\n",
                                      role="A freight broker in the north-west.")

    row = service.repair_show(proposal_id)

    assert row["kind"] == repair_schema.KIND_ENTITY_BODY
    assert row["ops"][0]["body_markdown"] == "## What / Who\n\nA freight broker.\n"
    assert row["ops"][0]["role"] == "A freight broker in the north-west."


def test_an_additive_op_keeps_exactly_the_fields_it_had(conn, service):
    """The benign twin for the reshaping change: a second kind's fields must not appear on the
    first kind's ops, where a console table would render an empty column for every repair."""
    proposal_id = propose_repair(conn, kind="overlap", note="the same ground")

    (op,) = service.repair_show(proposal_id)["ops"]

    assert sorted(op) == ["link", "note", "op", "path"]


def test_repair_approve_applies_and_records_both_ledgers(conn, repair_service, monkeypatch):
    """The console's own half: an `admin_actions` row naming this door, and — through the shared
    sequence — the `review_decisions` row that answers "who approved this change to the corpus"
    identically whichever door was used."""
    calls = _apply_records(monkeypatch)
    proposal_id = propose_repair(conn)

    result = repair_service.repair_approve(proposal_id, actor="steward@example.com")

    assert result == {"applied": True, "commit": FAKE_COMMIT,
                      "paths": ["wiki/notes/Renewals.md"]}
    assert [c["approved_by"] for c in calls] == ["steward@example.com"]
    row = repair_store.proposal(conn, proposal_id)
    assert (row["status"], row["applied_commit"]) == (repair_schema.STATUS_APPLIED, FAKE_COMMIT)
    decision = server_review.latest_decisions(conn)[
        (server_review.KIND_REPAIR_PROPOSAL, str(proposal_id))]
    assert (decision["verdict"], decision["source"]) == ("approve", server_review.SOURCE_ADMIN)
    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == (
        "steward@example.com", "repairs.approve", "ok")


def test_pages_delete_runs_the_shared_sequence_and_records_the_console_as_the_door(
        conn, repair_service, monkeypatch):
    """The console's deletion door (ADR 043 D2). It reaches `server.review.delete_and_record` — the
    SAME sequence MCP's `brain_delete` runs — and hands in NO authorization: its token is the
    authorization, exactly as `repair_approve` and `entity_approve` do. So what this asserts is the
    wiring and the two bookkeeping rows, not a second copy of the sequence's own behaviour
    (`tests/server/test_delete_pages_pg.py` proves that against a real remote)."""
    seen = {}

    def fake(conn_arg, *, repo_url, paths, why, actor, source, authorize=None):
        seen.update({"repo_url": repo_url, "paths": list(paths), "why": why, "actor": actor,
                     "source": source, "authorize": authorize})
        return {"deleted": list(paths), "rewritten": {}, "commit": FAKE_COMMIT,
                "proposal_id": 7, "model_calls": 0, "message": "done"}

    monkeypatch.setattr(server_review, "delete_and_record", fake)

    result = repair_service.pages_delete(actor="ops@example.com",
                                         paths=["wiki/notes/Old Memo.md"], why="superseded")

    assert result["commit"] == FAKE_COMMIT
    assert seen["paths"] == ["wiki/notes/Old Memo.md"]
    assert (seen["actor"], seen["source"]) == ("ops@example.com", server_review.SOURCE_ADMIN)
    assert seen["authorize"] is None, (
        "the console passes no steward guard — its token is the authorization (ADR 029/030 D2)")
    recorded = _actions(conn)[0]
    assert (recorded["actor"], recorded["action"], recorded["outcome"]) == (
        "ops@example.com", "pages.delete", "ok")
    assert "superseded" not in str(recorded["args"]), (
        "the reason is free text a person wrote: `admin_actions` keeps its LENGTH, not the words")


def test_a_refused_deletion_records_the_real_class_before_it_becomes_AdminRefused(
        conn, repair_service, monkeypatch):
    """`_mutate`'s ordering, on this door too: the console's own log keeps the library's exception
    class, and the caller gets the sentence as a refusal rather than a 500."""
    def refuse(*_a, **_k):
        raise server_review.ReviewError("wiki/entities/Acme Corp.md is an entity page")

    monkeypatch.setattr(server_review, "delete_and_record", refuse)

    with pytest.raises(AdminRefused, match="entity page"):
        repair_service.pages_delete(actor="ops@example.com",
                                    paths=["wiki/entities/Acme Corp.md"], why="stale")

    recorded = _actions(conn)[0]
    assert (recorded["action"], recorded["outcome"], recorded["error_class"]) == (
        "pages.delete", "error", "ReviewError")


def test_repair_approve_maps_a_refusal_to_AdminRefused_after_recording_the_real_class(
        conn, repair_service, monkeypatch):
    """The mapping order `entity_approve` established: `_mutate` sees the LIBRARY's exception and
    records its class name, and only then does the caller get `AdminRefused` with the library's own
    sentence. Renaming it inside `_do` would rename what the row already captured."""
    def refuse(*_a, **_k):
        raise RepairError("the gates refused this repair, so nothing was committed or pushed")

    monkeypatch.setattr(repair_remote, "apply_via_clone", refuse)
    proposal_id = propose_repair(conn)

    with pytest.raises(AdminRefused, match="the gates refused this repair"):
        repair_service.repair_approve(proposal_id, actor="steward@example.com")

    assert _actions(conn)[0]["error_class"] == "RepairError"
    row = repair_store.proposal(conn, proposal_id)
    assert row["status"] == repair_schema.STATUS_FAILED, "a failed apply stays visible as failed"
    assert "the gates refused" in row["error"]


def test_repair_approve_without_a_configured_repo_refuses_before_the_proposal_moves(
        conn, service, monkeypatch):
    """The plain `service` fixture has no `librarian_repo_url`. The refusal is a `ReviewError`, so
    `_mutate`'s own `CaptureError` branch maps it — and the proposal is untouched, because the
    check runs before `mark_decided`."""
    def never(*_a, **_k):
        raise AssertionError("apply_via_clone ran on a deployment with no knowledge-repo URL")

    monkeypatch.setattr(repair_remote, "apply_via_clone", never)
    proposal_id = propose_repair(conn)

    with pytest.raises(AdminRefused, match="STIGMERGY_LIBRARIAN_REPO_URL"):
        service.repair_approve(proposal_id, actor="steward@example.com")

    assert repair_store.proposal(conn, proposal_id)["status"] == repair_schema.STATUS_PENDING


def test_repair_reject_records_the_dismissal_on_the_row_and_in_the_ledger(conn, service):
    proposal_id = propose_repair(conn)

    assert service.repair_reject(proposal_id, actor="steward@example.com",
                                 reason="the two pages describe different quarters")

    row = repair_store.proposal(conn, proposal_id)
    assert (row["status"], row["decided_by"]) == (repair_schema.STATUS_REJECTED,
                                                  "steward@example.com")
    assert row["notes"] == "the two pages describe different quarters"
    assert row["content_key"] in repair_store.known_content_keys(conn)
    decision = server_review.latest_decisions(conn)[
        (server_review.KIND_REPAIR_PROPOSAL, str(proposal_id))]
    assert (decision["verdict"], decision["source"]) == ("reject", server_review.SOURCE_ADMIN)
    assert _actions(conn)[0]["action"] == "repairs.reject"


def test_deciding_a_proposal_twice_is_refused_and_the_first_decision_stands(conn, service):
    """`mark_decided`'s conditional UPDATE, seen from this door: the second decline loses and is
    told so, rather than overwriting the reason the first steward gave."""
    proposal_id = propose_repair(conn)
    service.repair_reject(proposal_id, actor="first@example.com", reason="already linked")

    with pytest.raises(AdminRefused, match="no longer pending"):
        service.repair_reject(proposal_id, actor="second@example.com", reason="disagree")

    assert repair_store.proposal(conn, proposal_id)["decided_by"] == "first@example.com"


def test_repair_approve_and_reject_404_on_a_proposal_that_does_not_exist(conn, repair_service):
    for call in (lambda: repair_service.repair_approve(999_999, actor="x"),
                 lambda: repair_service.repair_reject(999_999, actor="x", reason="no")):
        with pytest.raises(AdminNotFound):
            call()
    assert _actions(conn) == [], "a 404 is not an attempted mutation — no admin_actions row"


def test_a_deletion_reaches_the_console_with_the_prose_a_steward_has_to_read(conn, service):
    """The third kind's shape. A DELETE op is a path and nothing else — which page stops existing
    is the whole of it — and a SCRUB op carries its `planned_after` through, because since ADR 043
    those bytes are a MODEL's prose and this is the only reading they get before they land.

    Red before that: the console showed two path lists, so a steward approved model-written bodies
    they could not see anywhere — `entity-body`'s own mistake, which that kind's renderer exists to
    avoid."""
    proposal_id = propose_delete(conn)

    row = service.repair_show(proposal_id)

    assert row["kind"] == repair_schema.KIND_DELETE
    assert [op["op"] for op in row["ops"]] == [repair_schema.DELETE_OP_NAME,
                                               repair_schema.SCRUB_OP_NAME]
    assert sorted(row["ops"][0]) == ["op", "path"]
    assert sorted(row["ops"][1]) == ["op", "path", "planned_after"]
    assert "No link any more" in row["ops"][1]["planned_after"]
    assert "\n" in row["ops"][1]["planned_after"], (
        "a page flattened to one line is a page nobody can read as the page it would become")
