"""The console's composed reads — the inbox, the served entity registry and the registry check
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
from stigmergy.capture import decisions, queue
from stigmergy.capture import schema as capture_schema
from stigmergy.index import store as index_store
from stigmergy.review_kinds import KIND_ALIAS_PROPOSAL, KIND_IDENTITY_PROPOSAL, KIND_REPAIR_PROPOSAL
from tests.admin.conftest import (
    ADMIN_TOKEN,
    propose_identity,
    propose_repair,
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


# ── the inbox ─────────────────────────────────────────────────────────────────────────────────
def test_inbox_lists_every_kind_of_item_waiting_on_a_steward(conn, service, entity_mint_repo):
    """Three kinds, one list — the doorbell's own read: an identity the librarian proposed, a
    spelling it proposed for a registered entity, and a pending repair proposal."""
    entity_id = propose_identity(entity_mint_repo, conn, "Nimbus")
    register_entity(entity_mint_repo, conn, "Initech", proposed_aliases=["Initech Ltd"])
    proposal = propose_repair(conn)

    inbox = service.inbox()

    by = {(item["kind"], item["id"]): item for item in inbox["items"]}
    assert set(by) == {(KIND_IDENTITY_PROPOSAL, entity_id),
                       (KIND_ALIAS_PROPOSAL, "initech:Initech Ltd"),
                       (KIND_REPAIR_PROPOSAL, str(proposal))}
    assert inbox["count"] == 3
    assert inbox["counts"] == {KIND_IDENTITY_PROPOSAL: 1, KIND_ALIAS_PROPOSAL: 1,
                               KIND_REPAIR_PROPOSAL: 1}
    assert by[(KIND_IDENTITY_PROPOSAL, entity_id)]["name"] == "Nimbus"
    assert all(item["decision"] is None for item in inbox["items"]), "nothing decided yet"
    assert inbox["truncated"] is False


def test_inbox_carries_the_ledgers_latest_decision_cleaned(conn, service, entity_mint_repo):
    """A decision another door already recorded rides on the item — that is how a steward learns
    a second door got there first — with the console's own control-character strip applied to
    the free-text actor, as on every other string it renders."""
    entity_id = propose_identity(entity_mint_repo, conn, "Nimbus")
    decisions.record_decision(conn, item_kind=KIND_IDENTITY_PROPOSAL, item_id=entity_id,
                              verdict="reject", actor="ana\x01@example.com", source="slack",
                              notes="duplicate")

    [item] = service.inbox()["items"]

    assert item["decision"]["verdict"] == "reject"
    assert item["decision"]["actor"] == "ana@example.com"
    assert item["decision"]["source"] == "slack"
    assert isinstance(item["decision"]["created_at"], str), "ISO on the wire, never a datetime"


def test_inbox_is_empty_not_broken_on_an_empty_world(service):
    inbox = service.inbox()
    assert inbox["count"] == 0 and inbox["items"] == []
    assert inbox["counts"] == {KIND_IDENTITY_PROPOSAL: 0, KIND_ALIAS_PROPOSAL: 0,
                               KIND_REPAIR_PROPOSAL: 0}


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
                                       "aliases": ["Globex Corporation"], "proposed": False,
                                       "approved_by": "", "proposed_aliases": []}


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
        "aliases": ["Globex Corporation"], "proposed": False, "approved_by": "",
        "proposed_aliases": []}

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


def test_every_proposal_carries_the_registry_verdict_on_its_own_name(conn, service,
                                                                    entity_mint_repo):
    """The list and the detail both attach `check` — the birth gate's verdict on the proposed
    name against the REST of the registry, and `registry_check` names the copy it was checked
    against — so a steward sees which proposal is a registered entity under another spelling
    before opening it."""
    register_entity(entity_mint_repo, conn, "Globex", aliases=["Globex Corporation"])
    propose_identity(entity_mint_repo, conn, "Globex Corporation")
    propose_identity(entity_mint_repo, conn, "Kestrel")

    listed = service.entities_list()
    by_id = {p["id"]: p for p in listed["proposals"]}
    assert by_id["globex-corporation"]["check"]["verdict"] == admin_service.VERDICT_REGISTERED
    assert by_id["kestrel"]["check"]["verdict"] == admin_service.VERDICT_CLEAR
    assert listed["registry_check"]["road"] == "snapshot"
    assert service.entities_show("globex-corporation")["check"]["match"]["id"] == "globex"


def test_the_entities_page_still_renders_when_the_snapshot_cannot_be_read(conn, service):
    """Advisory means advisory: a snapshot the loader refuses must not take the Entities page
    down with it. The list renders empty, and the loader's sentence rides on
    `registry_check.error` for the page to show."""
    index_store.ensure_ops_file_table(conn)
    index_store.write_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH, "[]", "bad")
    try:
        listed = service.entities_list()
        assert listed["proposals"] == [] and listed["aliases"] == []
        assert "top level must be an object" in listed["registry_check"]["error"]
        assert listed["registry_check"]["available"] is False
    finally:
        index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)


# ── metrics ───────────────────────────────────────────────────────────────────────────────────
def _audit(conn, *, tool, outcome="ok", result=None, identity="a@example.com", error_class="",
           duration_ms=100.0, days_ago=0):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (ts, identity, tool, args, duration_ms, outcome, error_class,"
            " result) VALUES (now() - make_interval(days => %s), %s, %s, %s, %s, %s, %s, %s)",
            (days_ago, identity, tool, Jsonb({}), duration_ms, outcome, error_class,
             None if result is None else Jsonb(result)))


def test_metrics_shapes_asks_with_the_pilot_reports_own_predicates(conn, service):
    """`refused` truthy is a refusal; otherwise `citations` truthy is an answer with a citation;
    otherwise an answer without one — `pilot_report.answer_shape` to the letter, per day. An
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
    assert empty["decisions"] == []
    assert empty["repairs"] == {"pending": 0,
                                "by_status": {s: 0 for s in ("pending", "approved", "rejected",
                                                             "applied", "failed")},
                                "recent_by_kind": {}, "recent": []}
    assert set(empty["job_history"]) >= {"gardener", "capture-purge", "digest"}


def test_metrics_captures_by_day_carries_every_status_per_day(conn, service):
    """One row per arrival day with EVERY status present (zero included) — a stacked chart must
    not learn the vocabulary from whichever statuses happened to occur that day."""
    submit_one(conn)
    submit_one(conn)
    queue.claim_next(conn)

    [today] = service.metrics(days=7)["captures_by_day"]

    assert set(today) == {"day", *capture_schema.STATUSES}
    assert today["queued"] == 1 and today["claimed"] == 1 and today["filed"] == 0


def test_metrics_lists_the_ledgers_newest_rows_every_decision_bounded(conn, service):
    """A feed, not a state: two decisions on one item are two rows, newest first, and the bound
    is `DECISIONS_LIMIT` applied in SQL — the ledger is append-only and never truncated."""
    decisions.record_decision(conn, item_kind=KIND_IDENTITY_PROPOSAL, item_id="7",
                              verdict="reject", actor="ana", source="slack", notes="no")
    decisions.record_decision(conn, item_kind=KIND_IDENTITY_PROPOSAL, item_id="7",
                              verdict="approve", actor="marc", source="admin")
    decisions.record_decision(conn, item_kind=KIND_REPAIR_PROPOSAL, item_id="3",
                              verdict="reject", actor="ana\x01", source="mcp", notes="no")

    rows = service.metrics()["decisions"]

    assert [(r["kind"], r["id"], r["verdict"], r["source"], r["actor"]) for r in rows] == [
        (KIND_REPAIR_PROPOSAL, "3", "reject", "mcp", "ana"),
        (KIND_IDENTITY_PROPOSAL, "7", "approve", "admin", "marc"),
        (KIND_IDENTITY_PROPOSAL, "7", "reject", "slack", "ana")]
    assert len(rows) <= admin_service.DECISIONS_LIMIT


# ── the names a mint door may offer ───────────────────────────────────────────────────────────
# ── the inbox's bound ─────────────────────────────────────────────────────────────────────────
def test_inbox_reports_truncation_when_the_list_is_as_long_as_its_limit(conn, service,
                                                                        entity_mint_repo):
    """The conservative flag: a list exactly `limit` long says `truncated`, because the read cannot
    tell "exactly that many" from "more". The benign twin is the same rows under a wider limit."""
    for name in ("Nimbus", "Kestrel", "Vandelay Imports"):
        propose_identity(entity_mint_repo, conn, name)

    narrow = service.inbox(limit=2)
    assert narrow["count"] == 2 and narrow["truncated"] is True and narrow["limit"] == 2
    wide = service.inbox(limit=10)
    assert wide["count"] == 3 and wide["truncated"] is False


def test_inbox_cleans_every_string_leaf_not_a_list_of_keys(conn, service):
    """A control character in a leaf nobody named — a repair's rationale — is stripped all the
    same: the inbox cleans by walking, so a key added upstream arrives cleaned by default. The
    benign twin: a newline and a literal `<script>` survive (HTML inertness is the browser's
    half)."""
    propose_repair(conn, rationale="Nim\x01bus <script>\nCo")

    [item] = service.inbox()["items"]

    assert item["rationale"] == "Nimbus <script>\nCo"


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
def app(conn, server_settings, admin_settings, fake_gateway):
    return compose(_inner, conn=conn, server_settings=server_settings,
                   admin_settings=admin_settings, gateway=fake_gateway)


def _request(app, method, path, *, token=ADMIN_TOKEN, json_body=None):
    async def go():
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.request(method, path, headers=headers, json=json_body)

    return asyncio.run(go())


def test_the_new_reads_serve_over_http_and_refuse_without_the_token(conn, app, entity_mint_repo):
    register_entity(entity_mint_repo, conn, "Globex", aliases=["Globex Corporation"])
    propose_identity(entity_mint_repo, conn, "Globex Corporation")

    assert _request(app, "GET", "/admin/api/inbox").json()["count"] == 1
    registry = _request(app, "GET", "/admin/api/entities/registry").json()
    assert registry["count"] == 2
    checks = _request(app, "POST", "/admin/api/entities/resolve",
                      json_body={"names": ["Globex Corporation Ltd"]}).json()["checks"]
    assert checks[0]["verdict"] == admin_service.VERDICT_COLLIDES
    metrics = _request(app, "GET", "/admin/api/metrics?days=7").json()
    assert metrics["days"] == 7
    # `entities/registry`, `entities/resolve`, `entities/decide` and `entities/create` are
    # registered BEFORE `entities/{id}`, so the words are never read as an entity id.
    assert _request(app, "GET", "/admin/api/entities/registry").status_code == 200
    shown = _request(app, "GET", "/admin/api/entities/globex-corporation").json()
    assert shown["check"]["verdict"] == admin_service.VERDICT_REGISTERED

    for method, path in (("GET", "/admin/api/inbox"), ("GET", "/admin/api/entities/registry"),
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
