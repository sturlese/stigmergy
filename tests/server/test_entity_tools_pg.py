"""`list_entities` / `describe_entity`: the ACL-scoped entity vocabulary and the layered describe
view (entity meta + page, view ref, dated timeline). Its own small corpus + registry + three
identities, isolated from `tests/server/conftest.py`'s shared `Fixture` — same posture
`tests/answer/test_entity_first_pg.py` already takes for the same reason (a registry would change
what THAT fixture's many other consumers exercise).
"""
import json
import os

import pytest

from stigmergy.index import build
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.errors import RegistryError
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import connect_or_skip, write_page

ACME_ENTITY_PAGE = "wiki/entities/acme.md"
ACME_DECISION_DATED = "wiki/decisions/acme-decision-2026.md"
ACME_DECISION_DATED_OLDER = "wiki/decisions/acme-decision-2025.md"
ACME_DECISION_UNDATED = "wiki/decisions/acme-decision-undated.md"
ACME_RESTRICTED_MEMBER = "wiki/finance/acme-payroll.md"
ACME_VIEW = "views/acme.md"
UNREGISTERED_PAGE = "wiki/notes/ghostco-note.md"        # anchored to "ghostco" — unregistered
VAULT_RESTRICTED_PAGE = "wiki/finance/vault-corp-note.md"  # "vault-corp" — wholly finance-only


class _EntityDocsFixture:
    STEWARD = "steward@example.com"     # unrestricted
    ANA = "ana@example.com"       # scoped to ["finance"]
    ENG = "eng@example.com"       # scoped to ["eng"] only — never "finance"

    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        self.identities_path = os.path.join(self.repo, "ops", "identities.json")
        ops_dir = os.path.join(self.repo, "ops")
        os.makedirs(ops_dir, exist_ok=True)
        self.entity_registry_path = os.path.join(ops_dir, "entity-registry.json")
        with open(self.entity_registry_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"entities": {
                "acme": {"name": "Acme Corp", "type": "organization", "aliases": ["Acme"]},
                "vault-corp": {"name": "Vault Corp", "type": "organization", "aliases": []},
            }}))

        write_page(self.repo, ACME_ENTITY_PAGE,
                  {"type": "entity", "title": "Acme Corp", "entity": "['acme']",
                   "verification": "verified"},
                  "Acme Corp is a governed entity page.")
        write_page(self.repo, ACME_DECISION_DATED,
                  {"type": "decision", "title": "Acme Decision 2026", "entity": "['acme']",
                   "as_of": "2026-06-01", "status": "canonical", "verification": "verified"},
                  "A dated decision about Acme, the newer one.")
        write_page(self.repo, ACME_DECISION_DATED_OLDER,
                  {"type": "decision", "title": "Acme Decision 2025", "entity": "['acme']",
                   "as_of": "2025-01-15", "status": "canonical", "verification": "verified"},
                  "A dated decision about Acme, the older one.")
        write_page(self.repo, ACME_DECISION_UNDATED,
                  {"type": "note", "title": "Acme Undated Note", "entity": "['acme']",
                   "verification": "verified"},
                  "An undated note about Acme.")
        write_page(self.repo, ACME_RESTRICTED_MEMBER,
                  {"type": "report", "title": "Acme Payroll (restricted)", "entity": "['acme']",
                   "as_of": "2026-02", "verification": "verified", "acl": "['finance']"},
                  "Restricted payroll detail for Acme.")
        write_page(self.repo, ACME_VIEW,
                  {"type": "view", "title": "Acme Corp — view", "entity": "['acme']",
                   "verification": "partial", "generated_at": '"2026-07-20T10:00:00+00:00"'},
                  "## Timeline\n\nView rollup for Acme.")
        write_page(self.repo, UNREGISTERED_PAGE,
                  {"type": "note", "title": "GhostCo Sighting", "entity": "['ghostco']",
                   "verification": "verified"},
                  "A page anchored to an entity nobody registered.")
        write_page(self.repo, VAULT_RESTRICTED_PAGE,
                  {"type": "note", "title": "Vault Corp Note", "entity": "['vault-corp']",
                   "verification": "verified", "acl": "['finance']"},
                  "Vault Corp's only anchored page, finance-only.")


        os.makedirs(os.path.dirname(self.identities_path), exist_ok=True)
        with open(self.identities_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({self.STEWARD: "*", self.ANA: ["finance"], self.ENG: ["eng"]}))


@pytest.fixture(scope="module")
def entity_docs_indexed(tmp_path_factory):
    """This repo CARRIES an `ops/entity-registry.json`, so the rebuild caches it as the index's
    registry snapshot — a singleton row in a database every suite shares, and the one the server
    prefers over its `--entity-registry` file (issue #74). Cleared on the way out: whether this
    module leaves one behind (and which one — two tests below clear it mid-module) would otherwise
    depend on collection order, and it would change what an unrelated suite's `describe_entity`
    resolves."""
    fx = _EntityDocsFixture(str(tmp_path_factory.mktemp("entity-docs")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    conn.close()


def _service(conn, fx, identity_name: str, *, entity_registry_path=None) -> BrainService:
    from stigmergy.server.identity import resolve_audiences
    audiences_tuple = resolve_audiences(fx.identities_path, identity_name)
    audiences = set(audiences_tuple) if audiences_tuple is not None else None
    settings = Settings(identity=identity_name, identities_path=fx.identities_path,
                        entity_registry_path=(fx.entity_registry_path
                                              if entity_registry_path is None
                                              else entity_registry_path))
    return BrainService(settings, conn, build_embedder("fake"), audiences, identity=identity_name)


# ── list_entities ──────────────────────────────────────────────────────────────────────────────

def _service_with_registry(fixture, conn, registry_path: str):
    """A `BrainService` for the unrestricted identity, pointed at a specific entity registry —
    `make_service` wires no registry, and these two cases are ABOUT the registry."""
    from stigmergy.index.backends.embedder import build_embedder
    from stigmergy.server.service import BrainService
    from stigmergy.server.settings import Settings
    settings = Settings(identity=fixture.STEWARD, identities_path=fixture.identities_path,
                        entity_registry_path=registry_path)
    return BrainService(settings, conn, build_embedder("fake"), None, identity=fixture.STEWARD)

def test_list_entities_serves_exactly_the_scoped_entities_id_set_enriched(entity_docs_indexed):
    conn, fx = entity_docs_indexed
    svc = _service(conn, fx, fx.STEWARD)
    out = svc.list_entities()
    by_id = {e["id"]: e for e in out["entities"]}
    assert set(by_id) == set(svc.scoped_entities())
    assert by_id["acme"] == {"id": "acme", "name": "Acme Corp", "type": "organization",
                             "aliases": ["Acme"], "approved_by": ""}
    assert out["count"] == len(out["entities"]) == len(by_id)


def test_list_entities_registry_absent_id_serves_bare_id_alone(entity_docs_indexed):
    """`ghostco` is anchored (a real page names it) but never registered — served honestly as
    `{id}` alone, never dropped and never invented metadata."""
    conn, fx = entity_docs_indexed
    svc = _service(conn, fx, fx.STEWARD)
    by_id = {e["id"]: e for e in svc.list_entities()["entities"]}
    assert by_id["ghostco"] == {"id": "ghostco"}


def test_list_entities_is_scoped_like_scoped_entities(entity_docs_indexed):
    """`vault-corp`'s only page is finance-only: absent from ENG's list, present in ANA's."""
    conn, fx = entity_docs_indexed
    eng_ids = {e["id"] for e in _service(conn, fx, fx.ENG).list_entities()["entities"]}
    ana_ids = {e["id"] for e in _service(conn, fx, fx.ANA).list_entities()["entities"]}
    assert "vault-corp" not in eng_ids
    assert "vault-corp" in ana_ids


def test_list_entities_registry_missing_serves_ids_only(entity_docs_indexed):
    """Registry missing means missing on BOTH roads (issue #74): the index's snapshot is the
    service's first source and the `--entity-registry` file is the fallback, so the fixture's
    rebuild — which cached this repo's registry — has to be cleared for a bogus path to be the
    whole answer."""
    conn, fx = entity_docs_indexed
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    svc = _service(conn, fx, fx.STEWARD, entity_registry_path="/nonexistent/entity-registry.json")
    out = svc.list_entities()
    assert out["entities"]
    assert all(set(e) == {"id"} for e in out["entities"])


def test_list_entities_registry_malformed_raises_loudly(entity_docs_indexed, tmp_path):
    """Loudly, and as `RegistryError` — not the loader's bare `ValueError` it used to let out.
    `list_entities` was the one registry reader that reached the loader directly, so a message
    naming the registry's filesystem PATH left the service; every reader now goes through
    `_registry_records`, which converts it (`errors.RegistryError`'s own reason for existing)."""
    conn, fx = entity_docs_indexed
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)      # the file road is the one under test
    bad = tmp_path / "bad-registry.json"
    bad.write_text(json.dumps({"not-entities": {}}))
    svc = _service(conn, fx, fx.STEWARD, entity_registry_path=str(bad))
    with pytest.raises(RegistryError):
        svc.list_entities()


# ── describe_entity ────────────────────────────────────────────────────────────────────────────
def test_describe_entity_entity_layer_registry_meta_plus_own_page(entity_docs_indexed):
    conn, fx = entity_docs_indexed
    out = _service(conn, fx, fx.STEWARD).describe_entity("acme")
    assert out["entity"] == {
        "id": "acme", "name": "Acme Corp", "type": "organization", "aliases": ["Acme"],
        "approved_by": "",
        "page": {"path": ACME_ENTITY_PAGE, "title": "Acme Corp"},
    }


def test_describe_entity_view_layer_ref_or_null(entity_docs_indexed):
    conn, fx = entity_docs_indexed
    acme = _service(conn, fx, fx.STEWARD).describe_entity("acme")
    # no `verification` — nothing computes a verdict, so the ref does not carry one.
    assert acme["view"] == {
        "path": ACME_VIEW, "title": "Acme Corp — view",
        "generated_at": "2026-07-20T10:00:00+00:00",
    }
    # an entity with no view at all -> null, never a missing key or a synthesized placeholder
    vault = _service(conn, fx, fx.ANA).describe_entity("vault-corp")
    assert vault["view"] is None


def test_describe_entity_timeline_dated_first_desc_then_undated_by_path_excludes_self_and_view(
        entity_docs_indexed):
    conn, fx = entity_docs_indexed
    out = _service(conn, fx, fx.STEWARD).describe_entity("acme")
    paths = [item["path"] for item in out["timeline"]]
    # the entity's own page and its view are never timeline members
    assert ACME_ENTITY_PAGE not in paths
    assert ACME_VIEW not in paths
    # dated entries first, newest as_of first (2026-06-01 > 2026-02 > 2025-01-15), undated last
    assert paths.index(ACME_DECISION_DATED) < paths.index(ACME_RESTRICTED_MEMBER)
    assert paths.index(ACME_RESTRICTED_MEMBER) < paths.index(ACME_DECISION_DATED_OLDER)
    assert paths.index(ACME_DECISION_DATED_OLDER) < paths.index(ACME_DECISION_UNDATED)
    item = next(i for i in out["timeline"] if i["path"] == ACME_DECISION_DATED)
    assert item["type"] == "decision" and item["status"] == "canonical" and item["as_of"] == "2026-06-01"
    undated = next(i for i in out["timeline"] if i["path"] == ACME_DECISION_UNDATED)
    assert undated["as_of"] == ""


def test_describe_entity_timeline_is_existence_scoped_per_member(entity_docs_indexed):
    """The restricted payroll member never reaches ENG's timeline (finance-only), but does reach
    an unrestricted or finance-scoped identity's."""
    conn, fx = entity_docs_indexed
    steward_paths = {i["path"] for i in _service(conn, fx, fx.STEWARD).describe_entity("acme")["timeline"]}
    eng_paths = {i["path"] for i in _service(conn, fx, fx.ENG).describe_entity("acme")["timeline"]}
    assert ACME_RESTRICTED_MEMBER in steward_paths
    assert ACME_RESTRICTED_MEMBER not in eng_paths



def test_describe_entity_resolves_id_name_and_alias_through_the_one_registry_loader(
        entity_docs_indexed):
    """Id, canonical name, and a declared alias all resolve to the same entity."""
    conn, fx = entity_docs_indexed
    svc = _service(conn, fx, fx.STEWARD)
    for spelling in ("acme", "Acme Corp", "Acme"):
        assert svc.describe_entity(spelling)["entity"]["id"] == "acme"


def test_describe_entity_unknown_and_out_of_scope_are_byte_identical_absence(entity_docs_indexed):
    """A genuinely unregistered name and a registered-but-wholly-out-of-scope entity (vault-corp,
    from ENG's view — its only page is finance-only) return the identical shape."""
    conn, fx = entity_docs_indexed
    eng = _service(conn, fx, fx.ENG)
    nonexistent = eng.describe_entity("totally-made-up-entity")
    out_of_scope = eng.describe_entity("vault-corp")
    assert set(nonexistent) == set(out_of_scope) == {"error"}
    assert nonexistent["error"] == "unknown entity: totally-made-up-entity"
    assert out_of_scope["error"] == "unknown entity: vault-corp"
    # and the finance-scoped identity CAN describe it — proving it really exists behind that shape
    assert "error" not in _service(conn, fx, fx.ANA).describe_entity("vault-corp")


def test_describe_entity_an_anchored_but_unregistered_id_now_resolves_for_an_identity_that_can_see_it(
        entity_docs_indexed):
    """`ghostco` is anchored (real page, real content) but absent from the registry. Resolution
    used to run STRICTLY through the registry loader, so `describe_entity` answered "unknown" even
    though `list_entities` surfaced it as a bare id — breaking the navigation loop exactly where
    the registry is incomplete. Resolution now also accepts VERBATIM membership of the caller's
    own `scoped_entities()` set (the same existence rule the absence gate already consults, not a
    second resolver), so `ghostco` resolves honestly, with no registry metadata to invent: empty
    name/aliases, no self-anchored page."""
    conn, fx = entity_docs_indexed
    svc = _service(conn, fx, fx.STEWARD)
    assert "ghostco" in svc.scoped_entities()
    out = svc.describe_entity("ghostco")
    assert "error" not in out
    assert out["entity"] == {"id": "ghostco", "name": "", "type": "", "aliases": [],
                             "approved_by": "",
                             "page": None}
    assert out["view"] is None


def test_describe_entity_scoped_set_fallback_is_verbatim_never_normalized(entity_docs_indexed):
    """The fallback is EXACT raw-string membership — a differently-cased or
    accented spelling of an anchored-but-unregistered id must not resolve just because the
    registry-style normalizer WOULD have folded it to the same key; scoped ids are index facts,
    not free text a person typed."""
    conn, fx = entity_docs_indexed
    svc = _service(conn, fx, fx.STEWARD)
    assert "ghostco" in svc.scoped_entities()
    assert svc.describe_entity("GhostCo") == {"error": "unknown entity: GhostCo"}
    assert svc.describe_entity("ghost co") == {"error": "unknown entity: ghost co"}


# ── the timing oracle — scoped_entities() must run the same work either way ────────────────────
def test_describe_entity_calls_scoped_entities_exactly_once_whether_or_not_resolution_succeeded(
        entity_docs_indexed, monkeypatch):
    """Before the fix, `scoped_entities()` sat INSIDE the `or` of `entity_id is None or entity_id
    not in scoped_entities()` — short-circuited away entirely for a never-registered input, so a
    registered-but-out-of-scope id paid a DB query a never-registered one did not. Response
    latency itself was an oracle for "does this name mean anything to the registry at all." A
    call-counting double proves the COUNT, not merely the outcome — an outcome-only assertion
    would stay green even if the short-circuit came back."""
    conn, fx = entity_docs_indexed
    svc = _service(conn, fx, fx.STEWARD)
    calls = []
    real = svc.scoped_entities

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(svc, "scoped_entities", counting)

    svc.describe_entity("acme")                              # resolves (registry match)
    assert len(calls) == 1
    calls.clear()

    svc.describe_entity("ghostco")                            # resolves (scoped-set fallback)
    assert len(calls) == 1
    calls.clear()

    svc.describe_entity("totally-unregistered-and-unanchored")   # never resolves at all
    assert len(calls) == 1


# ── loop closure: list_entities and describe_entity agree on existence for every visible id ─────
def test_describe_entity_closes_the_navigation_loop_with_list_entities(entity_docs_indexed):
    """`list_entities` -> `describe_entity` -> `read_page` is the documented walk: every id
    `list_entities` serves an identity must `describe_entity` successfully for that SAME identity
    — no id "exists" to one tool and "unknown" to the other. The scoped identity's absence for an
    id it cannot see stays byte-identical to a genuinely nonexistent one, proven against the SAME
    identity two different tools already agree is scoped out."""
    conn, fx = entity_docs_indexed
    steward = _service(conn, fx, fx.STEWARD)
    eng = _service(conn, fx, fx.ENG)

    listed = steward.list_entities()["entities"]
    assert listed   # sanity: the fixture actually has entities to loop over
    for record in listed:
        out = steward.describe_entity(record["id"])
        assert "error" not in out, record["id"]
        assert out["entity"]["id"] == record["id"]

    # vault-corp is finance-only: absent from ENG's own list_entities, and its describe_entity
    # absence has the SAME shape as a name nobody ever registered or anchored at all — the
    # messages differ only by echoing each one's own input (the established "byte-identical
    # absence SHAPE" discipline `test_describe_entity_unknown_and_out_of_scope_are_byte_identical_
    # absence` above already pins).
    eng_ids = {e["id"] for e in eng.list_entities()["entities"]}
    assert "vault-corp" not in eng_ids
    nonexistent = eng.describe_entity("totally-made-up-entity-loop-closure")
    out_of_scope = eng.describe_entity("vault-corp")
    assert set(nonexistent) == set(out_of_scope) == {"error"}
    assert nonexistent["error"] == "unknown entity: totally-made-up-entity-loop-closure"
    assert out_of_scope["error"] == "unknown entity: vault-corp"


# ── _entity_own_page_row is deterministic when >1 page self-anchors an id ──────────────────────
_DUP_ID = "duplicate-org"
_DUP_PAGE_A = "wiki/entities/duplicate-org-a.md"      # path-sorted FIRST
_DUP_PAGE_B = "wiki/entities/duplicate-org-z.md"      # path-sorted second


class _DuplicateSelfAnchorFixture:
    STEWARD = "steward@example.com"

    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        self.identities_path = os.path.join(self.repo, "ops", "identities.json")
        self.entity_registry_path = os.path.join(self.repo, "ops", "entity-registry.json")
        os.makedirs(os.path.dirname(self.entity_registry_path), exist_ok=True)
        with open(self.entity_registry_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"entities": {_DUP_ID: {"name": "Duplicate Org", "type": "organization",
                                                        "aliases": []}}}))
        # a data anomaly the code must still handle deterministically: TWO pages both self-anchor
        # the SAME entity id (`type: entity`, `entity_id = ANY(entity)`) — never supposed to
        # happen, but "never supposed to happen" is exactly what ORDER BY exists to make safe.
        write_page(self.repo, _DUP_PAGE_A,
                  {"type": "entity", "title": "Duplicate Org A", "entity": f"['{_DUP_ID}']",
                   "verification": "verified"},
                  "First self-anchored page.")
        write_page(self.repo, _DUP_PAGE_B,
                  {"type": "entity", "title": "Duplicate Org B", "entity": f"['{_DUP_ID}']",
                   "verification": "verified"},
                  "Second self-anchored page.")
        os.makedirs(os.path.dirname(self.identities_path), exist_ok=True)
        with open(self.identities_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({self.STEWARD: "*"}))


@pytest.fixture(scope="module")
def duplicate_self_anchor_indexed(tmp_path_factory):
    """Same snapshot hygiene as `entity_docs_indexed` above: this repo carries a registry too, so
    the rebuild caches one into the shared singleton row."""
    fx = _DuplicateSelfAnchorFixture(str(tmp_path_factory.mktemp("dup-self-anchor")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    conn.close()


def test_describe_entity_page_ref_is_deterministic_across_repeated_calls_when_multiply_anchored(
        duplicate_self_anchor_indexed):
    """Without `ORDER BY path`, which of two self-anchored rows `LIMIT 1` returns is whatever the
    scan order happens to be — this pins the path-sorted-first contract explicitly, and that it
    stays stable across repeated calls in the same process (a flip between calls would be the
    unordered-scan symptom, not a one-off)."""
    conn, fx = duplicate_self_anchor_indexed
    svc = _service(conn, fx, fx.STEWARD)
    results = [svc.describe_entity(_DUP_ID)["entity"]["page"]["path"] for _ in range(5)]
    assert results == [_DUP_PAGE_A] * 5



def test_a_scoped_raw_id_still_resolves_when_the_registry_folds_it_out_of_scope(indexed,
                                                                                tmp_path):
    """OLD BEHAVIOUR: `describe_entity` refused an id `list_entities` was advertising.

    The resolution was `resolve_exact(...) or (entity if entity in scoped else None)`, and `or`
    short-circuits on ANY truthy registry hit — so when the registry folded the caller's input to
    a canonical id that is NOT in scope, the scoped-set fallback was never tried, even though the
    caller's raw input WAS a scoped id.

    That is exactly the drift the fallback exists for (ADR 022 D5, restated in `server/index.md`
    and `docs/reference/server.md`): pages anchored to a display name while the registry uses a
    slug. The two tools then disagreed about which ids exist — `list_entities` served it, this one
    called it unknown.
    """
    conn, fixture = indexed
    registry = tmp_path / "entity-registry.json"
    # `globex` IS anchored in the fixture corpus and therefore scoped; the registry knows it only
    # as an ALIAS of a differently-named canonical id, which is not anchored anywhere.
    registry.write_text(json.dumps({"entities": {
        "globex-robotics": {"name": "Globex Robotics", "type": "organization",
                            "aliases": ["Globex"]},
    }}), encoding="utf-8")

    service = _service_with_registry(fixture, conn, str(registry))

    assert "globex" in service.scoped_entities()
    assert "error" not in service.describe_entity("globex"), \
        "list_entities advertises this id; describe_entity must not call it unknown"


def test_a_registry_id_that_IS_in_scope_still_wins(indexed, tmp_path):
    """The benign twin: the registry hit must keep winning whenever it is in scope, so an alias
    still resolves to its canonical page rather than being read as a raw id."""
    conn, fixture = indexed
    registry = tmp_path / "entity-registry.json"
    registry.write_text(json.dumps({"entities": {
        "globex": {"name": "Globex", "type": "organization", "aliases": ["Globex Inc"]},
    }}), encoding="utf-8")

    service = _service_with_registry(fixture, conn, str(registry))

    described = service.describe_entity("Globex Inc")
    assert "error" not in described
    assert described["entity"]["id"] == "globex"
