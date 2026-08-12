"""The retrieval-substrate lint: the knowledge repo's linter lints the PAGES, this lints the
INDEX — the only other witness to a substrate defect is eyeballing a search result.

Deterministic SQL over `pages_index`, plus one optional file read. ERROR (exit 1 via the CLI):
duplicate `page_id` (golden expectations and chain grouping both key on it), orphan continuation
part (a part-shaped id with no primary beside it — the chain machinery treats it as its own
document), missing embedding / empty tsv (a page invisible to one arm is a silent retrieval
hole). WARN: dangling `superseded_by` (may be legitimately historical), anchored-but-unregistered
entity (resolves for navigation but gets no aliases, no entity-first search, no TOLD boost).

The registry is read as a FILE (id set only), never through `stigmergy.server` — the index sits
below the server, and packages talk through files. The lint sees the WHOLE index by design: it is
an operator tool with no caller identity to scope to, a named ACL exception in
`tests/test_architecture.py`.
"""
import json
import os
import posixpath

FINDING_SEVERITIES = ("error", "warn")


def _finding(severity: str, check: str, detail: str) -> dict:
    return {"severity": severity, "check": check, "detail": detail}


def registry_ids(path: str | None) -> set[str] | None:
    """The registry's id set, or None when there is no registry to check against (missing path or
    file — the coverage check is then skipped). Malformed JSON raises: a broken registry is an
    operator-visible fault."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("entities"), dict):
        raise ValueError(f"malformed entity registry at {path}: expected {{'entities': {{...}}}}")
    return set(data["entities"])


def run_checks(conn, registry_path: str | None = None) -> list[dict]:
    """Every finding over the live `pages_index`, ordered errors-first then by check name."""
    from stigmergy.index.rank import chain_base  # local: avoid import cycles at module load

    with conn.cursor() as cur:
        cur.execute("SELECT path, page_id, superseded_by, entity,"
                    "       embedding IS NULL AS no_embedding,"
                    "       (tsv IS NULL OR tsv = ''::tsvector) AS empty_tsv"
                    " FROM pages_index ORDER BY path")
        rows = [dict(zip(("path", "page_id", "superseded_by", "entity",
                          "no_embedding", "empty_tsv"), r, strict=True))
                for r in cur.fetchall()]

    findings: list[dict] = []

    by_id: dict[str, list[str]] = {}
    ids_present = set()
    by_dir_id: dict[tuple[str, str], str] = {}
    for r in rows:
        by_id.setdefault(r["page_id"], []).append(r["path"])
        ids_present.add(r["page_id"])
        by_dir_id[(posixpath.dirname(r["path"]), r["page_id"])] = r["path"]

    for pid, paths in sorted(by_id.items()):
        if len(paths) > 1:
            findings.append(_finding(
                "error", "duplicate-page-id",
                f"page_id {pid!r} is carried by {len(paths)} pages: {', '.join(paths)} — "
                f"golden expectations and chain grouping key on it; give one a frontmatter id:"))

    for r in rows:
        base = chain_base(r["page_id"])
        if base != r["page_id"]:      # a continuation part
            directory = posixpath.dirname(r["path"])
            if (directory, base) not in by_dir_id:
                findings.append(_finding(
                    "error", "orphan-continuation-part",
                    f"{r['path']} (page_id {r['page_id']!r}) has no primary {base!r} in "
                    f"{directory or '(root)'} — the chain machinery cannot group it"))

    for r in rows:
        if r["no_embedding"]:
            findings.append(_finding(
                "error", "missing-embedding",
                f"{r['path']} has no embedding — invisible to the vector arm"))
        if r["empty_tsv"]:
            findings.append(_finding(
                "error", "empty-tsv",
                f"{r['path']} has an empty tsv — invisible to the lexical arm"))

    for r in rows:
        target = r["superseded_by"]
        if target and target not in ids_present:
            findings.append(_finding(
                "warn", "dangling-superseded-by",
                f"{r['path']} says superseded_by {target!r}, which no indexed page carries"))

    known = registry_ids(registry_path)
    if known is not None:
        anchored = sorted({e for r in rows for e in (r["entity"] or ()) if e})
        for entity_id in anchored:
            if entity_id not in known:
                findings.append(_finding(
                    "warn", "anchored-but-unregistered",
                    f"entity {entity_id!r} anchors pages but has no registry record — no "
                    f"aliases, no entity-first search, no TOLD boost for it"))

    findings.sort(key=lambda f: (FINDING_SEVERITIES.index(f["severity"]), f["check"], f["detail"]))
    return findings


def render(findings: list[dict], pages: int) -> str:
    if not findings:
        return f"substrate check: {pages} pages, 0 findings — clean"
    lines = [f"substrate check: {pages} pages, {len(findings)} finding(s)"]
    lines += [f"  [{f['severity'].upper()}] {f['check']}: {f['detail']}" for f in findings]
    return "\n".join(lines)
