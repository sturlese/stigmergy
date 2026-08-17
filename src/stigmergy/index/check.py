"""The retrieval-substrate lint: the knowledge repo's linter lints the PAGES, this lints the
INDEX — the only other witness to a substrate defect is eyeballing a search result.

Deterministic SQL over `pages_index`, plus one optional file read. ERROR (exit 1 via the CLI):
duplicate `page_id` (golden expectations and chain grouping both key on it), orphan continuation
part (a part-shaped id with no primary beside it — the chain machinery treats it as its own
document), missing embedding / empty tsv (a page invisible to one arm is a silent retrieval
hole). WARN: dangling `superseded_by` (may be legitimately historical), anchored-but-unregistered
entity (resolves for navigation but gets no aliases, no entity-first search, no TOLD boost).

The registry is read through `kernel.registry`, the one reader, never through `stigmergy.server` —
the index sits below the server, and packages talk through files. The shared loader rather than a
local parse, so the lint accepts exactly what the server loads. WHICH COPY it reads is
`served_registry`'s answer: the index's snapshot wherever the database has one, the
`--entity-registry` file where it does not — the server's own order, because a lint reading a
different copy than the server serves reports on a world nobody is living in.
The lint sees the WHOLE index by design: it is an operator tool with no caller identity to scope
to, a named ACL exception in `tests/test_architecture.py`.
"""
import os
import posixpath

from stigmergy.index import store
from stigmergy.index.rank import chain_base
from stigmergy.kernel import registry as kernel_registry

FINDING_SEVERITIES = ("error", "warn")

# What names the served copy in a parse error. There is no path to give when the bytes came out of
# Postgres, and the operator still has to know WHICH copy to fix.
SNAPSHOT_ORIGIN = "entity_registry_snapshot (the index's cached ops/entity-registry.json)"


def _finding(severity: str, check: str, detail: str) -> dict:
    return {"severity": severity, "check": check, "detail": detail}


def served_registry(conn, registry_path: str | None) -> tuple[str | None, str]:
    """The registry the SERVER answers from, as `(TEXT, origin)`: the index's snapshot wherever
    this database has one, the `--entity-registry` file where it does not.

    `server.service.BrainService._registry_source`'s order, mirrored here rather than imported
    (`stigmergy.index` sits below `stigmergy.server`). It exists because the console lints a LIVE
    index: on a deployed server the registry file is the copy baked at deploy time, so every entity
    minted since the rollout would be reported `anchored-but-unregistered` — "no aliases, no
    entity-first search, no TOLD boost" — while the server has full records for all of them out of
    the snapshot (issue #74). Two surfaces, one answer, or the console teaches an operator to
    distrust it."""
    snapshot = store.read_entity_registry(conn)
    if snapshot is not None:
        return snapshot, SNAPSHOT_ORIGIN
    return _read_registry_file(registry_path), registry_path or ""


def _read_registry_file(path: str | None) -> str | None:
    """A registry FILE's text, or None when there is no file to read — the "no registry to check
    against" answer, distinct from an empty one."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def registry_ids(path: str | None) -> set[str] | None:
    """`registry_ids_from_text` over a FILE — the file-only road, for a caller with no index
    connection to prefer a snapshot from."""
    return registry_ids_from_text(_read_registry_file(path), path or "")


def registry_ids_from_text(text: str | None, origin: str) -> set[str] | None:
    """The registry's id set, or None when there is no registry to check against (the coverage
    check is then skipped, so None and an empty set mean different things). A malformed registry
    raises: a broken registry is an operator-visible fault.

    Read through `kernel.registry`, the one reader, and not by hand: a second parse validates
    whatever it happens to check, which here was strictly less than the loader — a nameless entity
    passed this lint and then made every real consumer refuse the file. A lint that blesses a
    substrate the server will not load is worse than no lint.
    """
    if text is None:
        return None
    return set(kernel_registry.registry_from_text(text, origin).entities)


def run_checks(conn, registry_path: str | None = None, *,
               registry: tuple[str | None, str] | None = None) -> list[dict]:
    """Every finding over the live `pages_index`, ordered errors-first then by check name.

    `registry` is a resolved `(TEXT, origin)` pair — what `served_registry` returns, and what the
    console and `stigmergy-index --check` pass so that both lint the copy the server serves.
    `registry_path` is the file-only road, and it is what a caller naming ONE file to lint uses."""
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

    known = (registry_ids_from_text(*registry) if registry is not None
             else registry_ids(registry_path))
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
