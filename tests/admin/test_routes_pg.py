"""The composed `/admin` branch, driven end to end over `httpx.ASGITransport` — real middleware
order, real static files, real Postgres underneath. The inner app is a marker so every test can
prove non-admin traffic still reaches it untouched."""
import asyncio
import json

import httpx
import pytest
from starlette.responses import JSONResponse

from stigmergy.admin.routes import compose
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import schema as capture_schema
from stigmergy.server.settings import Settings
from tests.admin.conftest import (
    ADMIN_TOKEN,
    park,
    propose_repair,
    submit_one,
    unresolved_entity_names_report,
    unresolved_entity_report,
)


async def _inner(scope, receive, send):
    if scope["type"] == "http":
        await JSONResponse({"inner": True})(scope, receive, send)


@pytest.fixture()
def app(conn, server_settings, admin_settings, fake_gateway):
    return compose(_inner, conn=conn, server_settings=server_settings,
                   admin_settings=admin_settings, gateway=fake_gateway)


def _request(app, method, path, *, token=ADMIN_TOKEN, headers=None, json_body=None):
    async def go():
        if isinstance(headers, list):
            request_headers = list(headers)   # verbatim, duplicates included (the smuggling test)
        else:
            request_headers = dict(headers or {})
            if token is not None:
                request_headers.setdefault("Authorization", f"Bearer {token}")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.request(method, path, headers=request_headers, json=json_body)

    return asyncio.run(go())


# ── inert until configured ────────────────────────────────────────────────────────────────────
def test_unconfigured_console_is_404_everywhere_and_inner_traffic_flows(conn, server_settings):
    app = compose(_inner, conn=conn, server_settings=server_settings,
                  admin_settings=AdminSettings())
    for path in ("/admin", "/admin/", "/admin/api/meta", "/admin/assets/styles.css"):
        assert _request(app, "GET", path).status_code == 404, path
    assert _request(app, "GET", "/anything-else").json() == {"inner": True}


def test_an_unconfigured_console_runs_no_admin_ddl(conn, server_settings):
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS admin_actions")
    compose(_inner, conn=conn, server_settings=server_settings, admin_settings=AdminSettings())
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('admin_actions')")
        assert cur.fetchone()[0] is None, "inert must mean NO DDL, not quiet DDL"
    from stigmergy.admin.schema import ensure_admin_schema
    ensure_admin_schema(conn)   # restore for the module's other tests


# ── auth ──────────────────────────────────────────────────────────────────────────────────────
def test_the_shell_is_tokenless_and_the_api_is_not(app):
    page = _request(app, "GET", "/admin/", token=None)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert _request(app, "GET", "/admin/assets/app.js", token=None).status_code == 200
    refused = _request(app, "GET", "/admin/api/meta", token=None)
    assert refused.status_code == 401
    assert refused.json() == {"error": "unauthorized"}


def test_wrong_token_and_smuggled_headers_get_the_generic_401(app):
    assert _request(app, "GET", "/admin/api/meta", token="wrong").status_code == 401
    doubled = _request(app, "GET", "/admin/api/meta", token=None,
                       headers=[("Authorization", f"Bearer {ADMIN_TOKEN}"),
                                ("Authorization", "Bearer other")])
    assert doubled.status_code == 401


def test_the_right_token_reaches_the_handler_with_the_security_headers(app):
    response = _request(app, "GET", "/admin/api/meta")
    assert response.status_code == 200
    assert response.json()["actor_default"] == "suite-default-actor"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    # The admin token is TYPED INTO A FORM on this origin. Fly's `force_https` only REDIRECTS, so
    # without this the first request of a session can still leave the browser over http; HSTS is
    # what stops there being a first time. Added after a pre-publication audit named it.
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_the_root_path_redirects_into_the_shell(app):
    assert _request(app, "GET", "/admin", token=None).status_code == 307


# ── host defense ──────────────────────────────────────────────────────────────────────────────
def test_a_foreign_host_is_421_and_the_configured_one_passes(conn, server_settings,
                                                             admin_settings, monkeypatch):
    monkeypatch.setenv("STIGMERGY_PUBLIC_HOST", "brain.example.com")
    app = compose(_inner, conn=conn, server_settings=server_settings,
                  admin_settings=admin_settings)
    foreign = _request(app, "GET", "/admin/api/meta", headers={"host": "evil.example"})
    assert foreign.status_code == 421
    configured = _request(app, "GET", "/admin/api/meta",
                          headers={"host": "brain.example.com"})
    assert configured.status_code == 200                      # the benign twin
    localhost = _request(app, "GET", "/admin/api/meta", headers={"host": "localhost:8080"})
    assert localhost.status_code == 200


# ── the queue surface over HTTP: the wire shape ───────────────────────────────────────────────
def test_queue_flow_over_http(conn, app):
    ack = submit_one(conn)
    park(conn, ack["id"])
    listed = _request(app, "GET", "/admin/api/queue").json()
    assert listed["counts"]["triage"] == 1
    shown = _request(app, "GET", f"/admin/api/queue/{ack['id']}").json()
    assert shown["status"] == "triage"
    requeued = _request(app, "POST", f"/admin/api/queue/{ack['id']}/requeue",
                        json_body={"actor": "steward", "note": "again"})
    assert requeued.status_code == 200 and requeued.json()["attempts"] == 1


def test_the_error_mapping_carries_the_librarys_sentences(conn, app):
    assert _request(app, "GET", "/admin/api/queue/424242").status_code == 404
    ack = submit_one(conn)   # queued — not parked, so a disposition is refused
    refused = _request(app, "POST", f"/admin/api/queue/{ack['id']}/reject",
                       json_body={"actor": "steward", "reason": "no"})
    assert refused.status_code == 409 and refused.json()["error"]
    bad = _request(app, "GET", "/admin/api/queue?status=bogus")
    assert bad.status_code == 400 and "unknown status" in bad.json()["error"]
    empty_reason = _request(app, "POST", f"/admin/api/queue/{ack['id']}/reject",
                            json_body={"actor": "steward", "reason": "  "})
    assert empty_reason.status_code == 400


def test_a_malformed_body_is_a_400_not_a_traceback(conn, app):
    ack = submit_one(conn)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost") as client:
            return await client.post(
                f"/admin/api/queue/{ack['id']}/requeue", content=b"not json",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}",
                         "Content-Type": "application/json"})
    response = asyncio.run(go())
    assert response.status_code == 400
    assert response.json() == {"error": "request body must be valid JSON"}


# ── the entities surface over HTTP: what the console's mint form is actually handed ────────────
# CHARACTERIZATION, and a hole this file had: the two GET routes the Entities tab reads had no
# wire-level test at all — only the POST that mints. Everything the browser knows about an
# unresolved-entity park crosses exactly these two responses, and `views.js` decides its `Name`
# prefill from what it finds there, so an omission on this wire is an omission the frontend cannot
# recover from and no Python test would otherwise notice.
def test_characterization_the_entities_wire_carries_both_the_joined_subject_and_the_per_name_list(
        conn, app):
    """Pins the SHAPE, on both routes, for a park naming two unresolved entities. `subject` is the
    display string (`", ".join`) and `subjects` is the per-name list; the list route wraps its rows
    in a `situations` envelope and the detail route returns the object bare. The console navigates
    list -> detail, so a key present on one path only is a key the next reader finds missing."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_names_report("Jack", "Acme Capital"))

    listed = _request(app, "GET", "/admin/api/entities")
    assert listed.status_code == 200
    rows = {r["id"]: r for r in listed.json()["situations"]}
    assert rows[ack["id"]]["subject"] == "Jack, Acme Capital"
    assert rows[ack["id"]]["subjects"] == ["Jack", "Acme Capital"]

    detail = _request(app, "GET", f"/admin/api/entities/{ack['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["subject"] == "Jack, Acme Capital"
    assert body["subjects"] == ["Jack", "Acme Capital"]
    assert body["situation"] == "unresolved-entity"
    # The joined compound is a DISPLAY string and nothing on this wire may be mistaken for a name:
    # it is not among the names, so a client that prefilled from it mints something nobody wrote.
    assert body["subject"] not in body["subjects"]


def test_characterization_a_single_name_park_reaches_the_wire_as_a_one_element_list(conn, app):
    """The benign twin, and the case the console prefills from: one unresolved name arrives as a
    one-element `subjects` list whose only entry equals `subject`. The two keys agreeing here is
    what makes the several-names case distinguishable at all."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    body = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    assert body["subject"] == "Globex Robotics"
    assert body["subjects"] == ["Globex Robotics"]


def test_characterization_an_unsupported_type_park_reaches_the_wire_with_no_names_at_all(
        conn, app):
    """The third situation the same two routes serve. `subject` is the judged TYPE and `subjects`
    is EMPTY — never a one-element list holding the type, which a form prefilling from `subjects`
    would offer a steward as an entity named "person"."""
    ack = submit_one(conn)
    park(conn, ack["id"], report={
        capture_schema.SITUATION_KEY: capture_schema.SITUATION_UNSUPPORTED_TYPE,
        capture_schema.SITUATION_TYPE_KEY: "person"})

    body = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    assert body["situation"] == "unsupported-type"
    assert body["subject"] == "person"
    assert body["subjects"] == []


# ── the decided mint prefill over HTTP: the value the console's Approve form defaults to ───────
# `entities.situations.mint_name_prefill` decides one-vs-several ONCE, and `views.js` reads
# `row.mint_name_prefill` instead of counting names itself. That only holds if the key is actually
# on the wire — on BOTH routes, since the console navigates list -> detail and opens the Approve
# form from either. A frontend reading a key the API does not send gets `undefined`, prefills
# nothing, and shows no listing either: an empty required field with no explanation, silently.
# The rule's own cases live in `tests/entities/test_situations.py`; these prove delivery.
def test_a_multi_name_park_sends_an_empty_mint_prefill_on_both_entity_routes(conn, app):
    """Two unresolved names: `mint_name_prefill` is `""` — the instruction to leave `Name` empty
    and list `subjects` — and it is PRESENT rather than absent, on the list route and on the detail
    route alike. Absent and empty look the same to `row.mint_name_prefill || ""` in the browser and
    are not the same contract: only a present key proves the server decided."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_names_report("Jack", "Acme Capital"))

    listed = {r["id"]: r for r in _request(app, "GET", "/admin/api/entities").json()["situations"]}
    detail = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    for where, row in (("list", listed[ack["id"]]), ("detail", detail)):
        assert "mint_name_prefill" in row, (
            f"the {where} route omits `mint_name_prefill` — the console's Approve form reads it "
            "and would fall back to an unexplained empty field")
        assert row["mint_name_prefill"] == "", where
        # coexistence: the two older keys are unchanged in name, type and value on the same wire
        assert row["subject"] == "Jack, Acme Capital", where
        assert row["subjects"] == ["Jack", "Acme Capital"], where


def test_a_single_name_park_sends_that_name_as_the_mint_prefill_on_both_entity_routes(conn, app):
    """The benign twin. A consolidation that blanked every prefill satisfies the test above and
    fails here, and a steward who retypes the same name on every approval learns to stop reading
    the field — which is how the next wrong value gets submitted."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    listed = {r["id"]: r for r in _request(app, "GET", "/admin/api/entities").json()["situations"]}
    detail = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    assert listed[ack["id"]]["mint_name_prefill"] == "Globex Robotics"
    assert detail["mint_name_prefill"] == "Globex Robotics"
    assert detail["subjects"] == ["Globex Robotics"]


def test_an_unsupported_type_park_sends_an_empty_prefill_and_never_the_judged_type(conn, app):
    """The judged type is this row's `subject` and is not a name anybody mints — the prefill must
    not carry it into a form whose submission pushes a signed commit."""
    ack = submit_one(conn)
    park(conn, ack["id"], report={
        capture_schema.SITUATION_KEY: capture_schema.SITUATION_UNSUPPORTED_TYPE,
        capture_schema.SITUATION_TYPE_KEY: "person"})

    body = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    assert body["mint_name_prefill"] == ""
    assert body["subject"] == "person"


def test_an_entities_row_carrying_the_new_prefill_still_satisfies_an_old_shape_reader(conn, app):
    """COEXISTENCE, over HTTP. The key is ADDITIVE: a client written before it existed still finds
    every field it reads, with the same name, type and value. Consumed through such a reader rather
    than eyeballed, so a rename or a retype fails it too — and its own derived answer is asserted
    to equal the server's decision, which is what makes the derivation safe to delete."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_names_report("Jack", "Acme Capital"))

    row = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    def old_shape_reader(entry: dict) -> str:
        """`entityApproveFlow` before the consolidation: it counted `subjects` itself."""
        names = [str(n) for n in (entry["subjects"] or []) if str(n).strip()]
        return names[0] if len(names) == 1 else ""

    assert old_shape_reader(row) == ""
    assert old_shape_reader(row) == row["mint_name_prefill"]
    assert row["subject"] == "Jack, Acme Capital" and row["situation"] == "unresolved-entity"


def test_the_console_decides_the_prefill_on_the_raw_row_before_sanitizing_shows_the_names(
        conn, app):
    """**The one behavioural delta of the consolidation, pinned as a decision.**

    `_situation` decides on the RAW row and `_clean` (control characters stripped) runs on the way
    out, so a name made ENTIRELY of control characters — which `subjects_of`'s `.strip()` filter
    keeps, since `\\x01` is not whitespace — counts towards the decision and then leaves as `""`.

    OLD BEHAVIOUR for `subjects == ["Jack", "\\x01"]`: the console received `["Jack", ""]`, its own
    JS filter dropped the empty entry, it saw ONE name and prefilled "Jack" — while the Slack door,
    deciding on the same park before any such stripping, saw TWO and left its field empty. The two
    doors could not agree.

    NOW: the decision is taken once, before sanitizing, so both doors say "several names, no
    default" — the console leaves `Name` empty and lists what survived ("Jack"). Strictly safer
    (an emptied field is retyped; a wrong accepted default is a commit) and it is what makes the
    two doors agree at all. Accepted deliberately; asserted here so the decide-before-sanitize
    ORDERING is a contract rather than something rediscovered from a bug report."""
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_names_report("Jack", "\x01"))

    body = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    assert body["mint_name_prefill"] == "", (
        "the prefill is decided on the raw row, where this park has TWO names — deciding after "
        "`_clean` would prefill 'Jack' and disagree with the Slack door on the same park")
    assert body["subjects"] == ["Jack", ""], (
        "sanitizing still runs, and it runs AFTER: the control-character name reaches the browser "
        "emptied, so the listing shows only the name a human can read")
    assert "\x01" not in json.dumps(body), "no control character may reach the console"


def test_the_two_entity_routes_agree_byte_for_byte_on_the_prefill_of_a_ragged_name(conn, app):
    """One park, ONE default name — the same bytes on the list route and on the detail route.

    OLD BEHAVIOUR, for a park whose singular `SITUATION_NAME_KEY` is `"\\x01 Jack"`:

        GET /admin/api/entities        ->  mint_name_prefill == " Jack"   (what `main` answers)
        GET /admin/api/entities/{id}   ->  mint_name_prefill == "Jack"

    and the DETAIL route is the one the console's Approve form is opened from, so the string a
    steward submits unchanged was not the string the list had shown them, nor the one the Slack
    door offers for the same row — three surfaces, three default names for one park.

    Order, not rule: `entities_show` sanitized the row BEFORE handing it to the decision, so
    `mint_name_prefill` read a `report` whose STRING values had already had their control
    characters removed and `.strip()` then also took the space they had been shielding. (The
    plural-key tests above never caught it: `_traced_fields` only cleans `isinstance(v, str)`
    values, so a `SITUATION_NAMES_KEY` list passes through untouched.) The decision is taken on
    the RAW row — the test above pins that ordering — and sanitized ONCE on the way out.

    Byte-identical rather than "close enough": the difference is a leading space in a name minted
    into the knowledge repo as an entity title and slugged into its canonical id.
    """
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("\x01 Jack"))

    listed = {r["id"]: r for r in _request(app, "GET", "/admin/api/entities").json()["situations"]}
    detail = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()
    list_prefill = listed[ack["id"]]["mint_name_prefill"]
    detail_prefill = detail["mint_name_prefill"]

    assert list_prefill == detail_prefill, (
        f"one park, two default names: the list route offers {list_prefill!r} and the detail "
        f"route — the one the Approve form is opened from — offers {detail_prefill!r}")
    assert detail_prefill == " Jack", (
        "the prefill is decided on the RAW row, where this name is '\\x01 Jack', and the console's "
        "only transformation is `_clean` on the way out; tidying it further is a decision about "
        "what gets minted, taken in `entities.situations`, not a side effect of sanitizing twice")
    assert detail_prefill in detail["subjects"], (
        "the offered default must be one of the names the row DISPLAYS — a prefill that is in no "
        "listing is a name no surface ever showed the steward who submits it")
    assert "\x01" not in json.dumps(detail), "no control character may reach the console"


def test_an_ordinary_punctuated_name_still_prefills_and_still_agrees_on_both_routes(conn, app):
    """The specificity twin of the regression above: agreeing is not enough, the two routes have
    to agree on the NAME. Blanking every prefill, or normalizing one into the other, satisfies that
    test and fails this one. Its payload is deliberately not the plain-ASCII name of
    `test_a_single_name_park_sends_that_name_as_the_mint_prefill_on_both_entity_routes`: accents
    and punctuation are what a "sanitize harder / strip more" fix quietly rewrites, and they are
    ordinary in the entity names stewards actually mint."""
    name = "Jörg & Söhne AB"
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report(name))

    listed = {r["id"]: r for r in _request(app, "GET", "/admin/api/entities").json()["situations"]}
    detail = _request(app, "GET", f"/admin/api/entities/{ack['id']}").json()

    assert listed[ack["id"]]["mint_name_prefill"] == name
    assert detail["mint_name_prefill"] == name
    assert detail["subjects"] == [name]


# ── the entities surface over HTTP: a real Approve, mints (ADR 030) ────────────────────────────
def test_entities_approve_mints_over_http(conn, admin_settings, fake_gateway, entity_mint_repo,
                                          require_gitleaks):
    """The wire-level end-to-end proof: POSTing the form's own field shape through the REAL
    `compose` product mints for real and reports the entity + commit, over HTTP."""
    app = compose(_inner, conn=conn, server_settings=Settings(librarian_repo_url=entity_mint_repo),
                  admin_settings=admin_settings, gateway=fake_gateway)
    ack = submit_one(conn, submitted_by="steward@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    response = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", json_body={
        "actor": "steward@example.com", "name": "Globex Robotics", "entity_type": "organization",
        "aliases": "Globex, Globex Robotics Inc", "requeue": True,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["entity_id"] == "globex-robotics" and body["requeued"] is True
    assert len(body["commit"]) == 40
    with conn.cursor() as cur:
        cur.execute("SELECT verdict, extra FROM review_decisions WHERE item_id = %s",
                    (str(ack["id"]),))
        verdict, extra = cur.fetchone()
    assert verdict == "approve" and extra["entity_id"] == "globex-robotics"


def test_entities_approve_requires_the_token(conn, app):
    ack = submit_one(conn)
    park(conn, ack["id"], report=unresolved_entity_report("Acme Corp"))

    refused = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", token=None,
                       json_body={"actor": "x", "name": "Acme Corp",
                                  "entity_type": "organization"})

    assert refused.status_code == 401
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0, "an unauthorized request must never reach the mint"


def test_entities_approve_error_mapping_over_http(conn, app):
    """`app` (the module fixture) carries no `librarian_repo_url` — exactly the "not yet a
    console-drivable capability" shape the 409 case below needs; the 400 case never reaches that
    far at all."""
    ack = submit_one(conn, submitted_by="steward@example.com")
    park(conn, ack["id"], report=unresolved_entity_report("Globex Robotics"))

    bad = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve",
                   json_body={"actor": "steward", "name": "", "entity_type": ""})
    assert bad.status_code == 400 and "missing" in bad.json()["error"]

    not_boolean = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", json_body={
        "actor": "steward", "name": "Globex Robotics", "entity_type": "organization",
        "requeue": "yes",
    })
    assert not_boolean.status_code == 400 and "boolean" in not_boolean.json()["error"]

    refused = _request(app, "POST", f"/admin/api/entities/{ack['id']}/approve", json_body={
        "actor": "steward@example.com", "name": "Globex Robotics", "entity_type": "organization",
    })
    assert refused.status_code == 409
    assert "STIGMERGY_LIBRARIAN_REPO_URL" in refused.json()["error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0


# ── crons over HTTP: the wire shape ───────────────────────────────────────────────────────────
def test_cron_dispatch_and_the_allowlist_over_http(app, fake_gateway):
    ok = _request(app, "POST", "/admin/api/crons/gardener.yml/dispatch",
                  json_body={"actor": "steward"})
    assert ok.status_code == 200
    assert ("dispatch", "gardener.yml", "main", None) in fake_gateway.calls
    refused = _request(app, "POST", "/admin/api/crons/rm-rf.yml/dispatch",
                       json_body={"actor": "steward"})
    assert refused.status_code == 400


def test_a_github_failure_is_a_502_with_the_gateways_sentence(app, fake_gateway):
    from stigmergy.admin.github import ActionsError
    fake_gateway.fail_with = ActionsError("GitHub answered 403 for PUT x", status=403)
    response = _request(app, "POST", "/admin/api/crons/gardener.yml/enable",
                        json_body={"actor": "steward"})
    assert response.status_code == 502
    assert "403" in response.json()["error"]


def test_an_unexpected_failure_names_the_class_only(conn, app, monkeypatch):
    from stigmergy.admin import service as service_module

    def boom(self):
        raise RuntimeError("secret detail that must not cross")

    monkeypatch.setattr(service_module.AdminService, "worker_status", boom)
    response = _request(app, "GET", "/admin/api/worker")
    assert response.status_code == 500
    assert response.json() == {"error": "the operation failed (RuntimeError)"}


# ── repairs over HTTP (ADR 039) ───────────────────────────────────────────────────────────────
def test_repairs_list_and_show_over_http(conn, app):
    proposal_id = propose_repair(conn)

    listed = _request(app, "GET", "/admin/api/repairs")
    shown = _request(app, "GET", f"/admin/api/repairs/{proposal_id}")

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["pending"]] == [proposal_id]
    assert shown.status_code == 200
    assert shown.json()["ops"][0]["path"] == "wiki/notes/Renewals.md"
    assert _request(app, "GET", "/admin/api/repairs/999999").status_code == 404


def test_repairs_reject_requires_a_reason_and_records_it(conn, app):
    proposal_id = propose_repair(conn)

    blank = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/reject",
                     json_body={"actor": "steward@example.com", "reason": "   "})
    given = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/reject",
                     json_body={"actor": "steward@example.com", "reason": "already linked"})

    assert blank.status_code == 400
    assert given.status_code == 200
    with conn.cursor() as cur:
        cur.execute("SELECT status, notes FROM repair_proposals WHERE id = %s", (proposal_id,))
        assert cur.fetchone() == ("rejected", "already linked")


def test_repairs_approve_requires_the_token_and_never_reaches_the_apply_without_it(conn, app,
                                                                                   monkeypatch):
    """The benign twin lives at the service level (`test_repair_approve_applies_and_records_both_
    ledgers`); what THIS pins is that an unauthorized POST is refused by the gate, before any of it
    — no clone, no decision, no ledger row."""
    from stigmergy.repair import remote as repair_remote

    def never(*_a, **_k):
        raise AssertionError("apply_via_clone ran on an unauthorized request")

    monkeypatch.setattr(repair_remote, "apply_via_clone", never)
    proposal_id = propose_repair(conn)

    refused = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/approve", token=None,
                       json_body={"actor": "mallory"})

    assert refused.status_code == 401
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM repair_proposals WHERE id = %s", (proposal_id,))
        assert cur.fetchone()[0] == "pending"
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0


# ── the two approvals that clone, and where they run ──────────────────────────────────────────
# Both Approve handlers reach code that clones a repo, runs the eight gates and pushes — seconds of
# blocking work, and `gitleaks`/`git` are subprocesses. On the event loop that stalls EVERY other
# request the process is serving, the MCP tools included, for as long as the push takes.
def _on_the_event_loop_probe(monkeypatch, method: str):
    """Replace one `AdminService` method with a probe that reports whether it was called ON the
    asyncio event loop. It answers a fact about the CALLER, so it works identically for a handler
    that awaits it directly and for one that hands it to a worker thread."""
    from stigmergy.admin.service import AdminService

    def probe(*_a, **_k):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {"on_the_event_loop": False}
        return {"on_the_event_loop": True}

    monkeypatch.setattr(AdminService, method, probe)


@pytest.mark.parametrize("method, path, body", [
    ("repair_approve", "/admin/api/repairs/{id}/approve", {"actor": "steward@example.com"}),
    ("entity_approve", "/admin/api/entities/{id}/approve",
     {"actor": "steward@example.com", "name": "Globex Robotics", "entity_type": "organization"}),
])
def test_an_approve_that_clones_never_runs_on_the_event_loop(conn, app, monkeypatch, method, path,
                                                             body):
    """Red before the fix: both handlers awaited nothing and called the blocking service method
    inline, so the whole clone-gate-push sat on the loop and every concurrent request waited on it.

    The response SHAPE is asserted too: moving the call to a worker thread must not change what the
    route returns, or the console's own JavaScript stops reading it."""
    proposal_id = propose_repair(conn)
    _on_the_event_loop_probe(monkeypatch, method)

    response = _request(app, "POST", path.format(id=proposal_id), json_body=body)

    assert response.status_code == 200
    assert response.json() == {"on_the_event_loop": False}


def test_repairs_approve_on_an_unconfigured_deployment_is_the_409(conn, app):
    """`app` carries a default `Settings()` — no `librarian_repo_url` — so this is the deployment
    shape an operator meets before configuring one, and it must read as a refusal with the reason
    rather than a 500 naming a class."""
    proposal_id = propose_repair(conn)

    response = _request(app, "POST", f"/admin/api/repairs/{proposal_id}/approve",
                        json_body={"actor": "steward@example.com"})

    assert response.status_code == 409
    assert "STIGMERGY_LIBRARIAN_REPO_URL" in response.json()["error"]
