"""The deterministic checks — `ALL_CHECK_SLUGS` below is the list, and the COUNT belongs to it
rather than to this sentence, which is how the number drifted to "eight" and "nine" in six other
places while the tuple grew to ten. Each returns a list of finding dicts, never touches
`gardener_findings` (`store.py`'s job) and never prints (`report.py`'s).

Population invariants: the two windowed checks resolve "the last N filings" out of
`capture_queue` — never the `updated`/`as_of` TEXT columns, which describe a page's authored
date, not when it was FILED. Those two and `check_company_page_names_entity` exclude
provenance-type pages: a provenance page's `entity: []` means "the extractor found no evidence",
never a checked company-wide declaration. `check_aging_seeds` ages `updated` IN Postgres, so a
test backdates the fixture row and lets `current_date` do the comparison.
"""
import logging
import pathlib
import re

from stigmergy.gardener.schema import (
    MAX_DETAIL_CHARS,
    SEVERITY_INFO,
    SEVERITY_WARN,
    SOURCE_DETERMINISTIC,
)
from stigmergy.kernel.registry import Registry
from stigmergy.librarian import page as page_policy
from stigmergy.text import parse_result_ref
from stigmergy.views import staleness as view_staleness

log = logging.getLogger(__name__)

# ── check slugs ──────────────────────────────────────────────────────────────────────────────
CHECK_ORPHAN_PAGE = "orphan-page"
CHECK_AGING_SEED = "aging-seed"
CHECK_STALE_VIEW = "stale-view"
CHECK_ANCHOR_CONCENTRATION = "anchor-concentration"
CHECK_DEAD_VOCABULARY = "dead-vocabulary"
CHECK_COMPANY_WIDE_FRACTION = "company-wide-fraction"
CHECK_COMPANY_PAGE_NAMES_ENTITY = "company-page-names-entity"
# A style convention, not a safety property — a finding here, never a filing veto.
# `librarian.processing` names the same slug where it declines to veto it; one grep finds both.
CHECK_DATE_BEARING_BODY_LINK = "date-bearing-body-link"
# The one check with a repair kind of its own (`repair.schema.KIND_ENTITY_BODY`): the finding names
# an entity page, and the repair proposer drafts that page's body from the pages anchored to it.
CHECK_ENTITY_PLACEHOLDER_BODY = "entity-placeholder-body"

# The residual an applied `entity-alias` merge leaves behind and can never sweep up itself: the
# absorbed id stays REGISTERED (the page stays by governance, and the knowledge repo's contract
# linter refuses an alias naming an existing page), so a capture filed later spelling that name
# anchors its history to the retired identity — and the loop cannot re-propose the pair, because
# the decision's `content_key` is permanent. This count is where that accumulation is visible, and
# the measurable target the filing-time fix (issue #77's other half) converges to zero.
CHECK_ANCHORED_TO_SUPERSEDED_ENTITY = "anchored-to-superseded-entity"

ALL_CHECK_SLUGS = (
    CHECK_ORPHAN_PAGE, CHECK_AGING_SEED, CHECK_STALE_VIEW, CHECK_ANCHOR_CONCENTRATION,
    CHECK_DEAD_VOCABULARY, CHECK_COMPANY_WIDE_FRACTION, CHECK_COMPANY_PAGE_NAMES_ENTITY,
    CHECK_DATE_BEARING_BODY_LINK, CHECK_ENTITY_PLACEHOLDER_BODY,
    CHECK_ANCHORED_TO_SUPERSEDED_ENTITY,
)

# The tail of every "this page may be anchored wrong" action — `check_company_page_names_entity`
# here and `sweep.MODEL_SUGGESTED_ACTIONS[CHECK_MODEL_ANCHOR_FIT]` compose the SAME instruction,
# and an operator reading one after the other must not find two wordings of one procedure.
REANCHOR_BY_HAND = (
    "a re-anchor has to be done by hand — edit `entity:` on the page in the knowledge repo "
    "yourself, commit and push, since a hand edit in the wiki zone never passes through the "
    "filing gates at all (that zone is people's to edit, not a capture's). If the content itself "
    "needs restating, file a superseding page instead; and if the page really is company-wide, "
    "leaving it alone is a legitimate answer too")


def build_finding(*, check: str, severity: str, subject: str, detail: str,
            suggested_action: str, source: str = SOURCE_DETERMINISTIC,
            subjects: list[str] | None = None, **extra) -> dict:
    """The one place a finding dict is assembled — shared by every check here and by
    `gardener.sweep.to_finding` (`model_id` rides through `**extra`). `store.py` persists the
    seven named keys plus `model_id` and nothing else, so anything else `**extra` carries stays
    in memory for the run that put it there.

    `subject` is the DISPLAY string and `subjects` the same fact as data. Omitted, it derives from
    `subject` — one page, named once — and an EMPTY subject derives to `[]` rather than `[""]`:
    `check_company_wide_fraction` reports a corpus-wide fraction that names no page at all, and a
    consumer iterating `subjects` must not be handed an empty string as if it were a path. A check
    whose subject is not a page path (`check_stale_views` names an entity id) passes it through
    unchanged — this function does not classify, and every reader that acts on a path filters for
    one.
    """
    finding = {
        "check": check, "severity": severity, "source": source,
        "subject": subject, "detail": detail[:MAX_DETAIL_CHARS],
        "suggested_action": suggested_action,
        "subjects": list(subjects) if subjects is not None else ([subject] if subject else []),
    }
    finding.update(extra)
    return finding


def count_indexed_pages(conn) -> int:
    """`pages_index`'s total row count — the report header's "checked N pages"."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index")
        return cur.fetchone()[0]


# ── orphans ──────────────────────────────────────────────────────────────────────────────────
# Types a zero-inbound page is never flagged for, each with a stated reason — never by silence.
ORPHAN_EXEMPT_TYPES = {
    "entity": (
        "entity pages are addressed through `entity:` frontmatter anchoring, never through a "
        "wikilink — nothing in this corpus links `[[<entity-id>]]` by convention, so a "
        "zero-inbound entity page reflects the anchoring mechanism working as designed, not "
        "neglect (the hub shape: a page meant to be pointed AT by a different mechanism, not to "
        "accumulate inbound links of its own)"),
}

_ORPHANS_SQL = """
SELECT p.path, p.type
FROM pages_index p
WHERE p.zone = 'wiki'
  AND p.type <> ALL(%(exempt)s)
  AND NOT EXISTS (SELECT 1 FROM pages_index q WHERE q.links @> ARRAY[p.path])
ORDER BY p.path
"""


def check_orphans(conn) -> list[dict]:
    """Population: `zone = 'wiki'` pages of a non-exempt type with zero inbound wikilinks — a
    live `links @> ARRAY[path]` containment check, never the `inlinks` column, which is only as
    fresh as the last FULL index rebuild."""
    with conn.cursor() as cur:
        cur.execute(_ORPHANS_SQL, {"exempt": list(ORPHAN_EXEMPT_TYPES)})
        rows = cur.fetchall()
    return [
        build_finding(
            check=CHECK_ORPHAN_PAGE, severity=SEVERITY_INFO, subject=path,
            detail=f'type "{page_type}", nothing in the corpus links to it',
            suggested_action=(
                "no command — either link it from a page it belongs under, or, if it's meant to "
                "stand alone (a hub or an index), check whether its type belongs on the exemption "
                "list (that list is code, with stated reasons — a developer change, not an "
                "operator one)"),
        )
        for path, page_type in rows
    ]


# ── aging seeds ──────────────────────────────────────────────────────────────────────────────
# A malformed hand-edited `updated:` must not abort the whole run mid-query. A regex guard is not
# enough (it passes "2026-02-30", and Postgres may reorder ANDed quals, so "regex before cast" is
# never guaranteed): `pg_input_is_valid` (PG16+) tests the VALUE without raising, and `CASE` is
# the documented way to force it to run before the cast. Malformed rows are excluded and COUNTED
# (`_MALFORMED_UPDATED_SQL`). The SELECT's own `updated::date` is safe: the WHERE already proved
# validity for every row that reaches it.
_AGE_SQL = """
SELECT path, status, updated, (current_date - updated::date) AS age_days
FROM pages_index
WHERE status = ANY(%(statuses)s)
  AND updated <> ''
  AND CASE WHEN pg_input_is_valid(updated, 'date')
           THEN updated::date < (current_date - %(days)s::int)
           ELSE false END
ORDER BY path
"""
_MALFORMED_UPDATED_SQL = """
SELECT count(*) FROM pages_index
WHERE status = ANY(%(statuses)s) AND updated <> '' AND NOT pg_input_is_valid(updated, 'date')
"""


def _age_query(conn, *, statuses: list[str], days: int, population_stats: dict | None = None):
    """Rows in `statuses` older than `days`; a malformed `updated` is excluded and counted into
    `population_stats["malformed_updated"]` when given."""
    with conn.cursor() as cur:
        cur.execute(_AGE_SQL, {"statuses": statuses, "days": days})
        rows = cur.fetchall()
    if population_stats is not None:
        with conn.cursor() as cur:
            cur.execute(_MALFORMED_UPDATED_SQL, {"statuses": statuses})
            population_stats["malformed_updated"] = cur.fetchone()[0]
    return rows


def check_aging_seeds(conn, *, threshold_days: int, population_stats: dict | None = None
                      ) -> list[dict]:
    """Population: `status IN ('seed', 'developing')` pages whose `updated` is older than
    `threshold_days`. `population_stats`, when given, gets the malformed-`updated` count."""
    rows = _age_query(conn, statuses=["seed", "developing"], days=threshold_days,
                      population_stats=population_stats)
    return [
        build_finding(
            check=CHECK_AGING_SEED, severity=SEVERITY_WARN, subject=path,
            detail=f"{status}, updated {updated}, {age_days} days ago (threshold {threshold_days})",
            suggested_action=(
                "no command runs itself — read the page and decide whether it is ready to move "
                "on in `status`, needs more work first, or is fine to leave where it is a while "
                "longer"),
        )
        for path, status, updated, age_days in rows
    ]


# ── stale views — file-based, no DB ──────────────────────────────────────────────────────────
def check_stale_views(repo: str) -> list[dict]:
    """Population: every entity `views.staleness.list_stale_entities` names — reused verbatim,
    never re-derived. Staleness is a signal mismatch, not an age — the member hash OR the backlink
    hash, whichever moved — and reusing that function rather than re-deriving it is exactly why
    this check inherited the backlink half (#85) without a line changing here. Import
    `views.staleness`, never `views.regenerate`: the latter would load the git write stack into
    every gardener process.

    This finding HAS an actor, and it is not here: the librarian worker's periodic convergence
    sweep regenerates over a SUPERSET of this population (it also creates views that never existed
    and removes orphaned ones). So this stays detection — the gardener holds no git plumbing by
    construction — and the command below is what an operator runs to skip the wait."""
    return [
        build_finding(
            check=CHECK_STALE_VIEW, severity=SEVERITY_WARN, subject=entity_id,
            detail="the view no longer matches the corpus — its member set or the backlinks it "
                   "cites have changed since it was last generated",
            # No command: the worker's own sweep converges `views/` from state, and it runs on
            # every idle branch (ADR 044 D3). A finding that told an operator to run something
            # would be an executable promise nothing keeps — what this one says is that it will
            # take care of itself, and roughly when.
            suggested_action="no command — the librarian worker regenerates it on its next idle "
                             "pass; a view still listed here after several is worth checking the "
                             "worker's job runs for",
        )
        for entity_id in view_staleness.list_stale_entities(repo)
    ]


# ── shared population for the two windowed checks: "the last N filings" ──────────────────────
_RECENT_FILED_REFS_SQL = """
SELECT result_ref FROM capture_queue WHERE status = 'filed'
ORDER BY finished_at DESC LIMIT %(window)s
"""


def _recent_filed_pages(conn, *, window: int) -> tuple[list[dict], dict]:
    """The last `window` FILED `capture_queue` rows resolved to their page: `(pages, stats)`,
    `pages` = `[{"path", "type", "entity"}]` for indexed, non-provenance pages; `stats` counts
    every row that did not resolve — never silently guessed at."""
    with conn.cursor() as cur:
        cur.execute(_RECENT_FILED_REFS_SQL, {"window": window})
        result_refs = [row[0] for row in cur.fetchall()]

    stats = {"unparsed_result_ref": 0, "page_not_indexed": 0, "provenance_excluded": 0}
    paths: list[str] = []
    for ref in result_refs:
        parsed = parse_result_ref(ref)
        if parsed is None:
            stats["unparsed_result_ref"] += 1
            continue
        paths.append(parsed[0])

    by_path: dict[str, tuple[str, list[str]]] = {}
    if paths:
        with conn.cursor() as cur:
            cur.execute("SELECT path, type, entity FROM pages_index WHERE path = ANY(%s)",
                       (paths,))
            by_path = {row[0]: (row[1], list(row[2] or [])) for row in cur.fetchall()}

    pages: list[dict] = []
    for path in paths:
        found = by_path.get(path)
        if found is None:
            stats["page_not_indexed"] += 1
            continue
        page_type, entity = found
        if page_policy.is_provenance_type(page_type):
            stats["provenance_excluded"] += 1
            continue
        pages.append({"path": path, "type": page_type, "entity": entity})
    return pages, stats


# ── anchor concentration ──────────────────────────────────────────────────────────────────────
def check_anchor_concentration(conn, registry: Registry, *, window: int,
                               share_threshold: float, population_stats: dict | None = None
                               ) -> list[dict]:
    """Population: the last `window` filed pages, each contributing to every entity its `entity:`
    array names. Fires when the top entity's share exceeds `share_threshold`; an empty window
    fires nothing. `population_stats`, when given, gets this check's `_recent_filed_pages`
    exclusion counters under `"anchor_concentration"`."""
    pages, stats = _recent_filed_pages(conn, window=window)
    if population_stats is not None:
        population_stats["anchor_concentration"] = stats
    total = len(pages)
    if not total:
        return []
    counts: dict[str, int] = {}
    for page in pages:
        for entity_id in page["entity"]:
            counts[entity_id] = counts.get(entity_id, 0) + 1
    if not counts:
        return []
    # Ties broken alphabetically by entity id — deterministic across runs over the same window.
    top_count = max(counts.values())
    top_entity = min(eid for eid, c in counts.items() if c == top_count)
    share = top_count / total
    if share <= share_threshold:
        return []
    name = registry.title(top_entity) or top_entity
    return [build_finding(
        check=CHECK_ANCHOR_CONCENTRATION, severity=SEVERITY_WARN, subject=top_entity,
        detail=(f"{top_count} of the last {total} filings ({round(share * 100)}%) anchored here, "
               f"above the {round(share_threshold * 100)}% threshold"),
        suggested_action=(
            f"no command — read a few of the recent filings anchored to {name} and judge whether "
            "that's genuinely how lopsided the work has been, or whether unrelated material is "
            "defaulting here because picking the right anchor felt like more effort"),
    )]


# ── dead vocabulary — file-based, no DB ───────────────────────────────────────────────────────
def check_dead_vocabulary(repo: str, registry: Registry) -> list[dict]:
    """Population: every registered entity id NOT in
    `views.staleness.list_all_anchored_entities` (reused, never re-derived) — zero pages declare
    `entity: [<id>]`."""
    anchored = set(view_staleness.list_all_anchored_entities(repo))
    findings = []
    for entity_id in sorted(registry.entities):
        if entity_id in anchored:
            continue
        findings.append(build_finding(
            check=CHECK_DEAD_VOCABULARY, severity=SEVERITY_INFO, subject=entity_id,
            detail="registered in the entity registry, zero pages anchored to it",
            suggested_action=(
                "no command retires an entity — a birth has no inverse (no un-birth/"
                "retire/merge verb exists in `stigmergy.entities.cli`); decide by hand whether this "
                "was created too early or should be merged/retired by editing the registry "
                "directly"),
        ))
    return findings


# ── company-wide fraction ─────────────────────────────────────────────────────────────────────
def check_company_wide_fraction(conn, *, window: int, share_threshold: float,
                                population_stats: dict | None = None) -> list[dict]:
    """Population: the last `window` filed pages (shared `_recent_filed_pages`, provenance already
    excluded, so `entity: []` always means the checked declaration). Fires when the company-wide
    share exceeds `share_threshold`. No subject — a corpus-wide fraction. `population_stats` gets
    the exclusion counters under `"company_wide_fraction"`."""
    pages, stats = _recent_filed_pages(conn, window=window)
    if population_stats is not None:
        population_stats["company_wide_fraction"] = stats
    total = len(pages)
    if not total:
        return []
    company_wide = sum(1 for p in pages if not p["entity"])
    share = company_wide / total
    if share <= share_threshold:
        return []
    return [build_finding(
        check=CHECK_COMPANY_WIDE_FRACTION, severity=SEVERITY_WARN, subject="",
        detail=(f"{round(share * 100)}% of the last {total} filings declared company-wide, above "
               f"the {round(share_threshold * 100)}% threshold"),
        suggested_action=(
            "no command — review a few of the recent company-wide filings and judge whether "
            "they're genuinely about the whole company, or defaulting to company-wide because a "
            "specific anchor felt uncertain"),
    )]


# ── company-scoped page naming a registry entity ─────────────────────────────────────────────
def _entity_spellings(registry: Registry) -> list[tuple[str, list[str]]]:
    """`[(entity_id, [name, id, *aliases])]`, sorted by id — every registered spelling a body is
    tested against; an id or alias appearing verbatim is as strong a signal as the name."""
    out = []
    for entity_id in sorted(registry.entities):
        info = registry.entities[entity_id]
        spellings: list[str] = []
        name = info.get("name") or ""
        if name:
            spellings.append(name)
        spellings.append(entity_id)
        for alias in info.get("aliases") or []:
            if alias and alias not in spellings:
                spellings.append(alias)
        out.append((entity_id, spellings))
    return out


def _first_verbatim_match(body: str, spellings: list[str]) -> str | None:
    """The first of `spellings` found as exact, word-bounded text in `body` (case-insensitive).
    Lookarounds, never `\\b`: a trailing `\\b` after a spelling ending in punctuation
    ("Beta Robotics, Inc.") requires the NEXT body character to be a word character, so it
    silently never matches in prose; `(?!\\w)`/`(?<!\\w)` assert exactly "not inside a longer
    word"."""
    for spelling in spellings:
        pattern = r"(?<!\w)" + re.escape(spelling) + r"(?!\w)"
        if re.search(pattern, body or "", re.IGNORECASE):
            return spelling
    return None


_COMPANY_WIDE_PAGES_SQL = """
SELECT path, type, body FROM pages_index
WHERE zone = 'wiki' AND entity = '{}'
ORDER BY path
"""


def check_company_page_names_entity(conn, registry: Registry) -> list[dict]:
    """Population: every company-wide (`entity: []`), non-provenance `zone = 'wiki'` page — the
    whole corpus, no window. One finding per (page, entity) verbatim body match, naming the
    matched spelling."""
    with conn.cursor() as cur:
        cur.execute(_COMPANY_WIDE_PAGES_SQL)
        rows = cur.fetchall()
    spellings_by_entity = _entity_spellings(registry)
    findings = []
    for path, page_type, body in rows:
        if page_policy.is_provenance_type(page_type):
            continue
        for entity_id, spellings in spellings_by_entity:
            matched = _first_verbatim_match(body, spellings)
            if not matched:
                continue
            findings.append(build_finding(
                check=CHECK_COMPANY_PAGE_NAMES_ENTITY, severity=SEVERITY_WARN, subject=path,
                detail=f'company-wide, but its body names "{matched}" (`{entity_id}`) verbatim',
                suggested_action=(
                    "no command — read the page and judge whether it's really company-wide or "
                    f"should have been anchored to {registry.title(entity_id) or entity_id} "
                    f"instead; {REANCHOR_BY_HAND}"),
            ))
    return findings


# ── pages anchored to a retired identity ──────────────────────────────────────────────────────
# The population is the whole point, so it is spelled out: the KNOWLEDGE zone only, minus the
# entity zone itself. The absorbed page keeps its self-anchor forever BY DESIGN (its history is
# its own), and its `views/` rollup keeps declaring the id as a member set of one — a predicate
# alone would therefore report two permanent, unfixable findings per merge, forever: the exact
# disease the repair loop exists to end. With those out, the count is exactly zero the moment a
# merge lands, and every finding is a page somebody can actually re-anchor.
_ANCHORED_TO_SUPERSEDED_SQL = """
WITH retired AS (
  SELECT DISTINCT unnest(entity) AS id
  FROM pages_index
  WHERE path LIKE %(entity_zone)s AND superseded_by <> ''
)
SELECT p.path, array_agg(r.id ORDER BY r.id)
FROM pages_index p
JOIN retired r ON r.id = ANY(p.entity)
WHERE p.zone = 'wiki' AND p.path NOT LIKE %(entity_zone)s
GROUP BY p.path
ORDER BY p.path
"""


def check_anchored_to_superseded_entity(conn) -> list[dict]:
    """Knowledge pages whose `entity:` names an id whose OWN entity page declares
    `superseded_by:` — anchored to a retired identity. See `CHECK_ANCHORED_TO_SUPERSEDED_ENTITY`
    for why the loop cannot fix this itself and this count is the residual's one visible surface.
    True or false by inspection, so it is a deterministic check and never a model question."""
    with conn.cursor() as cur:
        cur.execute(_ANCHORED_TO_SUPERSEDED_SQL, {"entity_zone": "/".join(_ENTITY_ZONE) + "/%"})
        rows = cur.fetchall()
    findings = []
    for path, retired_ids in rows:
        listed = ", ".join(f"`{i}`" for i in retired_ids)
        findings.append(build_finding(
            check=CHECK_ANCHORED_TO_SUPERSEDED_ENTITY, severity=SEVERITY_INFO, subject=path,
            detail=(f"anchored to {listed}, a superseded identity — its page names the surviving "
                    f"one in `superseded_by:`, and this page's history sits on the retired side "
                    f"of that merge"),
            suggested_action=(
                "no command — move the page's anchor to the surviving identity (the retired "
                f"entity's own page names it in `superseded_by:`); {REANCHOR_BY_HAND}"),
        ))
    return findings


# ── date-bearing wikilinks in body prose ──────────────────────────────────────────────────────
# A `[[YYYY-MM-DD-…]]` target in BODY prose is a pointer that belongs in `sources:`/`related:`
# frontmatter — style, not safety, so a finding here rather than a filing-time veto.
_DATE_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")
# A wikilink target, alias/anchor stripped: `[[target|alias#anchor]]` resolves by `target` alone,
# and a leading `!` prefixes an embed.
_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")


def check_date_bearing_body_links(repo: str) -> list[dict]:
    """Population: every page in the three content zones, read from the repo checkout. One WARN
    finding per offending page, naming the first offending stem."""
    findings = []
    root = pathlib.Path(repo)
    for zone in ("wiki", "sources", "views"):
        zone_dir = root / zone
        if not zone_dir.is_dir():
            continue
        for path in sorted(zone_dir.rglob("*.md")):
            if path.name.startswith("."):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            _front, body = page_policy.split_frontmatter(text)
            offending = []
            for match in _WIKILINK_RE.finditer(body):
                target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
                stem = target.rsplit("/", 1)[-1].removesuffix(".md")
                if stem and _DATE_STEM_RE.match(stem):
                    offending.append(stem)
            if offending:
                rel = str(path.relative_to(root))
                findings.append(build_finding(
                    check=CHECK_DATE_BEARING_BODY_LINK, severity=SEVERITY_WARN, subject=rel,
                    detail=(f"its body links \"[[{offending[0]}]]\" in prose"
                            + (f" (and {len(offending) - 1} more)" if len(offending) > 1 else "")
                            + " — a date-bearing page name belongs in `sources:`/`related:` "
                              "frontmatter, never in a body sentence"),
                    suggested_action=(
                        "move the pointer into the page's `sources:` (or `related:`) frontmatter "
                        "list and reword the sentence without the bracketed date-bearing name"),
                ))
    return findings


# ── entity pages that are still their own template ────────────────────────────────────────────
# `ops/templates/entity.md` marks every unwritten span with angle brackets, and the birth door
# copies the template VERBATIM into the committed page — so a minted identity says nothing about
# itself until somebody writes it, and nothing counted those pages before this check: the orphan
# check exempts entity pages by type, and no other check reads a body at all.
#
# The zone folder is spelled in TWO segments on purpose: the gardener package may hold no literal
# path fragment under the knowledge checkout (`tests/test_architecture.py`), because it never
# writes and must not read as though it could address a file.
_ENTITY_ZONE = ("wiki", "entities")


def is_placeholder_line(line: str) -> bool:
    """The template's angle-marker convention: a line that IS a placeholder, start to end.

    Deliberately literal, and it has a known false positive: a body line that is a whole one-line
    HTML element (`<details>`, `<!-- a comment -->`) reads as a placeholder here. That is ACCEPTED
    v1 behaviour rather than engineered around — the finding is `info`, the repair it invites is a
    drafted body a steward reads before approving, and a heuristic that tried to tell markup from
    a placeholder would be a second, worse parser of somebody's prose. The mirror-image gap is
    equally deliberate: a placeholder carried under a list bullet (`- <fact…>`) is not a
    placeholder LINE and does not fire.
    """
    stripped = line.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def placeholder_lines(body: str) -> list[str]:
    """Every placeholder line in a body — the ONE spelling of "this body still carries its
    template". `check_entity_placeholder_bodies` reports on it and `sweep.select_empty_body_pages`
    EXCLUDES on it, and the two must be the same predicate or a page would be reported by both
    checks or by neither."""
    return [line for line in body.splitlines() if is_placeholder_line(line)]


def is_blank_body(body: str) -> bool:
    """Nothing below the title: an empty body, or an H1 and blank lines and no more. The ONE
    spelling, for `placeholder_lines`' reason — the deterministic check reports on it and the
    model pass excludes on it, and a page must never fall between the two. It used to: a blank
    body carries no placeholder LINE, and the model's rubric asks about a body that is "WRITTEN
    but says nothing" — which a blank one is not. Blank is decidable without a model, so it is
    answered here, free and exact, with the same finding and the same repair."""
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return not lines


# What one entity page may weigh before this walk refuses to open it at all. A FIXED figure, not
# an env setting: it bounds the shape of one file's contribution, never how much of the population
# is judged, which is the line `settings.py`'s own docstring draws. Generous by an entity page's
# standards (a written one is a few kilobytes) and small next to a process's memory, because the
# walk reads the whole zone into memory BEFORE the empty-body pass's ceiling applies — the ceiling
# bounds the model spend and has never bounded this.
MAX_ENTITY_PAGE_BYTES = 256_000


def entity_zone_pages(repo: str, *, walk_stats: dict | None = None) -> list[dict]:
    """Every page in the entity zone of the repo checkout, read from disk: `{"path", "body"}`
    dicts, `path` relative to the checkout root, `body` the text under the frontmatter, ordered by
    path. `walk_stats`, when given, gets this walk's own exclusion counters — the same shared-sink
    convention the windowed checks' `population_stats` uses.

    The ONE walk of that zone, and it happens ONCE per run: `run.run_gardener` calls it and hands
    the SAME list to `check_entity_placeholder_bodies` and to the sweep's empty-body pass, because
    the second EXCLUDES what the first reported. Two walks minutes apart (they used to straddle
    the editorial sweep's model call) would let a page edited in between be reported by both
    checks or by neither, and the exclusion is only exact over one page set.

    **A symlinked leaf or a symlinked path component is refused, not followed.** What this walk
    reads no longer stays on the machine: a body reaches a model prompt and a sanitized excerpt is
    persisted into `gardener_findings.detail`, printed in the terminal report and rendered in the
    admin console. `wiki/entities/Acme.md -> /proc/self/environ` would ship that file to the model
    provider. Both halves are needed and `librarian.gather._confined` gives the reasoning for
    each: `page.is_inside` resolves the whole path, since a symlinked DIRECTORY component is
    invisible to a leaf `islink` test, and the leaf test catches a link pointing back inside the
    zone — contained, and still not the bytes git tracks.

    Never `pages_index`: it stores the body with its frontmatter already parsed away, and it is a
    different tree from the one a repair would be applied to. A file that cannot be read is
    skipped rather than raised on — a corpus health check that dies on one unreadable page reports
    nothing about the rest — but every skip is COUNTED into `walk_stats`, never dropped in
    silence: a check whose whole justification is "a silent miss reads as nothing wrong" cannot
    drop a page from its own population without saying so.
    """
    root = pathlib.Path(repo)
    zone_dir = root.joinpath(*_ENTITY_ZONE)
    dropped = {"unconfined": 0, "unreadable": 0, "oversized": 0}
    pages = []
    # A symlinked zone DIRECTORY is refused whole and counted as one: `page.is_inside` resolves its
    # root, so a link there would read as confined and hide the entire real population behind it.
    if zone_dir.is_symlink():
        dropped["unconfined"] += 1
    elif zone_dir.is_dir():
        for path in sorted(zone_dir.rglob("*.md")):
            if path.name.startswith("."):
                continue
            if path.is_symlink() or not page_policy.is_inside(
                    str(zone_dir), str(path.relative_to(zone_dir))):
                dropped["unconfined"] += 1
                continue
            try:
                if path.stat().st_size > MAX_ENTITY_PAGE_BYTES:
                    dropped["oversized"] += 1
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                dropped["unreadable"] += 1
                continue
            _front, body = page_policy.split_frontmatter(text)
            pages.append({"path": str(path.relative_to(root)), "body": body})
    if any(dropped.values()):
        # Counts, never the paths: this line is an operator's terminal and a cron log, and the
        # durable account is `job_runs.stats`.
        log.warning("gardener: the entity-zone walk excluded %d unconfined, %d unreadable and %d "
                    "oversized file(s) — a symlinked page in a knowledge repo has no legitimate "
                    "producer in this system", dropped["unconfined"], dropped["unreadable"],
                    dropped["oversized"])
    if walk_stats is not None:
        walk_stats.update(dropped)
    return pages


def check_entity_placeholder_bodies(pages: list[dict]) -> list[dict]:
    """Population: `entity_zone_pages`'s own list, walked once per run and handed to this check
    and to the model's empty-body pass alike — a page list, never a repo path, so this check is a
    pure function of what the walk found. One INFO finding per page whose body still carries at
    least one placeholder line."""
    action = ("no command — the repair proposer drafts a body from the pages anchored to this "
              "entity; approve it in the review lane, or edit the page by hand")
    findings = []
    for page in pages:
        placeholders = placeholder_lines(page["body"])
        if placeholders:
            findings.append(build_finding(
                check=CHECK_ENTITY_PLACEHOLDER_BODY, severity=SEVERITY_INFO,
                subject=page["path"],
                # The COUNT, never the lines themselves: a finding's detail reaches a model's
                # prompt and a Slack digest, and this one has nothing to say that the page's own
                # text says better to whoever opens it.
                detail=(f"its body still carries {len(placeholders)} unwritten placeholder line"
                        f"{'' if len(placeholders) == 1 else 's'} from the entity template — this "
                        f"identity exists and says nothing about itself"),
                suggested_action=action))
        elif is_blank_body(page["body"]):
            findings.append(build_finding(
                check=CHECK_ENTITY_PLACEHOLDER_BODY, severity=SEVERITY_INFO,
                subject=page["path"],
                detail=("its body says nothing at all below the title — this identity exists "
                        "and says nothing about itself"),
                suggested_action=action))
    return findings
