"""The eight deterministic checks — each a query over the index/registry tables or the repo
checkout, none interpreting meaning. Every function returns a list of finding dicts (`{"check",
"severity", "source", "subject", "detail", "suggested_action"}` — `source` is always
`SOURCE_DETERMINISTIC` here; the model editorial sweep in `sweep.py` is the only writer of
`SOURCE_MODEL`), never touches `gardener_findings` itself (that is `store.py`'s job) and never
prints anything (that is `report.py`'s).

**Population notes, read together with `docs/decisions/024-gardener-digest.md`:**

- `check_orphans`, `check_aging_seeds` and `check_company_page_names_entity` read `pages_index`
  directly; `check_stale_views`, `check_dead_vocabulary` and `check_date_bearing_body_links` read
  the repo checkout instead. Both shapes answer the same question: corpus/registry health.
- The two windowed checks (`check_anchor_concentration`, `check_company_wide_fraction`) resolve
  "the last N filings" out of `capture_queue` — real `timestamptz`, real filing semantics — never
  the `updated`/`as_of` TEXT columns, which describe a PAGE's own authored date, not when it was
  FILED. A `result_ref` that does not parse, or that resolves to a page no longer indexed, is
  skipped and counted — never guessed at.
- Those two checks and `check_company_page_names_entity` exclude PROVENANCE-type pages
  (`librarian.page.is_provenance_type` — "meeting", "source") from their notion of "declared
  company-wide"/"anchored to an entity": a provenance page's `entity: []` means "the extractor
  found no evidence", never a checked company-wide declaration — counting it either way would make
  these three checks lie about exactly the population they exist to measure.
- `check_aging_seeds` ages off `updated` (`current_date - updated::date`, computed IN Postgres,
  never in Python) — the only per-page date this corpus carries (`index.corpus.PageRow`; there is
  no `created` column). Clock injection over sleeps: a test backdates the FIXTURE row via SQL and
  lets the check's own `current_date`/`now()` do the comparison, the same pattern
  `capture.retention`'s own test suite already uses.
"""
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

# ── check slugs — code with the exemption/population reasons beside them, never silent ─────────
CHECK_ORPHAN_PAGE = "orphan-page"
CHECK_AGING_SEED = "aging-seed"
CHECK_STALE_VIEW = "stale-view"
CHECK_ANCHOR_CONCENTRATION = "anchor-concentration"
CHECK_DEAD_VOCABULARY = "dead-vocabulary"
CHECK_COMPANY_WIDE_FRACTION = "company-wide-fraction"
CHECK_COMPANY_PAGE_NAMES_ENTITY = "company-page-names-entity"
# A style convention, not a safety property, so it lives here as a finding rather than in the
# meeting flow as a filing veto — gates veto the irreversible; the gardener flags conventions.
# `librarian.processing` names the same slug where it explains that it does NOT veto this, so one
# grep finds both halves of the rule.
CHECK_DATE_BEARING_BODY_LINK = "date-bearing-body-link"

ALL_CHECK_SLUGS = (
    CHECK_ORPHAN_PAGE, CHECK_AGING_SEED, CHECK_STALE_VIEW, CHECK_ANCHOR_CONCENTRATION,
    CHECK_DEAD_VOCABULARY, CHECK_COMPANY_WIDE_FRACTION, CHECK_COMPANY_PAGE_NAMES_ENTITY,
    CHECK_DATE_BEARING_BODY_LINK,
)


def build_finding(*, check: str, severity: str, subject: str, detail: str,
            suggested_action: str, source: str = SOURCE_DETERMINISTIC, **extra) -> dict:
    """The one place a finding dict is assembled — shared by every check IN this module and by
    `gardener.sweep.to_finding` for a model-sourced one (`source=SOURCE_MODEL`, `model_id=...`
    riding through `**extra`, the same escape hatch the SLA notice's `_notice_detail`/
    `_notice_action` keys use). Public rather than module-private for exactly that cross-module
    reuse — `store.py` only ever reads the SIX named keys below plus `model_id`, never any other
    `extra` key; `notice.py` is the one reader of `_notice_detail`/`_notice_action`, falling back
    to `detail`/`suggested_action` when they are absent."""
    finding = {
        "check": check, "severity": severity, "source": source,
        "subject": subject, "detail": detail[:MAX_DETAIL_CHARS],
        "suggested_action": suggested_action,
    }
    finding.update(extra)
    return finding


def count_indexed_pages(conn) -> int:
    """`pages_index`'s total row count — the report header's "checked N pages" (`run.py`; kept
    here, not there, so it lives beside this module's OWN other raw `pages_index` touches).

    This module is not the package's only raw `pages_index` reader — `sweep.select_pages` queries
    it directly too. Both files are named in
    `tests/test_architecture.py::ACL_REACHABILITY_EXCEPTIONS`, for the same stated reason: an
    operator tool, terminal output only, with no caller identity to scope reads to."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pages_index")
        return cur.fetchone()[0]


# ── orphans ──────────────────────────────────────────────────────────────────────────────────
# The exemption list: TYPES a zero-inbound page never gets flagged for, each with its own stated
# reason — never by silence. Extensible; a new entry is a reviewed code change, not a quiet skip.
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
    """Population: `zone = 'wiki'` pages of a non-exempt type with zero inbound wikilinks
    (`NOT EXISTS ... links @> ARRAY[path]` — a live containment check via the GIN index, never the
    `inlinks` column, which is only as fresh as the last FULL index rebuild)."""
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
# A hand-edited `updated:` value that is not a date must not be able to take the health surface
# down. Casting every non-empty `updated` unconditionally makes Postgres RAISE mid-query on
# `updated: "next week"`, aborting this check and — since `_run_all_checks` runs the checks inline
# — the WHOLE run with it: status='error', zero findings, every day, until a human diagnoses a cast
# error. The health surface must not be takeable-down by the exact data defect it exists to
# surface.
#
# Guarding the value's SHAPE is not enough. `updated ~ '^\d{4}-\d{2}-\d{2}'` before the cast passes
# `updated: "2026-02-30"` — a real, non-existent calendar date, arguably the more likely
# hand-editing mistake of the two — and `updated::date` still raises on it, aborting the run
# through the identical mechanism. Worse, PostgreSQL does not guarantee left-to-right evaluation of
# ANDed WHERE conditions at all (the planner is free to reorder quals by estimated cost), so "the
# regex runs before the cast" is never a rule — only the shape a given plan happens to pick, and a
# plan change alone could reintroduce the abort with no code change at all.
#
# So: `pg_input_is_valid(updated, 'date')` (PostgreSQL 16+; the local stack pins
# `pgvector/pgvector:pg16`) never raises and tests the VALUE, not the shape, wrapped in
# `CASE WHEN ... THEN ... ELSE false END` — `CASE` is PostgreSQL's documented mechanism for
# conditional evaluation (unlike a plain function call's arguments, a `CASE` branch not taken is
# never evaluated at all), so this forces "is it valid" to run before "cast and compare" regardless
# of what the planner would otherwise choose to do with two independent ANDed quals. A malformed
# (non-empty, calendar-invalid OR shape-invalid) `updated` is excluded and COUNTED
# (`_MALFORMED_UPDATED_SQL`, the same predicate negated), never silently dropped, matching this
# package's own "counted, never silently dropped" discipline (`_recent_filed_pages`'s identical
# posture, one section over). The SELECT list's own `age_days` expression still casts
# `updated::date` directly — safe, since it is only ever evaluated for rows that already survived
# the WHERE clause's `CASE`, which by then has proven `updated` a valid date.
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
    """The age query `check_aging_seeds` runs: rows in `statuses` older than `days`, with a
    malformed (non-empty, invalid as a date — shape OR calendar value) `updated` excluded from the
    age comparison and counted separately into `population_stats["malformed_updated"]` when given.
    Kept generic in `statuses` so a second status-scoped age check needs no second query."""
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
    `threshold_days` (`STIGMERGY_GARDENER_AGING_SEED_DAYS`, default 30). `seed` is a legal, if
    rarely-written, status value — `index.rank`'s own maturity factor ranks it against
    `evergreen` — so both it and `developing` count as "a page still being worked on".
    `population_stats`, when given, gets this check's own malformed-`updated` count."""
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
    """Population: every entity `views.staleness.list_stale_entities` names (member-hash
    mismatch since the view was last generated) — reused verbatim, never re-derived. No
    threshold: staleness here is a hash mismatch, not an age.

    `views.staleness`, not `views.regenerate`: `regenerate.py` module-level-imports `views.writer`
    (the commit-and-push path), so importing IT would load the full git write stack into every
    gardener process — exactly the reach this package's whole design promise (findings-only, no
    write path) rules out. `staleness.py` is the read-only extraction of these two population
    functions with none of that."""
    return [
        build_finding(
            check=CHECK_STALE_VIEW, severity=SEVERITY_WARN, subject=entity_id,
            detail="the view's member set has changed since it was last generated",
            # The WHOLE value is one code span: a real, runnable command is backtick-quoted, and
            # the backticks are baked into the stored string rather than added by the report
            # renderer, so `--json` and the printed report carry the identical value. "Executable
            # verbatim" means the text BETWEEN the backticks, the ordinary markdown-code-span
            # reading — never the backticks themselves as shell syntax.
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
    """The last `window` FILED `capture_queue` rows, resolved to their page. Returns `(pages,
    stats)`: `pages` is `[{"path", "type", "entity"}]` for every row that resolved to an indexed,
    NON-PROVENANCE page (a provenance page's `entity` is never a checked declaration, so it cannot
    honestly count as "anchored" or "company-wide" either); `stats` counts everything that did not
    resolve — an unparseable `result_ref`, or one naming a page no longer indexed — never silently
    guessed at."""
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
    """Population: the last `window` filed pages (`STIGMERGY_GARDENER_CONCENTRATION_WINDOW`, default
    30), each contributing to every entity its `entity:` array names (membership, not a single
    top pick — mirrors `index.rank`'s own "any element" boost). Fires when the single most-
    anchored entity's share exceeds `share_threshold` (`STIGMERGY_GARDENER_CONCENTRATION_SHARE`,
    default 0.6). An empty window (nothing filed yet, or nothing survived the population filter)
    fires nothing — there is no "share" of zero filings.

    `population_stats`, when given, gets THIS check's own `_recent_filed_pages` exclusion counters
    written into it under `"anchor_concentration"`, so an operator can see how many of the window's
    filings were dropped (an unparsed `result_ref`, a page no longer indexed, a provenance page)
    before this check ever got to judge a share. `run.py` passes one shared dict so this check and
    `check_company_wide_fraction` land in the SAME `job_runs.stats` key, keyed by check name."""
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
    `views.staleness.list_all_anchored_entities` (reused, never re-derived) — zero pages, in
    either `wiki/` or `sources/`, declare `entity: [<id>]`. `views.staleness`, not
    `views.regenerate` — see `check_stale_views`'s own docstring immediately above for why."""
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
    """Population: the same last-`window` filed pages `check_anchor_concentration` reads
    (`STIGMERGY_GARDENER_COMPANY_WINDOW`, default 20), sharing `_recent_filed_pages` rather than a
    second "which filings count" query. Fires when the share declaring `entity: []` (company-wide
    — provenance pages already excluded, so `[]` here always means the checked declaration, never
    "no evidence found") exceeds `share_threshold` (`STIGMERGY_GARDENER_COMPANY_SHARE`, default 0.3).
    No single subject — this is a corpus-wide fraction, not a per-page or per-entity fact.

    `population_stats`, when given, gets this check's own exclusion counters under
    `"company_wide_fraction"` — see `check_anchor_concentration`'s identical parameter for why."""
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
    """`[(entity_id, [name, id, *aliases])]`, entities sorted by id — every registered spelling
    `check_company_page_names_entity` tests a page's body against, not only the display name: an
    id or an alias appearing verbatim is exactly as strong a signal."""
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
    """The first of `spellings` found as exact, word-bounded text in `body` (case-insensitive) —
    word-bounded so a short alias/id is never credited for matching as a substring of an unrelated,
    longer word.

    **Lookarounds (`(?<!\\w)`/`(?!\\w)`), never `\\b`.** `\\b` matches at a word/non-word
    TRANSITION, which means a trailing `\\b` right after a spelling ending in punctuation
    (`"Beta Robotics, Inc."`) requires the very NEXT character in the body to be a word
    character — in ordinary prose that position is almost always whitespace or more punctuation,
    so the match could never actually fire: a control that appears to work, per alias, and
    silently never does for any alias with a trailing non-word character. `(?!\\w)`
    asserts only "not immediately followed by a word character" — true for whitespace, punctuation
    or end of string alike — which is the property this check actually wants; `(?<!\\w)` is its
    mirror on the leading edge, preserving the original "not a substring of a longer word" intent
    exactly."""
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
    whole corpus, not a window, because this check has no threshold to window against. For each,
    every registered entity's name/id/aliases are tested against the body; one finding per (page,
    entity) match, naming the matched spelling and the entity it resolved to. The copy says
    "company-wide", the term `docs/reference/page-contract.md` uses for the same declaration."""
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
                    "instead; a re-anchor has to be done by hand — edit `entity:` on the page "
                    "in the knowledge repo yourself, commit and push, since a hand edit in the "
                    "wiki zone never passes through the filing gates at all (that zone is "
                    "people's to edit, not a capture's). If the content itself needs restating, file a "
                    "superseding page instead; and if the page really is company-wide, leaving "
                    "it alone is a legitimate answer too"),
            ))
    return findings


# ── date-bearing wikilinks in body prose ──────────────────────────────────────────────────────
# Only a meeting page's own filename carries a calendar date
# (`wiki/meetings/YYYY-MM-DD-<slug>.md`), so a `[[YYYY-MM-DD-…]]` target in any page's BODY prose
# is a pointer that belongs in `sources:`/`related:` frontmatter instead. Style, not safety, which
# is why this is a finding here and not a veto at filing time — see
# `docs/decisions/027-the-contraction.md`.
_DATE_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\b")
# A wikilink target, alias/anchor stripped: `[[target|alias#anchor]]` resolves by `target` alone,
# and a leading `!` prefixes an embed.
_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")


def check_date_bearing_body_links(repo: str) -> list[dict]:
    """Population: every page in the three content zones, read from the repo checkout (the same
    posture as `check_stale_views`/`check_dead_vocabulary`: this is a corpus-shape question, not
    an index one). One WARN finding per offending page, naming the first offending stem — the fix
    (move the pointer into `sources:`/`related:` frontmatter) is per-page, so one line per page is
    what an operator acts on."""
    import pathlib

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
