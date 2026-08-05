"""The filing-window checks: anchor concentration, company-wide fraction, and a company-scoped
page naming a registered entity. The first two share `_recent_filed_pages`'s "last N filings"
population (real `capture_queue` timestamps); all three exclude PROVENANCE-type resolved pages
from counting as "anchored"/"declared company-wide".
"""
import os

from stigmergy.gardener import checks, sweep
from stigmergy.kernel.registry import load_registry
from tests.gardener import support

REGISTRY = {
    "acme-corp": {"name": "Acme Corp", "type": "organization", "aliases": ["Acme"]},
    # an alias ending in a non-word character, disjoint from both the entity's own `name`
    # ("Beta Robotics") and `id` ("beta-robotics") — deliberately, so the fixture body
    # below cannot ALSO match the plain name as a coincidental prefix (which would match first,
    # `_entity_spellings`' own name-then-id-then-aliases order, and prove nothing about the
    # trailing-punctuation case this alias exists to test). Mirrors an initialism a real filing
    # might use ("BR, Inc.").
    "beta-robotics": {"name": "Beta Robotics", "type": "organization", "aliases": ["BR, Inc."]},
}


def _registry(repo: str):
    support.write_registry(repo, REGISTRY)
    return load_registry(os.path.join(repo, "ops", "entity-registry.json"))


def _file_page(conn, repo, relpath: str, *, entity: list, page_type: str = "note",
              body: str = "") -> str:
    path = support.write_page(repo, "wiki", relpath,
                              frontmatter={"type": page_type, "title": relpath, "entity": entity,
                                          "status": "developing", "updated": "2026-07-01"},
                              body=body)
    return path


# ── anchor concentration ────────────────────────────────────────────────────────────────────────
def test_anchor_concentration_fires_above_the_share_threshold(conn, repo):
    registry = _registry(repo)
    for i in range(4):
        p = _file_page(conn, repo, f"notes/acme-{i}.md", entity=["acme-corp"])
        support.rebuild_index(conn, repo)
        support.seed_filed_capture(conn, result_ref=f"{p}@sha{i}")
    p = _file_page(conn, repo, "notes/beta-0.md", entity=["beta-robotics"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha4")

    findings = checks.check_anchor_concentration(conn, registry, window=5, share_threshold=0.6)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_ANCHOR_CONCENTRATION
    assert f["severity"] == "warn"
    assert f["subject"] == "acme-corp"
    assert "4 of the last 5 filings (80%) anchored here, above the 60% threshold" in f["detail"]
    assert "Acme Corp" in f["suggested_action"]


def test_anchor_concentration_the_benign_twin_a_spread_window_fires_nothing(conn, repo):
    registry = _registry(repo)
    for i, entity in enumerate(["acme-corp", "beta-robotics", "acme-corp", "beta-robotics"]):
        p = _file_page(conn, repo, f"notes/spread-{i}.md", entity=[entity])
        support.rebuild_index(conn, repo)
        support.seed_filed_capture(conn, result_ref=f"{p}@sha{i}")

    # 2 of 4 (50%) — an even split, not above the default 60% threshold.
    assert checks.check_anchor_concentration(conn, registry, window=4, share_threshold=0.6) == []


def test_anchor_concentration_threshold_override_changes_the_outcome(conn, repo):
    """A non-default threshold, exercised: the SAME window that is below the default share
    crosses a strict enough one."""
    registry = _registry(repo)
    for i, entity in enumerate(["acme-corp", "beta-robotics", "acme-corp", "beta-robotics"]):
        p = _file_page(conn, repo, f"notes/thr-{i}.md", entity=[entity])
        support.rebuild_index(conn, repo)
        support.seed_filed_capture(conn, result_ref=f"{p}@sha{i}")

    assert checks.check_anchor_concentration(conn, registry, window=4, share_threshold=0.6) == []
    tightened = checks.check_anchor_concentration(conn, registry, window=4, share_threshold=0.4)
    assert len(tightened) == 1
    assert tightened[0]["subject"] == "acme-corp"


def test_anchor_concentration_excludes_provenance_pages_from_the_population(conn, repo):
    """The load-bearing exclusion: 3 acme notes + 2 meeting pages in a window of 5 — WITHOUT
    excluding the meetings the share would sit exactly at 60% (not above it, no finding); WITH the
    exclusion the denominator drops to 3 and the same numerator reads 100%."""
    registry = _registry(repo)
    for i in range(3):
        p = _file_page(conn, repo, f"notes/acme-note-{i}.md", entity=["acme-corp"])
        support.rebuild_index(conn, repo)
        support.seed_filed_capture(conn, result_ref=f"{p}@sha{i}")
    for i in range(2):
        p = _file_page(conn, repo, f"meetings/standup-{i}.md", entity=["beta-robotics"],
                       page_type="meeting")
        support.rebuild_index(conn, repo)
        support.seed_filed_capture(conn, result_ref=f"{p}@shameeting{i}")

    findings = checks.check_anchor_concentration(conn, registry, window=5, share_threshold=0.6)

    assert len(findings) == 1
    assert findings[0]["subject"] == "acme-corp"
    assert "3 of the last 3 filings (100%)" in findings[0]["detail"]


def test_anchor_concentration_an_empty_window_fires_nothing(conn, repo):
    registry = _registry(repo)
    assert checks.check_anchor_concentration(conn, registry, window=30, share_threshold=0.6) == []


# ── the population_stats sink: the exclusion counters, surfaced rather than merely computed ─────
def test_anchor_concentration_surfaces_its_population_exclusions_when_given_a_sink(conn, repo):
    registry = _registry(repo)
    p = _file_page(conn, repo, "notes/acme-0.md", entity=["acme-corp"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")
    support.seed_filed_capture(conn, result_ref="not-a-parseable-ref")
    support.seed_filed_capture(conn, result_ref="wiki/notes/never-indexed.md@shaX")

    sink: dict = {}
    checks.check_anchor_concentration(conn, registry, window=10, share_threshold=0.6,
                                      population_stats=sink)

    assert sink["anchor_concentration"] == {
        "unparsed_result_ref": 1, "page_not_indexed": 1, "provenance_excluded": 0}


def test_anchor_concentration_never_touches_the_sink_when_none_is_given(conn, repo):
    """The parameter is optional and additive — a call site that omits it must keep working
    exactly as it did before the sink existed."""
    registry = _registry(repo)
    assert checks.check_anchor_concentration(conn, registry, window=30, share_threshold=0.6) == []


def test_company_wide_fraction_surfaces_its_population_exclusions_when_given_a_sink(conn, repo):
    p = _file_page(conn, repo, "notes/company-0.md", entity=[])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")
    support.seed_filed_capture(conn, result_ref="garbage")

    sink: dict = {}
    checks.check_company_wide_fraction(conn, window=10, share_threshold=0.3, population_stats=sink)

    assert sink["company_wide_fraction"] == {
        "unparsed_result_ref": 1, "page_not_indexed": 0, "provenance_excluded": 0}


def test_both_checks_share_one_sink_dict_without_clobbering_each_other(conn, repo):
    p = _file_page(conn, repo, "notes/shared.md", entity=["acme-corp"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha0")
    registry = _registry(repo)

    sink: dict = {}
    checks.check_anchor_concentration(conn, registry, window=5, share_threshold=0.6,
                                      population_stats=sink)
    checks.check_company_wide_fraction(conn, window=5, share_threshold=0.3, population_stats=sink)

    assert set(sink) == {"anchor_concentration", "company_wide_fraction"}


# ── company-wide fraction ───────────────────────────────────────────────────────────────────────
def test_company_wide_fraction_fires_above_the_share_threshold(conn, repo):
    for i in range(3):
        p = _file_page(conn, repo, f"notes/company-{i}.md", entity=[])
        support.rebuild_index(conn, repo)
        support.seed_filed_capture(conn, result_ref=f"{p}@sha{i}")
    p = _file_page(conn, repo, "notes/anchored.md", entity=["acme-corp"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p}@sha3")

    findings = checks.check_company_wide_fraction(conn, window=4, share_threshold=0.3)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_COMPANY_WIDE_FRACTION
    assert f["severity"] == "warn"
    assert f["subject"] == ""
    assert "75% of the last 4 filings declared company-wide, above the 30% threshold" in f["detail"]


def test_company_wide_fraction_the_benign_twin_a_mostly_anchored_window_fires_nothing(conn, repo):
    p1 = _file_page(conn, repo, "notes/a.md", entity=["acme-corp"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p1}@sha0")
    p2 = _file_page(conn, repo, "notes/b.md", entity=["beta-robotics"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p2}@sha1")

    assert checks.check_company_wide_fraction(conn, window=2, share_threshold=0.3) == []


def test_company_wide_fraction_excludes_provenance_pages(conn, repo):
    """A provenance page's `entity: []` means "no evidence found", never a checked company-wide
    declaration — it must not inflate this fraction."""
    p1 = _file_page(conn, repo, "notes/anchored.md", entity=["acme-corp"])
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p1}@sha0")
    p2 = _file_page(conn, repo, "meetings/standup.md", entity=[], page_type="meeting")
    support.rebuild_index(conn, repo)
    support.seed_filed_capture(conn, result_ref=f"{p2}@sha1")

    # Without the exclusion this window would read 1 anchored + 1 "company-wide" = 50%, above a
    # 30% threshold. With it, the meeting page never enters the population at all: 0/1 = 0%.
    assert checks.check_company_wide_fraction(conn, window=2, share_threshold=0.3) == []


# ── a company-scoped page naming a registry entity ──────────────────────────────────────────────
def test_company_page_names_entity_fires_when_the_body_names_a_registered_alias_verbatim(conn, repo):
    registry = _registry(repo)
    _file_page(conn, repo, "product/renewal-terms.md", entity=[], page_type="note",
              body="This applies to Acme Corp specifically.")
    support.rebuild_index(conn, repo)

    findings = checks.check_company_page_names_entity(conn, registry)

    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == checks.CHECK_COMPANY_PAGE_NAMES_ENTITY
    assert f["severity"] == "warn"
    assert f["subject"] == "wiki/product/renewal-terms.md"
    assert 'names "Acme Corp" (`acme-corp`) verbatim' in f["detail"]


def test_company_page_names_entity_the_benign_twin_naming_nothing_fires_nothing(conn, repo):
    registry = _registry(repo)
    _file_page(conn, repo, "product/general-policy.md", entity=[], page_type="note",
              body="This is a general statement about how we operate as a company.")
    support.rebuild_index(conn, repo)

    assert checks.check_company_page_names_entity(conn, registry) == []


def test_company_page_names_entity_ignores_anchored_pages(conn, repo):
    registry = _registry(repo)
    _file_page(conn, repo, "product/anchored.md", entity=["acme-corp"], page_type="note",
              body="Acme Corp specific terms.")
    support.rebuild_index(conn, repo)

    assert checks.check_company_page_names_entity(conn, registry) == []


def test_company_page_names_entity_excludes_provenance_pages(conn, repo):
    """A `meeting` page's `entity: []` never means "declared company-wide" — it must not be
    checked for a verbatim entity mention either."""
    registry = _registry(repo)
    _file_page(conn, repo, "meetings/standup.md", entity=[], page_type="meeting",
              body="Discussed Acme Corp renewal today.")
    support.rebuild_index(conn, repo)

    assert checks.check_company_page_names_entity(conn, registry) == []


def test_company_page_names_entity_does_not_match_a_substring_of_an_unrelated_word(conn, repo):
    """Word-bounded matching (`_first_verbatim_match`): neither the id nor the short alias must
    fire just because a longer, unrelated word happens to contain it as a substring (verified
    directly against `re`: a HYPHEN is not a word character, so "acme-corporate..." would give the
    short alias "Acme" a real boundary and is not a safe fixture for this case — this uses an
    unbroken compound instead, where no registered spelling has a boundary at all)."""
    registry = _registry(repo)
    _file_page(conn, repo, "product/unrelated.md", entity=[], page_type="note",
              body="See acmecorporation's unrelated announcement for the schedule.")
    support.rebuild_index(conn, repo)

    assert checks.check_company_page_names_entity(conn, registry) == []


# ── an alias ending in a non-word character used to be unmatchable — `\b` requires a WORD
# character immediately after the alias too, and in ordinary prose the position right after
# "Beta Robotics, Inc." is whitespace or more punctuation, never a word character, so the trailing
# `\b` could never fire. Fixed via lookarounds (`(?<!\w)`/`(?!\w)`, checks.py) ──────────────────
def test_company_page_names_entity_fires_for_an_alias_ending_in_punctuation(conn, repo):
    registry = _registry(repo)
    _file_page(conn, repo, "product/vendor-note.md", entity=[], page_type="note",
              body="We now work with BR, Inc. on this.")
    support.rebuild_index(conn, repo)

    findings = checks.check_company_page_names_entity(conn, registry)

    assert len(findings) == 1
    assert 'names "BR, Inc." (`beta-robotics`) verbatim' in findings[0]["detail"]


def test_company_page_names_entity_the_punctuation_alias_benign_twin_fires_nothing_when_absent(
        conn, repo):
    """The specificity half of the fix above — a test that only proves a gate fires measures its
    sensitivity and never its specificity: the SAME punctuation-suffixed alias must not fire on a
    page that never mentions it."""
    registry = _registry(repo)
    _file_page(conn, repo, "product/unrelated-vendor-note.md", entity=[], page_type="note",
              body="We now work with a different supplier on this.")
    support.rebuild_index(conn, repo)

    assert checks.check_company_page_names_entity(conn, registry) == []


# ── suggested_action: a re-anchor is not something a capture/gesture can file (issue #37) ───────
# `src/stigmergy/librarian/gates.py::gate_body_rewrite` allows an existing page's frontmatter to
# change in exactly ONE way — `related:` growth (rule 4) — and vetoes every other frontmatter
# change, `entity:` included, as `body-rewrite`, `repairable=False` (pinned directly against the
# gate in `tests/librarian/test_gates_unit.py::
# test_an_entity_only_change_to_an_existing_page_is_vetoed_as_body_rewrite_not_repairable`). The
# agent's own write path is allow-listed to NEW pages only (`GateContext.in_lane_new_pages` —
# status "A"), so neither the 🧠 gesture nor an MCP capture can ever touch an EXISTING page's
# `entity:` field. The old wording told the operator a re-anchor "is filed the same way as any
# other" correction — a promise the gates make impossible to keep.
def test_company_page_names_entity_suggested_action_drops_the_capture_can_reanchor_claim(
        conn, repo):
    registry = _registry(repo)
    _file_page(conn, repo, "product/renewal-terms.md", entity=[], page_type="note",
              body="This applies to Acme Corp specifically.")
    support.rebuild_index(conn, repo)

    action = checks.check_company_page_names_entity(conn, registry)[0]["suggested_action"]

    assert "filed the same way" not in action
    assert "MCP capture" not in action


def test_company_page_names_entity_suggested_action_names_the_real_routes(conn, repo):
    registry = _registry(repo)
    _file_page(conn, repo, "product/renewal-terms.md", entity=[], page_type="note",
              body="This applies to Acme Corp specifically.")
    support.rebuild_index(conn, repo)

    action = checks.check_company_page_names_entity(conn, registry)[0]["suggested_action"]
    lowered = action.lower()

    # (a) the working route: a hand edit of `entity:` in the knowledge repo, committed and pushed.
    assert "entity:" in action
    assert "edit" in lowered
    assert "commit" in lowered
    # (b) the alternative: filing a superseding page.
    assert "supersed" in lowered
    # (c) leaving a genuinely company-wide page alone is a legitimate outcome, not an oversight.
    assert "leav" in lowered or "legitimate" in lowered


def test_both_reanchor_suggested_actions_drop_the_capture_claim_and_name_the_real_routes(
        conn, repo):
    """Both call sites of the same broken promise (issue #37), walked TOGETHER, so a fix that
    corrects one and forgets its sibling fails loudly rather than passing on the half that got
    fixed: `checks.py`'s own emitted finding, AND `sweep.py`'s static `MODEL_SUGGESTED_ACTIONS`
    dict entry for `CHECK_MODEL_ANCHOR_FIT` (`sweep.py`'s own docstring: `suggested_action` for a
    model finding is NEVER model-generated — it is this fixed dict, looked up by slug alone)."""
    registry = _registry(repo)
    _file_page(conn, repo, "product/renewal-terms.md", entity=[], page_type="note",
              body="This applies to Acme Corp specifically.")
    support.rebuild_index(conn, repo)
    checks_action = checks.check_company_page_names_entity(conn, registry)[0]["suggested_action"]
    sweep_action = sweep.MODEL_SUGGESTED_ACTIONS[sweep.CHECK_MODEL_ANCHOR_FIT]

    sites = {"checks.check_company_page_names_entity": checks_action,
             "sweep.MODEL_SUGGESTED_ACTIONS[CHECK_MODEL_ANCHOR_FIT]": sweep_action}
    for label, action in sites.items():
        lowered = action.lower()
        assert "filed the same way" not in action, label
        assert "MCP capture" not in action, label
        assert "entity:" in action, label
        assert "edit" in lowered, label
        assert "commit" in lowered, label
        assert "supersed" in lowered, label
        assert "leav" in lowered or "legitimate" in lowered, label
