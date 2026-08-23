"""The two sections' data — plain dicts; `render.py` is the only reader and the only place any of
this becomes text.

Corpus health reads the latest completed gardener run through `gardener.store` — reused, never
re-derived. Corpus deltas: "pages filed" resolves `capture_queue.result_ref`/`report` through
`_filed_page_paths`; "entities born" sums `report -> 'entities_born'` over the same filings —
never `pages_index.updated` (an entity page updates long after its birth) and never a registry
run-over-run diff.

Every page this module names passes `_visible_pages`, the ONE `server.acl.visible()` call in the
package, at the destination channel's audiences. The tables are read directly HERE rather than
through a gardener-precomputed shape for a structural reason: the gardener does not know the
destination channel, so a title it precomputed would sit in `job_runs.stats` having passed no ACL
predicate at all — the scoping decision must live where the channel is known.
"""
from stigmergy.capture import schema as capture_schema
from stigmergy.gardener import schema as gardener_schema
from stigmergy.gardener import store as gardener_store
from stigmergy.repair import schema as repair_schema
from stigmergy.server.acl import visible
from stigmergy.text import parse_result_ref


# ── the shared page-resolution + ACL-scoping seam ────────────────────────────────────────────────
def _filed_page_paths(report: dict | None, result_ref: str) -> list[str]:
    """Every page path ONE filed `capture_queue` row actually put in the repo. A meeting
    capture's `result_ref` names only the MEETING page; the source page(s) and every decision
    page live in `report['filed_meeting']`. Every other capture has no `filed_meeting` key and
    `result_ref` names its one page directly."""
    filed_meeting = (report or {}).get("filed_meeting")
    if filed_meeting:
        paths = list(filed_meeting.get("source_pages") or [])
        meeting_page = filed_meeting.get("meeting_page") or ""
        if meeting_page:
            paths.append(meeting_page)
        paths += [d.get("path", "") for d in (filed_meeting.get("decisions") or [])]
        return [p for p in paths if p]
    parsed = parse_result_ref(result_ref)
    return [parsed[0]] if parsed else []


def _visible_pages(conn, paths: list[str], *, audiences: set[str]) -> dict[str, dict]:
    """`path -> {"title": ...}` for every path still indexed AND visible at `audiences` — the
    digest's one ACL-filtering seam. A path that fails either test is absent from BOTH the count
    and the list: never counted without being nameable."""
    if not paths:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT path, title, acl FROM pages_index WHERE path = ANY(%s)",
                    (sorted(set(paths)),))
        rows = cur.fetchall()
    return {path: {"title": title} for path, title, acl in rows if visible(acl, audiences)}


# ── corpus health ────────────────────────────────────────────────────────────────────────────────
def gather_corpus_health(conn, *, since) -> dict:
    """`{"state": "never_run"}`, `{"state": "stale", ...}` or `{"state": "ok", ...}` — the three
    honest cases. `model_passes_incomplete` is read off the run's own `stats` blob (a `'partial'` run's
    deterministic findings are complete but its sweep failed) so a reader never infers "the sweep
    found nothing" from "the sweep did not complete"."""
    run = gardener_store.latest_completed_run(conn)
    if run is None:
        return {"state": "never_run"}

    finished_at = run["finished_at"]
    if finished_at is None or finished_at < since:
        last_date = (finished_at or run["started_at"]).date()
        return {"state": "stale", "last_run_date": last_date,
                "days_before_window": (since.date() - last_date).days}

    findings = gardener_store.findings_for_run(conn, run["id"])
    counts = dict.fromkeys(gardener_schema.SEVERITIES, 0)
    checks_by_severity: dict[str, dict[str, int]] = {}
    for f in findings:
        sev, chk = f["severity"], f["check"]
        counts[sev] = counts.get(sev, 0) + 1
        by_check = checks_by_severity.setdefault(sev, {})
        by_check[chk] = by_check.get(chk, 0) + 1
    # An AGGREGATE over every model pass the run carries, not one pass's key: a run committed
    # 'partial' because the empty-body or the duplicate-identity pass failed used to render as a
    # clean run here — the silent clean bill this surface exists to end, closed in the terminal
    # report and left open one layer up. Sorted, so the rendered sentence is stable.
    stats = run["stats"] or {}
    incomplete = sorted(name for name in ("sweep", "empty_body", "duplicate_entity")
                        if (stats.get(name) or {}).get("error"))
    return {"state": "ok", "run_date": finished_at.date(), "total": len(findings),
            "counts_by_severity": counts, "checks_by_severity": checks_by_severity,
            "model_passes_incomplete": incomplete}


# ── corpus deltas — both queries bounded by the same `now` `run.run_digest` resolves first ───────
_FILED_PAGES_SQL = (
    "SELECT result_ref, report FROM capture_queue "
    "WHERE status = %(filed)s AND finished_at >= %(since)s AND finished_at < %(until)s "
    "ORDER BY finished_at, id")


def _pages_filed(conn, *, since, until, audiences: set[str]) -> dict:
    with conn.cursor() as cur:
        cur.execute(_FILED_PAGES_SQL, {"filed": capture_schema.FILED, "since": since,
                                       "until": until})
        rows = cur.fetchall()

    all_paths: list[str] = []
    seen: set[str] = set()
    for result_ref, report in rows:
        for path in _filed_page_paths(report, result_ref):
            if path not in seen:
                seen.add(path)
                all_paths.append(path)

    visible_pages = _visible_pages(conn, all_paths, audiences=audiences)
    ordered_visible = [p for p in all_paths if p in visible_pages]
    titles = [visible_pages[p]["title"] or p for p in ordered_visible]
    return {"count": len(ordered_visible), "titles": titles}


# An entity is born when a capture introduces it, so the count comes off the filings themselves —
# each filed report names the identities its own capture created (`librarian.report`,
# `entities_born`). There is no approval to count any more, and no second table that
# could disagree with the commits.
_ENTITIES_BORN_SQL = (
    "SELECT coalesce(sum(jsonb_array_length(report -> 'entities_born')), 0) "
    "FROM capture_queue "
    "WHERE status = %(filed)s AND finished_at >= %(since)s AND finished_at < %(until)s "
    "AND jsonb_typeof(report -> 'entities_born') = 'array'")


def _entities_born_count(conn, *, since, until) -> int:
    with conn.cursor() as cur:
        cur.execute(_ENTITIES_BORN_SQL, {"filed": capture_schema.FILED, "since": since,
                                         "until": until})
        return int(cur.fetchone()[0] or 0)


# What the worker REPAIRED in the window, by kind. A count and never a page list, for the reason
# the birth count is a count: a repair names the pages it edited, and this broadcast has no way to
# scope them to the destination channel's audiences without reading each one back. The kinds are a
# closed vocabulary (`repair.schema.KINDS`) and carry no corpus text at all.
#
# `applied` only. A `failed` row is an operator's business — it belongs in the console beside its
# refusing sentence, not in a weekly post to a channel that cannot act on it — and a `skipped` row
# is not an event at all.
_REPAIRS_APPLIED_SQL = (
    "SELECT kind, count(*) FROM repairs "
    "WHERE status = %(applied)s AND created_at >= %(since)s AND created_at < %(until)s "
    "GROUP BY kind")


def _repairs_applied(conn, *, since, until) -> dict:
    with conn.cursor() as cur:
        cur.execute(_REPAIRS_APPLIED_SQL, {"applied": repair_schema.STATUS_APPLIED,
                                           "since": since, "until": until})
        by_kind = {str(kind): int(count) for kind, count in cur.fetchall()}
    return {"count": sum(by_kind.values()), "by_kind": by_kind}


def gather_corpus_deltas(conn, *, since, until, audiences: set[str]) -> dict:
    """The section's three facts: pages filed (count + titles, ACL-scoped), entities born, and
    repairs applied — the last two COUNTS only.

    The identities are named on the filings that created them, and a page a channel may not see
    would be named through its entity here; the count is the honest half. A repair's pages are the
    same problem with no titles to scope, so it is counted by KIND, which is a closed vocabulary
    this code owns.
    """
    pages = _pages_filed(conn, since=since, until=until, audiences=audiences)
    repairs = _repairs_applied(conn, since=since, until=until)
    return {
        "pages_filed_count": pages["count"],
        "pages_filed_titles": pages["titles"],
        "entities_born_count": _entities_born_count(conn, since=since, until=until),
        "repairs_applied_count": repairs["count"],
        "repairs_by_kind": repairs["by_kind"],
    }
