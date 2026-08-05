"""The two sections' data — each a query over the gardener/corpus tables, returning plain dicts;
`render.py` is the only reader and the only place any of this becomes text (mirrors
`gardener.checks`/`gardener.report`'s own split, one package over: never touches the store itself,
never prints anything).

**How each section is populated:**

- **corpus health** reads the LATEST completed `job='gardener'` run
  (`gardener.store.latest_completed_run`) and, only when its own `finished_at` falls inside the
  window, its findings (`gardener.store.findings_for_run`) — both reused, never re-derived; this
  package writes no `gardener_findings` row and runs no check itself.
- **corpus deltas**: "pages filed" resolves `capture_queue.result_ref`/`report` through
  `_filed_page_paths` below — the shared answer to the meeting-capture blind spot
  (`librarian.report.filed_meeting`'s own shape: `result_ref` names only the MEETING page; the
  source page(s) and every decision page live in `report['filed_meeting']`, verified against
  `librarian/processing.py::_file_meeting`). A correction is single-page in practice —
  `librarian.processing._cross_check_outcome` vetoes a multi-page fast-lane filing — but the
  general-case helper is shared anyway, so neither query has to independently reason about a case
  the other already handles correctly. "Entities born" reads the governed-birth log
  (`review_decisions`, `item_kind='entity-proposal'`, `verdict='approve'`): entity approval IS the
  birth event, timestamped and append-only; never `pages_index.updated` (an entity page updates
  long after its birth) and never a registry run-over-run diff (its window would be the GARDENER's
  run cadence, not the digest's own, and a first gardener run has no predecessor to diff against).

**The broadcast scope** (`stigmergy.digest`'s own package docstring): every page this module names —
every "pages filed" title — passes through `_visible_pages`, which is the
ONE place `server.acl.visible()` is called in this package. `audiences` is always a real
`set[str]` (`slack.channels.channel_audiences`' own return contract: never `None`), so a page is
counted and named here if, and only if, the CHANNEL this digest is about to post to could see it —
never the operator's own, unscoped view of the corpus.

**Why this module reads `pages_index`/`capture_queue`/`review_decisions` directly, by raw SQL,
rather than through a `gardener`-precomputed shape.** The tempting alternative for corpus deltas
is to have `stigmergy-gardener` compute "pages filed"/"entities born" into its own `job_runs.stats`
(it already reads the corpus every run) and have `digest` read them through the ONE edge it
already has, `gardener.store` — no new import edge, and a clean gardener-reads-corpus →
digest-reads-gardener's-output pipeline. It is not taken, for a reason that is structural rather
than a style preference: the digest broadcasts, so every page TITLE it names must be filtered at
the DESTINATION CHANNEL's own audience scope (`_visible_pages`, above) — and the gardener does not
know the destination channel. It is an operator tool with no caller identity at all (this
package's own layering notes; ADR 024 D5), so a title it precomputed into `job_runs.stats` would
necessarily be rendered against no audience, or the wrong one — a value that never passed an ACL
predicate, sitting in a persisted, ungated blob for any future reader of `job_runs` to see
unscoped. Reading the tables directly, HERE, at the one place that actually knows the posting
channel's own audiences, is what keeps the ACL-scoping decision where `acl.visible()` is the one
place it is ever made.
"""
from stigmergy.capture import schema as capture_schema
from stigmergy.gardener import schema as gardener_schema
from stigmergy.gardener import store as gardener_store
from stigmergy.server import review
from stigmergy.server.acl import visible
from stigmergy.text import parse_result_ref


# ── the shared page-resolution + ACL-scoping seam (see the package docstring) ────────────────────
def _filed_page_paths(report: dict | None, result_ref: str) -> list[str]:
    """Every page path ONE filed `capture_queue` row actually put in the repo.

    Held against `librarian.processing._file_meeting`'s own construction and
    `librarian.report.filed_meeting`'s own documented return shape: a meeting capture's
    `result_ref` names ONLY the meeting page (`'<meeting page path>@<sha>'`); the source page(s)
    (the verbatim transcript, possibly split into parts) and every decision page live in
    `report['filed_meeting']` — `source_pages: [...]`, `meeting_page: str`,
    `decisions: [{"path": ..., "anchored_to": ...}, ...]`. Every OTHER capture (`raw`/`page`, and
    a correction's own fast-lane promotion) has no `filed_meeting` key at all, and `result_ref`
    names its one page directly — the ordinary, single-page case this function handles when the
    key is absent.
    """
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
    """`path -> {"title": ...}` for every one of `paths` that is (a) still indexed and (b) `acl.
    visible()` clears for `audiences` — the digest's ONE ACL-filtering seam, shared by every
    caller below rather than re-derived per section. A path that is not (yet) indexed, or that
    fails the ACL check, is silently absent from the returned mapping: excluded from BOTH the
    count and the list a caller renders, never counted without being nameable (a count and a list
    that disagree is its own kind of dishonest report)."""
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
    honest cases: no gardener run has EVER completed; the latest one predates this window; or a
    real, in-window run whose findings (`gardener.store.findings_for_run`, the findings store this
    package is granted) are grouped by severity and, within `sla`/`warn`, by check slug — the exact
    shape `render.py` needs and never re-derives from raw finding dicts a second time.

    **`sweep_incomplete`**: `latest_completed_run` widens to `status IN ('ok', 'partial')`, so the
    "ok" state below can describe a run whose deterministic findings are complete but whose model
    sweep failed (`job_runs.stats.sweep.error`, set by `gardener.run._run_sweep_pass`) — read
    straight off the SAME `stats` blob `latest_completed_run` already returns, never a second
    query. `render._render_health` surfaces this so a reader is never left to infer "no sweep
    findings this run" as "the sweep found nothing" when it may instead mean "the sweep did not
    run to completion"."""
    run = gardener_store.latest_completed_run(conn)
    if run is None:
        return {"state": "never_run"}

    finished_at = run["finished_at"]
    if finished_at is None or finished_at < since:
        last_date = (finished_at or run["started_at"]).date()
        return {"state": "stale", "last_run_date": last_date,
                "days_before_window": (since.date() - last_date).days}

    findings = gardener_store.findings_for_run(conn, run["id"])
    counts = {gardener_schema.SEVERITY_SLA: 0, gardener_schema.SEVERITY_WARN: 0,
             gardener_schema.SEVERITY_INFO: 0}
    checks_by_severity: dict[str, dict[str, int]] = {}
    for f in findings:
        sev, chk = f["severity"], f["check"]
        counts[sev] = counts.get(sev, 0) + 1
        by_check = checks_by_severity.setdefault(sev, {})
        by_check[chk] = by_check.get(chk, 0) + 1
    sweep_incomplete = bool(((run["stats"] or {}).get("sweep") or {}).get("error"))
    return {"state": "ok", "run_date": finished_at.date(), "total": len(findings),
            "counts_by_severity": counts, "checks_by_severity": checks_by_severity,
            "sweep_incomplete": sweep_incomplete}


# ── corpus deltas — both queries bounded, `until` the same `now` `run.run_digest` resolves
# before either of them runs ──────────────────────────────────────────────────────────────────────
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


_ENTITIES_BORN_SQL = (
    "SELECT count(*) FROM review_decisions "
    "WHERE item_kind = %(item_kind)s AND verdict = %(verdict)s AND created_at >= %(since)s "
    "AND created_at < %(until)s")


def _entities_born_count(conn, *, since, until) -> int:
    with conn.cursor() as cur:
        cur.execute(_ENTITIES_BORN_SQL, {"item_kind": review.KIND_ENTITY_PROPOSAL,
                                         "verdict": review.APPROVE, "since": since,
                                         "until": until})
        return cur.fetchone()[0]


def gather_corpus_deltas(conn, *, since, until, audiences: set[str]) -> dict:
    """The section's two facts: pages filed (count + titles, ACL-scoped) and entities born (a
    COUNT only).

    Since ADR 030 a server-driven approval mints in the same act and records `entity_id`/`commit`
    in `review_decisions.extra`, so the ledger CAN name the entity for those rows — but not for
    every row: a CLI `stigmergy-entities approve` still writes no ledger row at all, and rows
    predating ADR 030 carry no `extra`. This query is a bare count and reads none of it. Naming
    entities here would therefore mean naming SOME of them, which reads as a complete list and is
    not one; re-deriving the rest from the capture row behind each approval would be a guess about
    a row that can have moved on by digest time (re-queued, re-processed) — exactly what this
    codebase's "never guessed at" discipline refuses. An honest count is what the data supports."""
    pages = _pages_filed(conn, since=since, until=until, audiences=audiences)
    return {
        "pages_filed_count": pages["count"],
        "pages_filed_titles": pages["titles"],
        "entities_born_count": _entities_born_count(conn, since=since, until=until),
    }
