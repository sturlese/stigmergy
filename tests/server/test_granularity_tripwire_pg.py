"""The granularity tripwire — one aboutness per page, because granularity IS part of anchoring.

The entity door is built entirely on per-page `entity:` anchoring — `pages_index.entity`, read as
`<id> = ANY(entity)` by BOTH `BrainService.search`'s `entity` filter and `describe_entity`'s
timeline query. A page that bundles several subjects into one company-wide filing with
`entity: []` is therefore STRUCTURALLY invisible to both mechanisms, however clearly its own title
and body name those subjects: a page that would be company-wide because it bundles several
subjects is a granularity error. The identical content filed one page per subject is fully visible
through the same two mechanisms.

This is the standing instrument for that rule: the SAME three commitments, filed two ways, in the
SAME test run over the SAME content — so a silent granularity collapse (the entity door quietly
stops noticing bundled pages) cannot hide behind the two filings drifting apart in wording. The
merged filing's invisibility is proven alongside its own benign twin: the merged page is a
completely ordinary, ACL-open, indexed page, findable by plain text search and by `read_page` — it
is invisible ONLY through entity anchoring, never broken or ACL-hidden, which is the exact shape
of a granularity collapse nobody would notice (nothing errors) rather than a crash or an
existence-leak case the ACL suites already cover.

Its own small corpus + its own entity registry, isolated from `tests/server/conftest.py`'s shared
`Fixture` — the same posture every other entity-door pg file takes.
"""
import json
import os

import pytest

from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.identity import resolve_audiences
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings
from tests.server.conftest import connect_or_skip, write_page

STEWARD = "steward@example.com"

# Three commitments, three subjects, filed BOTH ways over the same quarter so the two filings are
# lexically comparable — the tripwire is about ANCHORING structure, not about search finding words.
MERGED_PAGE = "wiki/notes/q3-vendor-roundup-merged.md"
SPLIT_VANTAGE = "wiki/notes/vantage-robotics-q3-renewal.md"
SPLIT_COBALT = "wiki/notes/cobalt-freight-q3-pricing.md"
SPLIT_MERIDIAN = "wiki/notes/meridian-health-q3-sla.md"

VANTAGE = "vantage-robotics"
COBALT = "cobalt-freight"
MERIDIAN = "meridian-health"

SUBJECTS = ((VANTAGE, SPLIT_VANTAGE), (COBALT, SPLIT_COBALT), (MERIDIAN, SPLIT_MERIDIAN))

MERGED_BODY = (
    "Three vendor commitments closed this quarter, filed together in one roundup. Vantage "
    "Robotics agreed to a Q3 renewal commitment at the current rate. Cobalt Freight locked in "
    "a Q3 pricing commitment on the new per-mile schedule. Meridian Health accepted a Q3 SLA "
    "commitment on the revised terms. All three effective immediately.")


class _GranularityFixture:
    def __init__(self, root: str):
        self.repo = os.path.join(root, "repo")
        self.identities_path = os.path.join(self.repo, "ops", "identities.json")
        self.entity_registry_path = os.path.join(self.repo, "ops", "entity-registry.json")

        # THE MERGED FILING: one company-wide page, all three subjects bundled in its own title
        # and body, `entity: []` — the granularity error itself.
        write_page(self.repo, MERGED_PAGE,
                  {"type": "decision", "title": "Q3 Vendor Commitments Roundup",
                   "entity": [], "as_of": "2026-07-01", "verification": "verified"},
                  MERGED_BODY)

        # THE SPLIT FILING: the SAME three commitments, one page per subject, each self-anchored —
        # the repair the brief clause prescribes ("split it, then anchor each piece").
        write_page(self.repo, SPLIT_VANTAGE,
                  {"type": "decision", "title": "Vantage Robotics Q3 Renewal",
                   "entity": [VANTAGE], "as_of": "2026-07-01", "verification": "verified"},
                  "Vantage Robotics agreed to a Q3 renewal commitment at the current rate, "
                  "effective immediately.")
        write_page(self.repo, SPLIT_COBALT,
                  {"type": "decision", "title": "Cobalt Freight Q3 Pricing",
                   "entity": [COBALT], "as_of": "2026-07-01", "verification": "verified"},
                  "Cobalt Freight locked in a Q3 pricing commitment on the new per-mile "
                  "schedule, effective immediately.")
        write_page(self.repo, SPLIT_MERIDIAN,
                  {"type": "decision", "title": "Meridian Health Q3 SLA",
                   "entity": [MERIDIAN], "as_of": "2026-07-01", "verification": "verified"},
                  "Meridian Health accepted a Q3 SLA commitment on the revised terms, "
                  "effective immediately.")

        os.makedirs(os.path.dirname(self.identities_path), exist_ok=True)
        with open(self.identities_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({STEWARD: ["brain-admins"]}))

        # `describe_entity` resolves STRICTLY through the registry — without this,
        # every id below would be "unknown entity" regardless of anchoring, which would make the
        # timeline half of this tripwire pass for the wrong reason: a check that cannot go red for
        # the reason it claims is not a check.
        os.makedirs(os.path.dirname(self.entity_registry_path), exist_ok=True)
        with open(self.entity_registry_path, "w", encoding="utf-8") as f:
            f.write('{"entities": {'
                   '"vantage-robotics": {"name": "Vantage Robotics", "type": "organization", "aliases": []}, '
                   '"cobalt-freight": {"name": "Cobalt Freight", "type": "organization", "aliases": []}, '
                   '"meridian-health": {"name": "Meridian Health", "type": "organization", "aliases": []}'
                   '}}')


@pytest.fixture(scope="module")
def granularity_indexed(tmp_path_factory):
    fx = _GranularityFixture(str(tmp_path_factory.mktemp("granularity-tripwire")))
    conn = connect_or_skip()
    build.rebuild(conn, fx.repo, build_embedder("fake"))
    yield conn, fx
    conn.close()


def _service(conn, fx) -> BrainService:
    audiences_tuple = resolve_audiences(fx.identities_path, STEWARD)
    audiences = set(audiences_tuple) if audiences_tuple is not None else None
    settings = Settings(identity=STEWARD, identities_path=fx.identities_path,
                        entity_registry_path=fx.entity_registry_path)
    return BrainService(settings, conn, build_embedder("fake"), audiences, identity=STEWARD)


# ── the split filing IS visible through both entity-door mechanisms ────────────────────────────
def test_split_filing_is_visible_through_the_entity_filter(granularity_indexed):
    conn, fx = granularity_indexed
    svc = _service(conn, fx)
    for entity_id, split_path in SUBJECTS:
        out = svc.search("Q3 commitment", filters={"entity": entity_id})
        assert any(h["path"] == split_path for h in out["hits"]), \
            f"{entity_id}: split filing {split_path} missing from the entity-filtered search"


def test_split_filing_is_visible_in_describe_entity_timeline(granularity_indexed):
    conn, fx = granularity_indexed
    svc = _service(conn, fx)
    for entity_id, split_path in SUBJECTS:
        out = svc.describe_entity(entity_id)
        assert "error" not in out, f"{entity_id}: describe_entity failed: {out}"
        paths = [item["path"] for item in out["timeline"]]
        assert split_path in paths, f"{entity_id}: split filing {split_path} missing from timeline"


# ── the merged filing is INVISIBLE through both, despite naming all three subjects ──────────────
def test_merged_filing_is_invisible_to_the_entity_filter(granularity_indexed):
    conn, fx = granularity_indexed
    svc = _service(conn, fx)
    for entity_id, _split_path in SUBJECTS:
        out = svc.search("Q3 commitment", filters={"entity": entity_id})
        assert not any(h["path"] == MERGED_PAGE for h in out["hits"]), \
            f"{entity_id}: merged filing {MERGED_PAGE} leaked into the entity-filtered search"


def test_merged_filing_is_invisible_in_describe_entity_timeline(granularity_indexed):
    conn, fx = granularity_indexed
    svc = _service(conn, fx)
    for entity_id, _split_path in SUBJECTS:
        out = svc.describe_entity(entity_id)
        paths = [item["path"] for item in out["timeline"]]
        assert MERGED_PAGE not in paths, \
            f"{entity_id}: merged filing {MERGED_PAGE} leaked into the timeline"


# ── the benign twin: the merged page is not broken or ACL-hidden — plain search finds it fine ───
def test_merged_filing_is_a_normal_visible_page_outside_the_entity_door(granularity_indexed):
    """The tripwire is about ANCHORING, not brokenness: the merged page indexes, is ACL-visible,
    and is lexically findable through plain search and `read_page` — it
    is invisible ONLY to the entity-anchored mechanisms above. Without this twin, the four tests
    above could all be passing because the merged page is simply broken/unindexed, which would
    prove nothing about granularity collapse specifically."""
    conn, fx = granularity_indexed
    svc = _service(conn, fx)
    out = svc.search("vendor roundup commitments quarter")
    assert any(h["path"] == MERGED_PAGE for h in out["hits"])
    page = svc.read_page(MERGED_PAGE)
    assert "error" not in page
    assert page["entity"] == []
