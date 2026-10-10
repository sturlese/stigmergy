"""Backoffice access: personal sign-in, sessions, CSRF, roles, roster administration, and the
permission-scoped wiki, driven through the real routes against Postgres."""
import datetime as dt
import subprocess
from dataclasses import dataclass
from html.parser import HTMLParser

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from stigmergy.admin import auth
from stigmergy.admin.routes import compose
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.capture.schema import ensure_capture_schema
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.knowledge.pages import render_page
from stigmergy.ops import erasure
from stigmergy.server.audit import ensure_audit_table
from stigmergy.server.ratelimit import AuthFailureThrottle
from stigmergy.server.settings import Settings
from tests import testdb
from tests.roster import seed_roster

ADMIN = "admin@example.com"
MEMBER = "member@example.com"
AGENT = "agent@example.com"
ROSTER = {
    ADMIN: {"display_name": "Admin", "groups": ["brain-admins"], "default_audience": ["public"]},
    MEMBER: {"display_name": "Member", "groups": ["product"], "default_audience": ["product"]},
    AGENT: {"display_name": "Agent", "groups": ["brain-admins"], "default_audience": ["public"],
            "kind": "agent"},
}
PUBLIC = "wiki/notes/Roadmap.md"
FINANCE = "wiki/notes/Payroll.md"
FINANCE_FILE = f"layers/finance/{FINANCE}"
HOSTILE = "wiki/notes/Hostile.md"
ENTITY = "wiki/entities/Acme.md"
CSRF = {"X-Stigmergy-CSRF": "1", "Origin": "http://testserver"}
HOSTILE_BODY = (
    "# Hostile\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n"
    "[click](javascript:alert(3)) [data](data:text/html,<b>x</b>) "
    "[fine](https://example.com/docs)\n\n<svg onload=alert(4)><style>*{}</style>\n\n"
    "[[Acme\"><img src=x onerror=alert(5)>]] and [[Payroll]]"
)


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.seen = []

    def handle_starttag(self, tag, attrs):
        self.seen.append((tag, attrs))


@dataclass
class Rig:
    client: TestClient
    conn: object
    tokens: dict
    app: object
    repo: object


def _page(path, role, title, body, acl=None, **extra):
    return render_page(path=path, role=role, title=title, body=body, acl=acl,
                       created=dt.date(2026, 9, 1), updated=dt.date(2026, 9, 1), **extra)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv("STIGMERGY_PUBLIC_HOST", raising=False)
    repo = tmp_path / "brain"
    for folder in ("wiki/notes", "wiki/entities", "layers/finance/wiki/notes"):
        (repo / folder).mkdir(parents=True)
    (repo / PUBLIC).write_text(_page(
        PUBLIC, "note", "Roadmap", "# Roadmap\n\nThe roadmap names [[Acme]] and [[Payroll]]."))
    (repo / FINANCE_FILE).write_text(_page(
        FINANCE, "note", "Payroll", "# Payroll\n\nConfidential payroll for [[Acme]].",
        acl=["finance"]))
    (repo / HOSTILE).write_text(_page(HOSTILE, "note", "Hostile", HOSTILE_BODY))
    (repo / ENTITY).write_text(_page(
        ENTITY, "entity", "Acme", "# Acme\n\nA customer.", entity_type="organization",
        aliases=("Acme Corp",)))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit",
                    "-qm", "init"], cwd=repo, check=True)

    conn = testdb.connect_or_skip("admin-access")
    ensure_admin_schema(conn)
    ensure_audit_table(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM admin_actions")
        cur.execute("DELETE FROM audit_log")
    build.rebuild(conn, str(repo), build_embedder("fake"))
    tokens = seed_roster(conn, ROSTER, admins={ADMIN})
    app = compose(
        Starlette(),
        conn=conn,
        server_settings=Settings(knowledge_repo=str(repo), dsn=testdb.dsn(), embedder="fake",
                                 llm="fake"),
        embedder=build_embedder("fake"),
        throttle=AuthFailureThrottle(failures=5),
    )
    with TestClient(app) as client:
        yield Rig(client, conn, tokens, app, repo)
    conn.close()


def _sign_in(rig, subject, headers=CSRF):
    return rig.client.post("/admin/api/session", json={"token": rig.tokens[subject]},
                           headers=headers)


def _cookie(response) -> str:
    value = response.headers["set-cookie"].split(";", 1)[0]
    assert value.startswith(f"{auth.SESSION_COOKIE}=")
    return value


def _session(rig, subject) -> dict:
    return {"Cookie": _cookie(_sign_in(rig, subject))}


def _actions(rig, action):
    with rig.conn.cursor() as cur:
        cur.execute("SELECT actor, args::text FROM admin_actions WHERE action = %s", (action,))
        return cur.fetchall()


# ── sign-in and sessions ──────────────────────────────────────────────────────────────────────
def test_sign_in_sets_a_hardened_session_cookie_and_never_returns_the_token(rig):
    response = _sign_in(rig, MEMBER)

    assert response.status_code == 200
    assert response.json()["subject"] == MEMBER and response.json()["is_admin"] is False
    cookie = response.headers["set-cookie"]
    for attribute in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/admin"):
        assert attribute in cookie
    assert rig.tokens[MEMBER] not in response.text and rig.tokens[MEMBER] not in cookie
    me = rig.client.get("/admin/api/me", headers={"Cookie": _cookie(response)})
    assert me.status_code == 200 and me.json()["subject"] == MEMBER


def test_sign_in_requires_the_csrf_header_and_a_same_host_origin(rig):
    missing = _sign_in(rig, MEMBER, headers={"Origin": "http://testserver"})
    foreign = _sign_in(rig, MEMBER, headers={"X-Stigmergy-CSRF": "1",
                                             "Origin": "https://attacker.example"})
    no_origin = _sign_in(rig, MEMBER, headers={"X-Stigmergy-CSRF": "1"})

    for response in (missing, foreign, no_origin):
        assert response.status_code == 403
        assert "set-cookie" not in response.headers


def test_refused_sign_ins_are_indistinguishable(rig):
    with rig.conn.cursor() as cur:
        cur.execute("UPDATE roster_principals SET disabled_at = now() WHERE subject = %s",
                    (ADMIN,))
    unknown = rig.client.post("/admin/api/session", json={"token": "stg_unknown"}, headers=CSRF)
    disabled = _sign_in(rig, ADMIN)

    assert unknown.status_code == disabled.status_code == 401
    assert unknown.json() == disabled.json()


def test_repeated_failures_throttle_the_address_but_never_a_valid_token(rig):
    for _ in range(5):
        rig.client.post("/admin/api/session", json={"token": "stg_wrong"}, headers=CSRF)

    throttled = rig.client.post("/admin/api/session", json={"token": "stg_wrong"}, headers=CSRF)

    assert throttled.status_code == 429
    assert _sign_in(rig, MEMBER).status_code == 200


def test_an_issued_token_signs_in_but_the_api_refuses_it_as_a_bearer(rig):
    token = rig.client.post(f"/admin/api/people/{MEMBER}/tokens", json={"label": "laptop"},
                            headers={**_session(rig, ADMIN), **CSRF}).json()["token"]

    signed_in = rig.client.post("/admin/api/session", json={"token": token}, headers=CSRF)

    assert signed_in.status_code == 200 and signed_in.json()["subject"] == MEMBER
    for credential in (token, rig.tokens[ADMIN]):
        assert rig.client.get("/admin/api/people", headers={
            "Authorization": f"Bearer {credential}"}).status_code == 401


def test_bodies_are_capped_before_they_are_parsed(rig):
    oversized_sign_in = rig.client.post(
        "/admin/api/session", headers=CSRF,
        content=b'{"token": "' + b"x" * 5000 + b'"}')
    admin = {**_session(rig, ADMIN), **CSRF}
    oversized_json = rig.client.post("/admin/api/groups", headers=admin,
                                     content=b'{"name": "' + b"x" * 300_000 + b'"}')
    nested = rig.client.post("/admin/api/groups", headers=admin, content=b"[" * 3000 + b"]" * 3000)

    assert oversized_sign_in.status_code == oversized_json.status_code == 413
    assert nested.status_code == 400


def test_sign_out_ends_the_session_immediately(rig):
    session = _session(rig, MEMBER)
    out = rig.client.delete("/admin/api/session", headers={**session, **CSRF})

    assert out.status_code == 200
    assert "Max-Age=0" in out.headers["set-cookie"]
    assert rig.client.get("/admin/api/me", headers=session).status_code == 401


def test_a_cookie_state_change_without_the_csrf_header_is_refused(rig):
    session = _session(rig, MEMBER)

    refused = rig.client.delete("/admin/api/session", headers=session)

    assert refused.status_code == 403
    assert rig.client.get("/admin/api/me", headers=session).status_code == 200


def test_sign_in_rotates_the_session_and_closes_the_one_it_replaces(rig):
    first = _session(rig, MEMBER)
    second = rig.client.post("/admin/api/session", json={"token": rig.tokens[MEMBER]},
                             headers={**first, **CSRF})
    fixated = {"Cookie": f"{auth.SESSION_COOKIE}=attacker-chosen-value"}

    assert _cookie(second) != first["Cookie"]
    assert rig.client.get("/admin/api/me", headers=first).status_code == 401
    assert rig.client.get("/admin/api/me", headers={"Cookie": _cookie(second)}).status_code == 200
    assert rig.client.get("/admin/api/me", headers=fixated).status_code == 401


@pytest.mark.parametrize("change", [
    "expires_at = now() - interval '1 second'",
    "last_seen_at = now() - interval '9 hours'",
    "revoked_at = now()",
])
def test_expired_idle_and_revoked_sessions_are_refused(rig, change):
    session = _session(rig, MEMBER)
    with rig.conn.cursor() as cur:
        cur.execute(f"UPDATE roster_sessions SET {change}")

    assert rig.client.get("/admin/api/me", headers=session).status_code == 401


def test_revoking_the_token_or_disabling_the_person_ends_their_sessions(rig):
    member = _session(rig, MEMBER)
    admin = _session(rig, ADMIN)
    tokens = rig.client.get(f"/admin/api/people/{MEMBER}", headers=admin).json()["tokens"]

    revoked = rig.client.delete(f"/admin/api/tokens/{tokens[0]['id']}", headers={**admin, **CSRF})

    assert revoked.status_code == 200
    assert rig.client.get("/admin/api/me", headers=member).status_code == 401
    rig.tokens[MEMBER] = rig.client.post(f"/admin/api/people/{MEMBER}/tokens",
                                         json={"label": "again"},
                                         headers={**admin, **CSRF}).json()["token"]
    member = _session(rig, MEMBER)
    rig.client.put(f"/admin/api/people/{MEMBER}/disabled", json={"disabled": True},
                   headers={**admin, **CSRF})
    assert rig.client.get("/admin/api/me", headers=member).status_code == 401
    assert _sign_in(rig, MEMBER).status_code == 401


# ── roles ─────────────────────────────────────────────────────────────────────────────────────
def _admin_endpoints(rig):
    gate = rig.app._admin
    samples = {"subject": MEMBER, "token_id": "00000000-0000-4000-8000-000000000000",
               "name": "product", "channel_id": "C0PRODUCT1", "capture_id": "x", "change_id": "x",
               "entity": "Acme"}
    for route in gate._app.state.admin_routes:
        for method in sorted(route.methods - {"HEAD"}):
            path = route.path
            for key, value in samples.items():
                path = path.replace(f"{{{key}}}", value)
            yield method, path


def test_a_member_meets_every_admin_endpoint_as_an_unknown_route(rig):
    session = {**_session(rig, MEMBER), **CSRF}
    unknown = rig.client.request("GET", "/admin/api/does-not-exist", headers=session)
    endpoints = list(_admin_endpoints(rig))
    assert len(endpoints) > 30

    for method, path in endpoints:
        response = rig.client.request(method, path, headers=session, json={"label": "x"})
        assert response.status_code == unknown.status_code == 404, (method, path)
        assert response.json() == unknown.json() == {"error": "not found"}
    assert len(_actions(rig, "backoffice.denied")) == len(endpoints) + 1


def test_an_agent_signs_in_to_read_the_wiki_and_meets_no_admin_endpoint(rig):
    session = {**_session(rig, AGENT), **CSRF}

    assert rig.client.get("/admin/api/me", headers=session).json()["is_admin"] is False
    assert rig.client.get("/admin/api/wiki/pages", headers=session).status_code == 200
    for method, path in _admin_endpoints(rig):
        response = rig.client.request(method, path, headers=session, json={"label": "x"})
        assert response.status_code == 404, (method, path)


# ── roster administration ─────────────────────────────────────────────────────────────────────
def test_an_issued_token_is_shown_once_and_never_recorded(rig):
    admin = {**_session(rig, ADMIN), **CSRF}

    grant = rig.client.post(f"/admin/api/people/{MEMBER}/tokens",
                            json={"label": "laptop"}, headers=admin).json()

    assert grant["token"].startswith("stg_") and len(grant["token"]) == 47
    detail = rig.client.get(f"/admin/api/people/{MEMBER}", headers=admin).text
    assert grant["token"] not in detail
    assert grant["prefix"] in detail
    with rig.conn.cursor() as cur:
        cur.execute("SELECT args::text FROM admin_actions UNION ALL "
                    "SELECT args::text FROM audit_log UNION ALL "
                    "SELECT sha256 FROM roster_tokens UNION ALL SELECT sha256 FROM roster_sessions")
        stored = " ".join(row[0] for row in cur.fetchall())
    assert grant["token"] not in stored
    assert rig.tokens[MEMBER] not in stored


@pytest.mark.parametrize("payload,message", [
    ({"subject": "bot@example.com", "display_name": "Bot", "kind": "agent", "is_admin": True,
      "groups": ["brain-admins"]}, "only a person"),
    ({"subject": "lead@example.com", "display_name": "Lead", "is_admin": True,
      "groups": ["product"]}, "must belong to brain-admins"),
    ({"subject": "x@example.com", "display_name": "X", "groups": ["no-such-group"]},
     "does not exist"),
    ({"subject": "y@example.com", "display_name": "Y", "groups": ["product"],
      "default_audience": ["brain-admins"]}, "default layer must be"),
])
def test_invalid_people_are_refused_at_save(rig, payload, message):
    admin = {**_session(rig, ADMIN), **CSRF}
    before = rig.client.get("/admin/api/people", headers=admin).json()

    refused = rig.client.post("/admin/api/people", json=payload, headers=admin)

    assert refused.status_code == 400
    assert message in refused.json()["error"]
    assert rig.client.get("/admin/api/people", headers=admin).json() == before


def test_the_last_administrator_cannot_be_removed_in_any_way(rig):
    admin = {**_session(rig, ADMIN), **CSRF}
    for response in (
        rig.client.put(f"/admin/api/people/{ADMIN}", json={"is_admin": False}, headers=admin),
        rig.client.put(f"/admin/api/people/{ADMIN}/disabled", json={"disabled": True},
                       headers=admin),
        rig.client.delete(f"/admin/api/people/{ADMIN}", headers=admin),
    ):
        assert response.status_code == 400
    assert rig.client.get("/admin/api/me", headers=admin).json()["is_admin"] is True


def test_group_changes_take_effect_on_the_next_wiki_request(rig):
    admin = {**_session(rig, ADMIN), **CSRF}
    member = _session(rig, MEMBER)
    rig.client.post("/admin/api/groups", json={"name": "finance"}, headers=admin)
    assert rig.client.get(f"/admin/api/wiki/page?path={FINANCE}", headers=member).status_code == 404

    granted = rig.client.put(f"/admin/api/people/{MEMBER}", headers=admin, json={
        "display_name": "Member", "groups": ["product", "finance"],
        "default_audience": ["product"]})

    assert granted.status_code == 200
    assert rig.client.get(f"/admin/api/wiki/page?path={FINANCE}", headers=member).status_code == 200


def test_only_an_administrator_erases_a_layer_with_its_name_typed_and_it_is_audited(rig):
    ensure_capture_schema(rig.conn)
    with rig.conn.cursor() as cur:
        cur.execute("DELETE FROM layer_erasures")
    admin = {**_session(rig, ADMIN), **CSRF}
    member = {**_session(rig, MEMBER), **CSRF}
    rig.client.post("/admin/api/groups", json={"name": "finance"}, headers=admin)
    path = "/admin/api/groups/finance/erasure"

    assert rig.client.post(path, json={"confirm": "finance"}, headers=member).status_code == 404
    view = rig.client.get(path, headers=admin).json()
    assert view["latest"] is None
    assert view["inventory"]["pages_total"] == 1 and view["inventory"]["pages"] == {"note": 1}
    for target, confirm, message in (("finance", "Finance", "type the layer's name"),
                                     ("public", "public", "only a restricted layer"),
                                     ("payroll", "payroll", "does not exist")):
        refused = rig.client.post(f"/admin/api/groups/{target}/erasure", json={"confirm": confirm},
                                  headers=admin)
        assert refused.status_code == 400 and message in refused.json()["error"], target
    assert not erasure.is_erasing(rig.conn, "finance")

    queued = rig.client.post(path, json={"confirm": "finance"}, headers=admin)
    again = rig.client.post(path, json={"confirm": "finance"}, headers=admin)

    assert queued.status_code == 200
    assert queued.json()["status"] == "queued" and queued.json()["requested_by"] == ADMIN
    assert "rewrite" not in queued.json() and "garbage" not in queued.json()
    groups = rig.client.get("/admin/api/groups", headers=admin).json()["groups"]
    assert [group["name"] for group in groups if group["erasing"]] == ["finance"]
    assert again.status_code == 400 and "already under way" in again.json()["error"]
    assert rig.client.get(path, headers=admin).json()["latest"]["id"] == queued.json()["id"]
    with rig.conn.cursor() as cur:
        cur.execute("SELECT outcome, args->>'name' FROM admin_actions WHERE action = 'layer.erase' "
                    "ORDER BY id")
        assert cur.fetchall() == [("error", "public"), ("error", "payroll"), ("ok", "finance"),
                                  ("error", "finance")]
        cur.execute("DELETE FROM layer_erasures")       # an open request closes the layer suite-wide


# ── the wiki ──────────────────────────────────────────────────────────────────────────────────
def test_a_member_sees_the_same_wiki_whether_or_not_hidden_pages_exist(rig):
    member = _session(rig, MEMBER)

    pages = rig.client.get("/admin/api/wiki/pages", headers=member).json()["pages"]
    roadmap = rig.client.get(f"/admin/api/wiki/page?path={PUBLIC}", headers=member).json()
    acme = rig.client.get(f"/admin/api/wiki/page?path={ENTITY}", headers=member).json()

    assert FINANCE not in {page["path"] for page in pages}
    assert all(link["path"] != FINANCE for link in roadmap["links"])
    assert "Payroll" not in roadmap["html"].replace("wikilink-missing\">Payroll", "")
    assert '<span class="wikilink-missing">Payroll</span>' in roadmap["html"]
    assert [item["path"] for item in acme["backlinks"]] == [PUBLIC]
    assert FINANCE not in {node["path"] for node in acme["graph"]["nodes"]}
    assert all(FINANCE not in (edge["from"], edge["to"]) for edge in acme["graph"]["edges"])


def test_a_member_sees_byte_identical_wiki_responses_without_the_hidden_pages(rig):
    member = _session(rig, MEMBER)

    def responses():
        return [rig.client.get(url, headers=member).json() for url in (
            "/admin/api/wiki/pages", "/admin/api/wiki/graph", f"/admin/api/wiki/page?path={ENTITY}",
            f"/admin/api/wiki/page?path={PUBLIC}", "/admin/api/wiki/search?q=acme payroll",
            "/admin/api/wiki/status")]

    with_hidden = responses()
    (rig.repo / FINANCE_FILE).unlink()
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit",
                    "-qam", "drop hidden"], cwd=rig.repo, check=True)
    build.rebuild(rig.conn, str(rig.repo), build_embedder("fake"))

    assert responses() == with_hidden


def test_hidden_and_missing_pages_are_the_same_absence(rig):
    member = _session(rig, MEMBER)

    hidden = rig.client.get(f"/admin/api/wiki/page?path={FINANCE}", headers=member)
    missing = rig.client.get("/admin/api/wiki/page?path=wiki/notes/Nope.md", headers=member)

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json() == {"error": "not found"}


def test_search_returns_no_hidden_page(rig):
    member = _session(rig, MEMBER)

    hits = rig.client.get("/admin/api/wiki/search?q=payroll", headers=member).json()["hits"]

    assert FINANCE not in {hit["path"] for hit in hits}


def test_a_restricted_page_shows_the_layers_it_is_read_from(rig):
    admin = _session(rig, ADMIN)

    page = rig.client.get(f"/admin/api/wiki/page?path={FINANCE}", headers=admin).json()
    by_file = rig.client.get(f"/admin/api/wiki/page?path={FINANCE_FILE}", headers=admin).json()

    assert page["layers"] == ["finance"]
    assert by_file == page


def test_hostile_markdown_renders_inert(rig):
    member = _session(rig, MEMBER)

    page = rig.client.get(f"/admin/api/wiki/page?path={HOSTILE}", headers=member).json()
    elements = _Elements()
    elements.feed(page["html"])

    assert {tag for tag, _attrs in elements.seen} <= {"h1", "p", "a", "span"}
    for _tag, attrs in elements.seen:
        assert all(not name.startswith("on") and name != "style" for name, _value in attrs)
    hrefs = [value for _tag, attrs in elements.seen for name, value in attrs if name == "href"]
    assert hrefs == ["https://example.com/docs"]
    assert "&lt;script&gt;" in page["html"]


def test_every_wiki_read_is_audited_under_the_reader(rig):
    member = _session(rig, MEMBER)
    rig.client.get(f"/admin/api/wiki/page?path={FINANCE}", headers=member)
    rig.client.get("/admin/api/wiki/pages", headers=member)

    with rig.conn.cursor() as cur:
        cur.execute("SELECT identity, tool FROM audit_log ORDER BY id")
        rows = cur.fetchall()

    assert (MEMBER, "browse_page") in rows and (MEMBER, "browse_pages") in rows
