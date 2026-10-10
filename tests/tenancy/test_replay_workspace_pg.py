"""Refiling a workspace into a fresh one: the export carries layers, people, and landed captures
whose sources nobody deleted; the import queues each capture once, in order, under a derived id,
into a workspace that holds nothing else; the handover moves the people's live tokens to the new
workspace once every capture landed there, and back."""
import pytest

from stigmergy.capture import references
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.kernel import tenancy
from stigmergy.ops import replay_workspace as replay
from stigmergy.server import platform, roster
from tests import testdb
from tests.tenancy.conftest import acting, clear

ANA = Actor(subject="ana@example.com", display_name="Ana")
OPERATOR = "ops@example.com"
SOURCE = "00000000-0000-4000-8000-000000000009"
SOURCE_SLUG = "replay-source"
TARGET = "00000000-0000-4000-8000-000000000010"
TWIN_SLUG = "replay-target"


def _fresh(conn, tenant: str, slug: str) -> None:
    testdb.ensure_tenant(conn, tenant, slug)
    clear(conn, tenant)
    with acting(conn, tenant), conn.cursor() as cur:
        roster.seed_tenant(conn)
        cur.execute("DELETE FROM roster_tokens")
        cur.execute("DELETE FROM roster_principals")


@pytest.fixture()
def twin(conn):
    _fresh(conn, TARGET, TWIN_SLUG)
    yield TARGET
    clear(conn, TARGET)
    testdb.ensure_tenant(conn, TARGET, TWIN_SLUG, status="suspended")


@pytest.fixture()
def source(conn, twin):
    _fresh(conn, SOURCE, SOURCE_SLUG)
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute("INSERT INTO platform_operators (subject, display_name) VALUES (%s, 'Ops') "
                    "ON CONFLICT DO NOTHING", (OPERATOR,))
    yield SOURCE
    clear(conn, SOURCE)
    testdb.ensure_tenant(conn, SOURCE, SOURCE_SLUG, status="suspended")


def _source(conn) -> tuple[list[str], str]:
    store = MemoryEvidenceStore()
    with acting(conn, SOURCE):
        if "finance" not in roster.known_groups(conn):
            roster.create_group(conn, "finance", "Money matters", actor="test")
        roster.create_principal(conn, {"subject": ANA.subject, "display_name": "Ana", "kind": "person",
                                       "is_admin": False, "groups": ["finance"],
                                       "default_audience": ["finance"]}, actor="test")
        token = roster.issue_token(conn, ANA.subject, "laptop", actor="test").token
        ids = [str(CaptureService(conn, store).capture_text(actor=ANA, audience=("finance",), adapter="mcp",
                                                            text=f"Ledger {n} closed.", idempotency_key=f"l{n}")["id"])
               for n in range(4)]
        with conn.cursor() as cur:
            cur.execute("UPDATE capture_queue SET status = 'landed' WHERE id = ANY(%s::uuid[])", (ids,))
        ref = "sha256/00/00/" + "0" * 64
        cur = conn.cursor()
        cur.execute("INSERT INTO capture_artifacts (capture_id, position, original_ref, readable_ref, source_path) "
                    "VALUES (%s, 1, %s, %s, 'sources/2026/10/deleted.md')", (ids[3], ref, ref))
        references.release_sources(conn, {"sources/2026/10/deleted.md"})
    return ids, token


def _land_target(conn, twin):
    with acting(conn, twin), conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = 'landed'")


def test_a_workspace_refiles_into_another_and_hands_its_people_over(conn, twin, source):
    ids, token = _source(conn)
    exported = replay.export(conn, SOURCE_SLUG, operator=OPERATOR)
    assert [item["id"] for item in exported["captures"]] == ids[:3]
    assert exported["excluded"] == {"deleted_sources": 1}
    assert {"name": "finance", "description": "Money matters"} in exported["groups"]

    order = list(reversed(ids[:3]))
    confirm = f"{SOURCE_SLUG}:{TWIN_SLUG}"
    assert replay.import_(conn, TWIN_SLUG, exported, operator=OPERATOR, confirm=confirm, order=order) == {
        "workspace": TWIN_SLUG, "queued": 3, "already_queued": 0}
    assert replay.import_(conn, TWIN_SLUG, exported, operator=OPERATOR, confirm=confirm,
                          order=order)["already_queued"] == 3
    with acting(conn, twin), conn.cursor() as cur:
        cur.execute("SELECT id, idempotency_key, acl FROM capture_queue ORDER BY created_at, id")
        assert [(str(r[0]), r[1], r[2]) for r in cur.fetchall()] == [
            (str(replay.replay_id(twin, original)), f"replay-{original}", ["finance"]) for original in order]
        assert roster.principal(conn, ANA.subject).groups == ("finance",)

    with pytest.raises(replay.ReplayRefused, match="still filing"):
        replay.handover(conn, SOURCE_SLUG, TWIN_SLUG, operator=OPERATOR, confirm=confirm)
    _land_target(conn, twin)
    moved = replay.handover(conn, SOURCE_SLUG, TWIN_SLUG, operator=OPERATOR, confirm=confirm)
    assert (moved["tokens"], moved["unlanded"], moved["suspended"]) == (1, 0, SOURCE_SLUG)
    assert str(roster.principal_for_token(conn, token)[0].tenant) == twin

    testdb.ensure_tenant(conn, SOURCE, SOURCE_SLUG)
    back = replay.handover(conn, TWIN_SLUG, SOURCE_SLUG, operator=OPERATOR, confirm=f"{TWIN_SLUG}:{SOURCE_SLUG}")
    assert back["tokens"] >= 1
    testdb.ensure_tenant(conn, twin, TWIN_SLUG)
    assert str(roster.principal_for_token(conn, token)[0].tenant) == SOURCE


def test_an_import_refuses_a_wrong_confirmation_a_partial_order_and_a_used_workspace(conn, twin, source):
    ids, _token = _source(conn)
    exported = replay.export(conn, SOURCE_SLUG, operator=OPERATOR)
    confirm = f"{SOURCE_SLUG}:{TWIN_SLUG}"
    with pytest.raises(replay.ReplayRefused, match="confirm with exactly"):
        replay.import_(conn, TWIN_SLUG, exported, operator=OPERATOR, confirm="yes")
    with pytest.raises(replay.ReplayRefused, match="every exported capture exactly once"):
        replay.import_(conn, TWIN_SLUG, exported, operator=OPERATOR, confirm=confirm, order=ids[:2])
    with acting(conn, twin):
        CaptureService(conn, MemoryEvidenceStore()).capture_text(
            actor=Actor(subject="morgan@example.com", display_name="Morgan"), audience=None, adapter="mcp",
            text="Twin's own note.", idempotency_key="own")
    with pytest.raises(replay.ReplayRefused, match="fresh workspace"):
        replay.import_(conn, TWIN_SLUG, exported, operator=OPERATOR, confirm=confirm)


def test_a_handover_needs_the_exact_confirmation_a_live_target_and_every_capture_landed(conn, twin, source):
    _source(conn)
    exported = replay.export(conn, SOURCE_SLUG, operator=OPERATOR)
    confirm = f"{SOURCE_SLUG}:{TWIN_SLUG}"
    with pytest.raises(replay.ReplayRefused, match="confirm with exactly"):
        replay.handover(conn, SOURCE_SLUG, TWIN_SLUG, operator=OPERATOR, confirm="yes")
    with pytest.raises(replay.ReplayRefused, match="3 of the source's captures have not landed"):
        replay.handover(conn, SOURCE_SLUG, TWIN_SLUG, operator=OPERATOR, confirm=confirm)
    replay.import_(conn, TWIN_SLUG, exported, operator=OPERATOR, confirm=confirm)
    testdb.ensure_tenant(conn, twin, TWIN_SLUG, status="suspended")
    try:
        with pytest.raises(replay.ReplayRefused, match="not active"):
            replay.handover(conn, SOURCE_SLUG, TWIN_SLUG, operator=OPERATOR, confirm=confirm)
    finally:
        testdb.ensure_tenant(conn, twin, TWIN_SLUG)
    with tenancy.platform_scope(conn):
        assert platform.tenant_by_id(conn, SOURCE).status == "active"
