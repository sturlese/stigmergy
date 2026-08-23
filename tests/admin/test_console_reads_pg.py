"""The console's composed reads — the served entity registry and the registry check
over it, and the metrics window — against real Postgres, through the service AND over the
composed `/admin` branch. The queue, repair, ledger and snapshot writes they summarize go through
the packages' own writers (`queue`, `repair.store`, `decisions`, `index.store`), so every
aggregate here is counted over rows shaped exactly as production shapes them; `audit_log` rows
alone are inserted by hand, because the one writer (`server.audit.AuditWriter`) stamps `now()`
and these tests need rows placed on a day.

Benign twins throughout: a name the registry knows is told apart from one it would merely
confuse, and both from one that is genuinely new — a check that only ever says "stop" teaches a
steward to ignore it."""
import asyncio
import json

import httpx
import pytest
from psycopg.types.json import Jsonb
from starlette.responses import JSONResponse

from stigmergy.admin import service as admin_service
from stigmergy.admin.routes import compose
from stigmergy.admin.service import AdminBadRequest, AdminRefused, AdminService
from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.index import store as index_store
from stigmergy.repair import schema as repair_schema
from tests.admin.conftest import (
    ADMIN_TOKEN,
    introduce_entity,
    register_entity,
    submit_one,
)

REGISTRY = {
    "entities": {
        "globex": {"name": "Globex", "type": "organization", "aliases": ["Globex Corporation"]},
        "aurora-systems": {"name": "Aurora Systems", "type": "organization",
                           "aliases": ["Aurora"]},
        "stigmergy": {"name": "Stigmergy", "type": "product", "aliases": ["the brain"]},
    }
}


@pytest.fixture()
def service(conn, server_settings, admin_settings):
    return AdminService(conn, server_settings=server_settings, admin_settings=admin_settings)


@pytest.fixture()
def snapshot(conn):
    """The served registry, installed the way the push webhook installs it. Cleared afterwards so
    the module's other suites see the "no snapshot" road they were written against."""
    index_store.ensure_ops_file_table(conn)
    index_store.write_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH, json.dumps(REGISTRY),
                               "abc1234")
    yield
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)


# ── the served registry ───────────────────────────────────────────────────────────────────────
def test_entities_registry_lists_the_served_snapshot_sorted_by_name(conn, service, snapshot):
    registry = service.entities_registry()

    assert registry["available"] is True
    assert registry["road"] == "snapshot" and registry["source"] == "abc1234"
    assert registry["refreshed_at"]
    assert registry["count"] == 3
    assert registry["by_type"] == {"organization": 2, "product": 1}
    assert [e["name"] for e in registry["entities"]] == ["Aurora Systems", "Globex", "Stigmergy"]
    assert registry["entities"][1] == {"id": "globex", "name": "Globex", "type": "organization",
                                       "aliases": ["Globex Corporation"], "approved_by": ""}


def test_entities_registry_without_a_snapshot_or_a_file_says_so_rather_than_failing(service):
    """The keyless, checkout-less posture most local servers run in: no snapshot, no
    `--entity-registry` file. The page renders an empty vocabulary and names the road."""
    registry = service.entities_registry()
    assert registry == {"available": False, "road": "none", "source": "", "refreshed_at": None,
                        "count": 0, "by_type": {}, "entities": []}


def test_a_malformed_snapshot_is_a_refusal_with_the_loaders_sentence(conn, service):
    """The same posture the substrate check holds: `kernel.registry`'s own `ValueError` crosses
    as a 409, never as `the operation failed (ValueError)`."""
    index_store.ensure_ops_file_table(conn)
    index_store.write_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH,
                               json.dumps({"entities": {"x": {}}}), "bad")
    try:
        with pytest.raises(AdminRefused, match="needs at least a 'name'"):
            service.entities_registry()
        with pytest.raises(AdminRefused):
            service.entities_resolve(["Globex"])
    finally:
        index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)


# ── the pre-mint check ────────────────────────────────────────────────────────────────────────
def test_resolve_tells_registered_from_colliding_from_similar_from_clear(conn, service, snapshot):
    """Four verdicts, each the answer a different question gives. `registered`: the FILING fold
    resolves the spelling (`Registry.canonical_id`) — nothing to create. `collides`: the
    filing fold does not, but the BIRTH GATE's fold would refuse it (`Registry.collision_id`
    strips the legal suffix) — the same refusal the door would raise after the clone, delivered
    before it. `similar`: neither fold, but a shared distinctive word — advisory only. `clear`:
    none of the above. The benign twin is the last one: a genuinely new name must come back
    clean, with an EMPTY similar list, or the check is noise."""
    result = service.entities_resolve(["the brain", "Globex Corp", "Aurora Labs", "Kestrel"])

    verdicts = {c["name"]: c for c in result["checks"]}
    assert verdicts["the brain"]["verdict"] == admin_service.VERDICT_REGISTERED
    assert verdicts["the brain"]["match"]["id"] == "stigmergy"

    assert verdicts["Globex Corp"]["verdict"] == admin_service.VERDICT_COLLIDES
    assert verdicts["Globex Corp"]["match"] == {
        "id": "globex", "name": "Globex", "type": "organization",
        "aliases": ["Globex Corporation"], "approved_by": ""}

    assert verdicts["Aurora Labs"]["verdict"] == admin_service.VERDICT_SIMILAR
    assert verdicts["Aurora Labs"]["match"] is None
    assert [s["id"] for s in verdicts["Aurora Labs"]["similar"]] == ["aurora-systems"]
    assert verdicts["Aurora Labs"]["similar"][0]["why"] == "contains «Aurora»", (
        "the alias 'Aurora' is the spelling this name contains — the listing says which")

    assert verdicts["Kestrel"] == {"name": "Kestrel", "verdict": admin_service.VERDICT_CLEAR,
                                   "match": None, "similar": []}
    assert result["registry"]["road"] == "snapshot"


def test_resolve_collision_is_the_birth_gates_own_fold_not_a_looser_one(conn, service, snapshot):
    """`collides` must mean exactly what `birth._refuse_collisions` will say — it is computed by
    the same `Registry.collision_id`. A name that merely SHARES a word is therefore `similar`,
    never `collides`: reporting it as a refusal would be a second, stricter gate that stops a
    steward registering a legitimately distinct entity."""
    [shares_a_word] = service.entities_resolve(["Aurora Labs"])["checks"]
    assert shares_a_word["verdict"] == admin_service.VERDICT_SIMILAR
    [an_alias_with_a_suffix] = service.entities_resolve(["Globex Corporation Ltd"])["checks"]
    assert an_alias_with_a_suffix["verdict"] == admin_service.VERDICT_COLLIDES


def test_resolve_cleans_control_characters_and_collapses_whitespace_before_checking(
        conn, service, snapshot):
    """The name is checked as the form will SHOW it — `_clean` strips C0/C1, the whitespace
    collapse mirrors `entity_create`'s own — so the verdict is about the string the steward
    reads. A blank after cleaning is skipped, not checked as the empty name."""
    result = service.entities_resolve(["  Glo\x01bex   Corp ", "\x02", ""])
    assert [c["name"] for c in result["checks"]] == ["Globex Corp"]
    assert result["checks"][0]["verdict"] == admin_service.VERDICT_COLLIDES


def test_resolve_refuses_a_non_list_and_an_oversized_list(service):
    with pytest.raises(AdminBadRequest, match="list of strings"):
        service.entities_resolve("Globex")
    with pytest.raises(AdminBadRequest, match="list of strings"):
        service.entities_resolve([1, 2])
    with pytest.raises(AdminBadRequest, match="capped"):
        service.entities_resolve(["x"] * (admin_service.MAX_RESOLVE_NAMES + 1))


def test_resolve_without_a_registry_answers_unchecked_for_every_name(service):
    result = service.entities_resolve(["Globex"])
    assert result["registry"]["available"] is False
    assert result["checks"] == [{"name": "Globex", "verdict": admin_service.VERDICT_UNCHECKED,
                                 "match": None, "similar": []}]


# ── metrics ───────────────────────────────────────────────────────────────────────────────────
def _audit(conn, *, tool, outcome="ok", result=None, identity="a@example.com", error_class="",
           duration_ms=100.0, days_ago=0):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (ts, identity, tool, args, duration_ms, outcome, error_class,"
            " result) VALUES (now() - make_interval(days => %s), %s, %s, %s, %s, %s, %s, %s)",
            (days_ago, identity, tool, Jsonb({}), duration_ms, outcome, error_class,
             None if result is None else Jsonb(result)))


def test_metrics_shapes_asks_with_the_activity_tables_own_predicates(conn, service):
    """`refused` truthy is a refusal; otherwise `citations` truthy is an answer with a citation;
    otherwise an answer without one — `measurements.answer_shape` to the letter, per day. An
    errored call and a call with no recorded result are counted apart, never folded into either
    answer shape."""
    _audit(conn, tool="ask", result={"refused": True})
    _audit(conn, tool="ask", result={"refused": False, "citations": 2})
    _audit(conn, tool="ask", result={"refused": False, "citations": 0})
    _audit(conn, tool="ask", outcome="error", error_class="RateLimitError")
    _audit(conn, tool="ask")   # ok, no result recorded
    _audit(conn, tool="ask", result={"refused": True}, days_ago=3)

    asks = service.metrics(days=30)["asks_by_day"]

    assert len(asks) == 2 and asks[0]["day"] < asks[1]["day"], "ascending by day"
    assert asks[1] == {"day": asks[1]["day"], "answered_with_citation": 1,
                       "answered_no_citation": 1, "refused": 1, "errors": 1, "unrecorded": 1}
    assert asks[0]["refused"] == 1


def test_metrics_counts_calls_per_tool_and_identity_inside_the_window_only(conn, service):
    _audit(conn, tool="search_brain", duration_ms=50)
    _audit(conn, tool="search_brain", duration_ms=150, outcome="error",
           error_class="RateLimitError")
    _audit(conn, tool="ask", identity="b@example.com", duration_ms=5000)
    _audit(conn, tool="brain_submit", identity="b@example.com", duration_ms=20)
    _audit(conn, tool="search_brain", duration_ms=999, days_ago=40)   # outside a 30-day window

    metrics = service.metrics(days=30)

    by_tool = {r["tool"]: r for r in metrics["calls_by_tool"]}
    assert by_tool["search_brain"]["calls"] == 2 and by_tool["search_brain"]["errors"] == 1
    assert by_tool["search_brain"]["p50_ms"] == 50.0 and by_tool["search_brain"]["p95_ms"] == 150.0
    assert by_tool["ask"]["calls"] == 1
    by_identity = {r["identity"]: r for r in metrics["calls_by_identity"]}
    assert by_identity["a@example.com"]["calls"] == 2
    assert by_identity["a@example.com"]["rate_limited"] == 1
    assert by_identity["b@example.com"] == {**by_identity["b@example.com"], "asks": 1,
                                            "submits": 1, "calls": 2}
    by_day = [(r["tool"], r["calls"]) for r in metrics["calls_by_day"]]
    assert sorted(by_day) == [("ask", 1), ("brain_submit", 1), ("search_brain", 2)]
    assert metrics["days"] == 30


def test_metrics_clamps_the_window_and_answers_an_empty_world_with_empty_series(service):
    assert service.metrics(days=0)["days"] == 1
    assert service.metrics(days=10**6)["days"] == admin_service.MAX_METRICS_DAYS
    empty = service.metrics()
    assert empty["captures_by_day"] == [] and empty["asks_by_day"] == []
    assert empty["calls_by_tool"] == [] and empty["filed_latency_ms"] == []
    # The status vocabulary is READ from `repair.schema` rather than typed: it is the repair
    # package's to declare, and a list spelled here would go on asserting a lifecycle that package
    # had already retired (it did — `pending`/`approved`/`rejected` are gone with the capture-is-the-approval change).
    assert empty["repairs"] == {"applied": 0,
                                "by_status": {s: 0 for s in repair_schema.STATUSES},
                                "recent_by_kind": {}, "recent": []}
    assert set(empty["job_history"]) >= {"gardener", "capture-purge", "webhook-index-upsert"}


def test_metrics_captures_by_day_carries_every_status_per_day(conn, service):
    """One row per arrival day with EVERY status present (zero included) — a stacked chart must
    not learn the vocabulary from whichever statuses happened to occur that day."""
    submit_one(conn)
    submit_one(conn)
    queue.claim_next(conn)

    [today] = service.metrics(days=7)["captures_by_day"]

    assert set(today) == {"day", *capture_schema.STATUSES}
    assert today["queued"] == 1 and today["claimed"] == 1 and today["filed"] == 0


# ── the similarity index is built once per request ───────────────────────────────────────────
def test_resolving_many_names_folds_the_registry_once_not_once_per_name(conn, service, snapshot,
                                                                        monkeypatch):
    """`normalize` is NFKD plus regexes plus a suffix loop; folding every registered spelling
    again for every name checked made the Entities page O(names × entities) on the event loop.
    The index folds the registry once per request: the call count is spellings + names, and it
    must not grow with names × entities."""
    calls = []
    real = admin_service.normalize
    monkeypatch.setattr(admin_service, "normalize", lambda text: (calls.append(text), real(text))[1])
    names = [f"Name {i}" for i in range(20)]

    service.entities_resolve(names)

    spellings = sum(1 + len(e["aliases"]) + 1 for e in REGISTRY["entities"].values())   # name, aliases, id
    assert len(calls) == spellings + len(names), (
        f"{len(calls)} folds for {len(names)} names over {spellings} spellings — the registry was "
        "folded per name again")


# ── over the wire ─────────────────────────────────────────────────────────────────────────────
async def _inner(scope, receive, send):
    if scope["type"] == "http":
        await JSONResponse({"inner": True})(scope, receive, send)


@pytest.fixture()
def app(conn, server_settings, admin_settings):
    return compose(_inner, conn=conn, server_settings=server_settings,
                   admin_settings=admin_settings)


def _request(app, method, path, *, token=ADMIN_TOKEN, json_body=None):
    async def go():
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.request(method, path, headers=headers, json=json_body)

    return asyncio.run(go())


def test_the_new_reads_serve_over_http_and_refuse_without_the_token(conn, app, entity_mint_repo):
    register_entity(entity_mint_repo, conn, "Globex", aliases=["Globex Corporation"])
    introduce_entity(entity_mint_repo, conn, "Aurora Systems")

    registry = _request(app, "GET", "/admin/api/entities/registry").json()
    assert registry["count"] == 2
    checks = _request(app, "POST", "/admin/api/entities/resolve",
                      json_body={"names": ["Globex Corporation Ltd"]}).json()["checks"]
    assert checks[0]["verdict"] == admin_service.VERDICT_COLLIDES
    metrics = _request(app, "GET", "/admin/api/metrics?days=7").json()
    assert metrics["days"] == 7

    for method, path in (("GET", "/admin/api/entities/registry"),
                         ("POST", "/admin/api/entities/resolve"), ("GET", "/admin/api/metrics")):
        refused = _request(app, method, path, token=None)
        assert refused.status_code == 401 and refused.json() == {"error": "unauthorized"}, path


def test_the_new_reads_map_bad_input_to_400_with_the_reason(app):
    assert _request(app, "GET", "/admin/api/metrics?days=soon").status_code == 400
    assert "'days' must be an integer" in _request(app, "GET", "/admin/api/metrics?days=soon").json()["error"]
    bad = _request(app, "POST", "/admin/api/entities/resolve", json_body={"names": "Globex"})
    assert bad.status_code == 400 and "list of strings" in bad.json()["error"]
    missing = _request(app, "POST", "/admin/api/entities/resolve", json_body={})
    assert missing.status_code == 400
