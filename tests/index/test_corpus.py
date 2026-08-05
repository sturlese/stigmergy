"""Corpus loading: zone walk, frontmatter -> columns, wikilink inlinks, content hashing."""
from pathlib import Path

from stigmergy.index import corpus

FIXTURE = str(Path(__file__).parent / "fixtures" / "repo")


def _by_path():
    return {r.path: r for r in corpus.load_pages(FIXTURE)}


# ── entity_list: the bare-string-or-list dialect, normalized ─────────────────────────────────
def test_entity_list_of_none_is_empty():
    assert corpus.entity_list(None) == []


def test_entity_list_of_a_bare_string_is_a_one_element_list():
    """The bare-string dialect (`entity: initech`) stays valid."""
    assert corpus.entity_list("initech") == ["initech"]


def test_entity_list_of_an_empty_string_is_empty_never_a_list_holding_one_empty_string():
    """The empty-scalar edge: `""` -> `[]`, never `[""]`."""
    assert corpus.entity_list("") == []
    assert corpus.entity_list("   ") == []


def test_entity_list_of_a_list_passes_through_as_strings():
    assert corpus.entity_list(["stigmergy", "borealis-dynamics"]) == ["stigmergy", "borealis-dynamics"]


def test_entity_list_of_an_empty_list_is_empty():
    assert corpus.entity_list([]) == []


def test_entity_list_drops_blank_elements_from_a_list():
    assert corpus.entity_list(["stigmergy", "", "  "]) == ["stigmergy"]


# ── fail-CLOSED like `_acl_labels`, not a fail-open normalizer ──────────────────────────────────
def test_entity_list_strips_whitespace_from_each_list_element():
    """Before the fix, a list element was never `.strip()`'d — a trailing space would make the
    membership filter and the boost both miss silently, falling back to unscoped search."""
    assert corpus.entity_list([" initech "]) == ["initech"]


def test_entity_list_drops_a_none_element_from_a_list_rather_than_stringifying_it():
    """A YAML block list with an empty dash item (`entity:\\n  - initech\\n  -\\n`) parses to
    `["initech", None]` — the `None` must be dropped, not stringified into the literal `"None"`."""
    assert corpus.entity_list(["initech", None]) == ["initech"]


def test_entity_list_of_a_yaml_11_boolean_is_empty_not_the_word_false():
    """`entity: no` parses to the Python bool `False` under PyYAML's default (YAML 1.1) resolver.
    Turning it into `["False"]` would fire the boost with label `entity:False` for any query
    containing the token "false" — a SCORING change a normalizer has no business making."""
    assert corpus.entity_list(False) == []
    assert corpus.entity_list([False]) == []


def test_entity_list_of_a_falsy_but_real_int_scalar_is_kept():
    """`0` is an int, not a bool — `_acl_labels` keeps a falsy-but-real scalar, and so does this,
    for the same reason: only YAML's TRUTH values are rejected, not every falsy Python value."""
    assert corpus.entity_list(0) == ["0"]


def test_entity_list_drops_a_nested_list_element_rather_than_stringifying_its_repr():
    assert corpus.entity_list(["initech", ["nested"]]) == ["initech"]
    assert corpus.entity_list(["initech", {"a": 1}]) == ["initech"]


def test_loads_exactly_the_three_included_zones():
    rows = corpus.load_pages(FIXTURE)
    assert len(rows) == 11
    assert {r.zone for r in rows} == {"wiki", "sources", "views"}
    # the excluded-zone markers must never surface (ops/, meta/, datasets/)
    assert not [r for r in rows if "excluded" in r.path]


def test_zone_counts_match_the_fixture():
    rows = corpus.load_pages(FIXTURE)
    zones = {}
    for r in rows:
        zones[r.zone] = zones.get(r.zone, 0) + 1
    # the 4th `wiki` page (`globex-initech-partnership.md`) is anchored to TWO entities — the
    # fixture's only multi-element `entity:` page, and so the structural witness the plural
    # `entity:` contract needs.
    assert zones == {"wiki": 4, "sources": 6, "views": 1}


def test_page_id_prefers_frontmatter_id_and_falls_back_to_stem():
    rows = _by_path()
    assert rows["sources/entities/globex/quarterly-report-q1-2026-draft-aaaaaa.md"].page_id == "drive:G1"
    assert rows["views/globex.md"].page_id == "globex"           # no id -> file stem
    assert rows["wiki/decisions/refund-policy.md"].page_id == "refund-policy"


def test_contract_columns_are_parsed():
    rows = _by_path()
    draft = rows["sources/entities/globex/quarterly-report-q1-2026-draft-aaaaaa.md"]
    assert draft.superseded_by == "drive:G2"
    # `entity` normalizes to a LIST — the fixture's bare-string dialect (`entity: globex`)
    # reads as a one-element list.
    assert draft.entity == ["globex"] and draft.as_of == "2026-Q1" and draft.tier == 1
    final = rows["sources/entities/globex/quarterly-report-q1-2026-final-bbbbbb.md"]
    assert final.supersedes == "drive:G1" and final.superseded_by == ""
    policy = rows["wiki/decisions/refund-policy.md"]
    assert policy.status == "canonical" and policy.owner == "steward"
    assert policy.updated == "2026-07-01"
    # ADR 026 D2: `verification` is not a contract column — not parsed, not stored, not filtered
    # on, not ranked on. Asserted as a NEGATIVE rather than merely dropped: deleting the two
    # assertions below leaves this file silent about the field, and a restored
    # `PageRow.verification` would pass it green. The fixture page still CARRIES
    # `verification: failed` in its frontmatter, deliberately — the property worth pinning is
    # that the reader IGNORES it, which a corpus with no such page cannot show.
    scan = rows["sources/general/market-scan-failed-cccccc.md"]
    assert not hasattr(scan, "verification")
    review = rows["sources/general/scanned-contract-review-dddddd.md"]
    # `extraction_quality` and `period` are not contract columns either — nothing produces
    # either one, and `period` duplicated `as_of` exactly (every page carrying one carried both,
    # same value).
    assert not hasattr(review, "extraction_quality")


def test_machine_pages_take_updated_from_extracted_at():
    rows = _by_path()
    assert rows["sources/entities/globex/quarterly-report-q1-2026-final-bbbbbb.md"].updated == "2026-04-15"


def test_acl_distinguishes_absent_from_present():
    rows = _by_path()
    assert rows["views/globex.md"].acl == ["sales"]
    assert rows["wiki/decisions/refund-policy.md"].acl is None   # no acl = open


def test_acl_empty_list_is_nobody_not_open():
    fm, _ = corpus.split_frontmatter("---\nacl: []\n---\nbody")
    assert corpus._acl_labels(fm) == []          # [] = restricted to nobody, NOT None/open
    assert corpus._acl_labels({}) is None


def test_acl_scalar_is_read_as_a_one_label_list():
    """`acl: sales` (author forgot the brackets) asked for restriction — honor it, never
    fail open."""
    fm, _ = corpus.split_frontmatter("---\nacl: sales\n---\nbody")
    assert corpus._acl_labels(fm) == ["sales"]


def test_acl_malformed_shapes_fail_closed():
    """A page that carries an acl key in an unrecognized shape must index as visible to
    NOBODY (a loud retrieval gap), never as open (a silent leak once the server enforces the
    labels)."""
    assert corpus._acl_labels({"acl": {"team": "sales"}}) == []      # mapping: unrecognized
    assert corpus._acl_labels({"acl": True}) == []                   # boolean: unrecognized
    assert corpus._acl_labels({"acl": "   "}) == []                  # blank scalar
    assert corpus._acl_labels({"acl": None}) is None                 # explicit null: no request
    assert corpus._acl_labels({"acl": ["sales", "", "  "]}) == ["sales"]   # empties dropped


def test_inlinks_count_distinct_linking_pages():
    rows = _by_path()
    # refund-policy is linked from support-playbook and git-canonical-store
    assert rows["wiki/decisions/refund-policy.md"].inlinks == 2
    # support-playbook only from refund-policy (related + body count once)
    assert rows["wiki/playbooks/support-playbook.md"].inlinks == 1
    assert rows["views/globex.md"].inlinks == 0


def test_wikilinks_inside_code_are_not_links():
    assert corpus.link_targets("see [[real-page]] and `[[not-a-link]]`\n```\n[[fenced]]\n```") == \
        ["real-page"]


# ── outbound `links`, resolved repo-relative paths (never stems) ────────────────────────────────
def test_load_pages_resolves_outbound_links_to_paths():
    rows = _by_path()
    # refund-policy <-> support-playbook link each other; git-canonical-store links refund-policy
    # only (one direction) — mirrors the inlinks pairing `test_inlinks_count_distinct_linking_
    # pages` already pins, now checked from the OUTBOUND side.
    assert rows["wiki/decisions/refund-policy.md"].links == \
        ["wiki/playbooks/support-playbook.md"]
    assert rows["wiki/playbooks/support-playbook.md"].links == \
        ["wiki/decisions/refund-policy.md"]
    assert rows["wiki/concepts/git-canonical-store.md"].links == \
        ["wiki/decisions/refund-policy.md"]
    assert rows["views/globex.md"].links == []          # no outbound wikilinks at all


def test_by_stem_index_groups_by_lowercased_stem():
    assert corpus.by_stem_index(["a/B.md", "c/b.md", "d/Other.md"]) == \
        {"b": ["a/B.md", "c/b.md"], "other": ["d/Other.md"]}


def test_by_stem_index_of_no_paths_is_empty():
    assert corpus.by_stem_index([]) == {}


def test_by_stem_index_excludes_views_as_link_targets():
    """A view's filename is the entity ID, which for a single-word entity equals the entity
    page's stem lowercased (views/vantage.md vs wiki/entities/Vantage.md — the first real
    regeneration collided). views/ is outside the wikilink namespace, so `[[Entity]]` resolves
    to exactly the entity page and never the derived rollup."""
    index = corpus.by_stem_index(["wiki/entities/Vantage.md", "views/vantage.md"])
    assert index == {"vantage": ["wiki/entities/Vantage.md"]}


def test_resolve_links_ambiguous_stem_stores_every_match():
    """A stem resolving to multiple pages stores all matches — the same semantics `inlinks`
    already counts."""
    by_stem = {"x": ["a/x.md", "b/x.md"]}
    assert corpus.resolve_links("c/source.md", ["x"], by_stem) == ["a/x.md", "b/x.md"]


def test_resolve_links_unmatched_stem_stores_nothing():
    """A dead link is the linter's finding, not the index's."""
    assert corpus.resolve_links("c/source.md", ["ghost"], {}) == []


def test_resolve_links_excludes_self():
    """A page is never its own outbound neighbour, mirroring the inbound side's pre-existing
    self-exclusion."""
    by_stem = {"me": ["a/me.md"]}
    assert corpus.resolve_links("a/me.md", ["me"], by_stem) == []


def test_resolve_links_dedupes_repeated_stems_and_sorts_paths():
    by_stem = {"x": ["b/x.md"], "y": ["a/y.md"]}
    assert corpus.resolve_links("z.md", ["x", "x", "y"], by_stem) == ["a/y.md", "b/x.md"]


# ── superseded_by propagates onto split-chain siblings ──────────────────────────────────────────
def test_load_pages_propagates_superseded_by_onto_split_continuation_parts(tmp_path):
    """Only the primary page's frontmatter carries `superseded_by` — a continuation part's own
    frontmatter leaves it empty. `load_pages` propagates the primary's value onto every `#p<n>`
    sibling sharing the same `rank.chain_base`, so the field is TRUE on every row that reaches
    storage rather than needing reconstruction at rank time."""
    kdir = tmp_path / "wiki"
    kdir.mkdir()
    (kdir / "primary.md").write_text(
        "---\nid: drive:G1\nsuperseded_by: drive:G2\ntitle: Primary\n---\npart one body")
    (kdir / "part2.md").write_text(
        "---\nid: drive:G1#p2\ntitle: Part Two\n---\npart two continued")
    (kdir / "part3.md").write_text(
        "---\nid: drive:G1#p3\ntitle: Part Three\n---\npart three continued")
    (kdir / "current.md").write_text(
        "---\nid: drive:G2\ntitle: Current\n---\ncorrected current body")
    rows = {r.page_id: r for r in corpus.load_pages(str(tmp_path))}
    assert rows["drive:G1"].superseded_by == "drive:G2"          # unchanged: the primary's own
    assert rows["drive:G1#p2"].superseded_by == "drive:G2"       # propagated
    assert rows["drive:G1#p3"].superseded_by == "drive:G2"       # propagated
    assert rows["drive:G2"].superseded_by == ""                  # the successor is not superseded


def test_load_pages_does_not_propagate_across_different_chains(tmp_path):
    """The grouping is BY `chain_base` — an unrelated document's parts must not pick up a
    completely different document's supersession."""
    kdir = tmp_path / "wiki"
    kdir.mkdir()
    (kdir / "a.md").write_text(
        "---\nid: drive:A\nsuperseded_by: drive:A2\ntitle: A\n---\nbody a")
    (kdir / "b.md").write_text("---\nid: drive:B#p2\ntitle: B part 2\n---\nbody b")
    rows = {r.page_id: r for r in corpus.load_pages(str(tmp_path))}
    assert rows["drive:A"].superseded_by == "drive:A2"
    assert rows["drive:B#p2"].superseded_by == ""


def test_load_pages_unsuperseded_chain_stays_empty(tmp_path):
    kdir = tmp_path / "wiki"
    kdir.mkdir()
    (kdir / "doc.md").write_text("---\nid: drive:D\ntitle: Doc\n---\nbody")
    (kdir / "doc-p2.md").write_text("---\nid: drive:D#p2\ntitle: Doc part 2\n---\nbody")
    rows = {r.page_id: r for r in corpus.load_pages(str(tmp_path))}
    assert rows["drive:D"].superseded_by == ""
    assert rows["drive:D#p2"].superseded_by == ""


# ── propagation is marker-gated, not "same page_id" ─────────────────────────────────────────────
def test_load_pages_does_not_cross_stamp_unrelated_pages_sharing_a_stem(tmp_path):
    """Two ID-LESS pages that happen to share a file STEM in different directories (`page_id`
    falls back to the stem, `page_row`'s own docstring) are NOT chain siblings — `chain_base`
    only strips a trailing `#p<n>` marker, so grouping by `chain_base(page_id)` alone put both
    under the SAME bucket (both reduce to `"acme"`) and the old "first non-empty in the group"
    rule copied whichever `superseded_by` it found first onto BOTH, even though neither carries a
    real continuation marker (`^{base}#p\\d+$`). Only a genuine `#p<n>` id may RECEIVE; only the
    row whose `page_id` equals the base EXACTLY may DONATE."""
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "views").mkdir()
    (tmp_path / "wiki" / "entities" / "acme.md").write_text(
        "---\ntitle: Acme Entity\nsuperseded_by: some-new-acme-page\n---\nentity page body")
    (tmp_path / "views" / "acme.md").write_text(
        "---\ntitle: Acme View\n---\nview body, unrelated to the entity page's supersession")
    rows = {r.path: r for r in corpus.load_pages(str(tmp_path))}
    assert rows["wiki/entities/acme.md"].superseded_by == "some-new-acme-page"
    assert rows["views/acme.md"].superseded_by == ""          # must stay untouched


def test_load_pages_a_real_part_still_receives_even_with_an_unrelated_same_stem_page_present(
        tmp_path):
    """The marker gate does not throw out the legitimate case alongside the bogus one: a REAL
    `#p2` continuation part still receives its primary's `superseded_by`, even in a corpus that
    also contains an unrelated id-less page whose OWN page_id happens to equal that same base
    (the donor is picked by exact `page_id == base`, never merely "first non-empty in the
    bucket")."""
    kdir = tmp_path / "wiki"
    kdir.mkdir()
    (kdir / "primary.md").write_text(
        "---\nid: drive:G1\nsuperseded_by: drive:G2\ntitle: Primary\n---\nprimary body")
    (kdir / "part2.md").write_text(
        "---\nid: drive:G1#p2\ntitle: Part Two\n---\npart two continued")
    rows = {r.page_id: r for r in corpus.load_pages(str(tmp_path))}
    assert rows["drive:G1"].superseded_by == "drive:G2"
    assert rows["drive:G1#p2"].superseded_by == "drive:G2"


def test_load_pages_conflicting_donor_values_within_one_base_logs_a_warning(tmp_path, caplog):
    """Two rows that both legitimately carry `page_id == base` (a real id COLLISION — not the
    stem-fallback case above) with DIFFERING non-empty `superseded_by` values is a data problem,
    not a silent pick: the donor is whichever sorts first (deterministic, by path), and the
    conflict is logged so it doesn't ride through unnoticed."""
    import logging

    kdir = tmp_path / "wiki"
    kdir.mkdir()
    (kdir / "a-conflict.md").write_text(
        "---\nid: drive:X\nsuperseded_by: drive:X2\ntitle: A\n---\nbody a")
    (kdir / "b-conflict.md").write_text(
        "---\nid: drive:X\nsuperseded_by: drive:X3\ntitle: B\n---\nbody b")
    with caplog.at_level(logging.WARNING):
        rows = corpus.load_pages(str(tmp_path))
    assert any("conflict" in r.message.lower() for r in caplog.records)
    by_path = {r.path: r for r in rows}
    # deterministic: the donor is the path-sorted first ("a-conflict.md" < "b-conflict.md"); both
    # rows in this fixture have page_id == base (neither carries a `#p<n>` marker), so neither
    # RECEIVES anything — each keeps its own frontmatter value untouched.
    assert by_path["wiki/a-conflict.md"].superseded_by == "drive:X2"
    assert by_path["wiki/b-conflict.md"].superseded_by == "drive:X3"


def test_content_hash_is_stable_and_content_sensitive():
    h1 = corpus.content_hash("Title\nbody")
    assert h1 == corpus.content_hash("Title\nbody")
    assert h1 != corpus.content_hash("Title\nbody changed")
    assert h1.startswith("sha256:")


def test_unparseable_frontmatter_still_yields_a_body_only_page():
    """A broken page must remain findable."""
    fm, body = corpus.split_frontmatter("---\ntitle: x: [unclosed\n---\nfindable needle body")
    assert fm == {}
    assert "findable needle" in body


def test_rows_are_sorted_by_path_for_determinism():
    rows = corpus.load_pages(FIXTURE)
    assert [r.path for r in rows] == sorted(r.path for r in rows)


# ── one parser, provably, for both the full walk and the webhook ────────────────────────────────
def test_page_row_is_the_public_seam_load_pages_itself_calls():
    """`page_row` is what the incremental webhook parses one changed file with — the SAME
    function `load_pages` calls per file, not a private name reached into from outside, which is
    what keeps incremental and full rebuild from disagreeing. Proven by construction: parsing the
    same file's text through `page_row` directly must match what `load_pages`'s own walk produced
    for that path, field for field except `inlinks` and `links` — both are whole-corpus facts
    `page_row` alone cannot compute (see its own docstring, and `resolve_links`'s)."""
    from pathlib import Path as _Path
    rel = "wiki/decisions/refund-policy.md"
    text = (_Path(FIXTURE) / rel).read_text(encoding="utf-8")

    direct = corpus.page_row(rel, "wiki", text)
    from_walk = _by_path()[rel]

    for field in ("path", "zone", "page_id", "title", "body", "type", "status", "entity",
                 "owner", "tier", "as_of", "updated",
                 "superseded_by", "supersedes", "acl", "tags", "mentions",
                 "content_hash"):
        assert getattr(direct, field) == getattr(from_walk, field), field
    # inlinks is the one field `page_row` alone cannot compute (needs the whole corpus's graph)
    assert direct.inlinks == 0
    # `links` is a SECOND such field — `page_row` alone can only produce the raw
    # STEMS `link_targets` found in the text (undeduped: `refund-policy.md` names
    # `[[support-playbook]]` once in frontmatter `related:` and once in the body); `load_pages`'s
    # whole-corpus walk resolves and dedupes them into paths (`resolve_links`).
    assert direct.links == ["support-playbook", "support-playbook"]
    assert from_walk.links == ["wiki/playbooks/support-playbook.md"]


# ── build-time propagation: the live stem convention, and the directory gate ────────────────────
def test_propagation_reaches_live_stem_convention_parts(tmp_path):
    kdir = tmp_path / "sources" / "meetings"
    kdir.mkdir(parents=True)
    (kdir / "x-transcript.md").write_text(
        "---\nsuperseded_by: x-transcript-v2\ntitle: Primary\n---\npart one")
    (kdir / "x-transcript-p2.md").write_text("---\ntitle: Part Two\n---\npart two")
    rows = {r.page_id: r for r in corpus.load_pages(str(tmp_path))}
    assert rows["x-transcript-p2"].superseded_by == "x-transcript-v2"   # propagated


def test_propagation_never_crosses_directories(tmp_path):
    """The cross-stamping twin class under the live convention: `report-p2.md` in another folder
    reduces to the same stem-derived base — the directory key keeps its `superseded_by` its
    own."""
    a = tmp_path / "wiki" / "a"
    b = tmp_path / "wiki" / "b"
    a.mkdir(parents=True), b.mkdir(parents=True)
    (a / "report.md").write_text("---\nsuperseded_by: report-v2\ntitle: R\n---\nbody")
    (b / "report-p2.md").write_text("---\ntitle: Unrelated\n---\nbody")
    rows = {r.path: r for r in corpus.load_pages(str(tmp_path))}
    assert rows["wiki/b/report-p2.md"].superseded_by == ""
