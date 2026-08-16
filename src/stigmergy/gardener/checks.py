"""The eight deterministic checks. Each returns a list of finding dicts, never touches
`gardener_findings` (`store.py`'s job) and never prints (`report.py`'s).

Population invariants: the two windowed checks resolve "the last N filings" out of
`capture_queue` — never the `updated`/`as_of` TEXT columns, which describe a page's authored
date, not when it was FILED. Those two and `check_company_page_names_entity` exclude
provenance-type pages: a provenance page's `entity: []` means "the extractor found no evidence",
never a checked company-wide declaration. `check_aging_seeds` ages `updated` IN Postgres, so a
test backdates the fixture row and lets `current_date` do the comparison.
"""
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

ALL_CHECK_SLUGS = (
    CHECK_ORPHAN_PAGE, CHECK_AGING_SEED, CHECK_STALE_VIEW, CHECK_ANCHOR_CONCENTRATION,
    CHECK_DEAD_VOCABULARY, CHECK_COMPANY_WIDE_FRACTION, CHECK_COMPANY_PAGE_NAMES_ENTITY,
    CHECK_DATE_BEARING_BODY_LINK,
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
            suggested_action: str, source: str = SOURCE_DETERMINISTIC, **extra) -> dict:
    """The one place a finding dict is assembled — shared by every check here and by
    `gardener.sweep.to_finding` (`model_id` and the `_notice_*` keys ride through `**extra`).
    `store.py` persists the six named keys plus `model_id` and nothing else."""
    finding = {
        "check": check, "severity": severity, "source": source,
        "subject": subject, "detail": detail[:MAX_DETAIL_CHARS],
        "suggested_action": suggested_action,
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
    never re-derived. Staleness is a member-hash mismatch, not an age. Import `views.staleness`,
    never `views.regenerate`: the latter would load the git write stack into every gardener
    process."""
    return [
        build_finding(
            check=CHECK_STALE_VIEW, severity=SEVERITY_WARN, subject=entity_id,
            detail="the view's member set has changed since it was last generated",
            # Backticks baked into the stored string, so `--json` and the printed report carry
            # the identical value; the runnable command is the text between them.
            suggested_action=f"`stigmergy-views regenerate --entity {entity_id}`",
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
                "no command retires an entity — `stigmergy-entities` only mints (no un-birth/"
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
