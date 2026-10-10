"""Erase one layer completely (specs/data-erasure.md §3): its folder from every branch and tag of
the knowledge repository, its evidence objects, and every row that holds or names its content.

The writer runs one request at a time under its lock. The rewrite happens in a scratch mirror and is
pushed atomically with a lease on every ref, after its commit map is stored; from then on every step
can run again with the same map. Rows change in one transaction, which also lists the evidence
objects to delete once it commits.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

from psycopg.types.json import Jsonb

from stigmergy.capture import references, schema, uploads
from stigmergy.changes import diff
from stigmergy.index import build, corpus, fragments, measures, store
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.kernel import layers
from stigmergy.librarian import bootstrap, gitcmd
from stigmergy.ops import erasure

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 900
ZERO = "0" * 40
REDACTED = {"redacted": "layer erased"}
MIN_NAME = 4
REBUILD_ATTEMPTS = 3
OWN_ACTIONS = ("layer.erase", "layer.erased")
GITHUB_PURGE_HELP = ("https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/"
                     "removing-sensitive-data-from-a-repository")


class ErasureError(RuntimeError):
    """The erasure cannot go on from this step."""


def run_next(conn, *, repo: str, branch: str, evidence, embedder_factory, scratch_root: str = "",
             on_output=print) -> dict | None:
    """Run the oldest due erasure, if any; returns it as it ended."""
    item = erasure.claim(conn)
    if item is None:
        return None
    return run_claimed(conn, item, repo=repo, branch=branch, evidence=evidence,
                       embedder_factory=embedder_factory, scratch_root=scratch_root,
                       on_output=on_output)


def run_claimed(conn, item: dict, *, repo: str, branch: str, evidence, embedder_factory,
                scratch_root: str = "", on_output=print) -> dict:
    """Run one claimed erasure in its tenant's checkout; the connection acts for that tenant."""
    try:
        receipt = erase(conn, item, repo=repo, branch=branch, evidence=evidence,
                        embedder_factory=embedder_factory, scratch_root=scratch_root)
    except Exception as error:   # noqa: BLE001 — every failure is recorded on the request
        final = (item["attempts"] >= erasure.MAX_ATTEMPTS
                 and not _may_be_pushed(erasure.latest(conn, item["layer"]), repo, branch))
        erasure.fail(conn, item["id"], f"{error.__class__.__name__}: {_safe(str(error))}",
                     final=final)
        log.error("erasure of layer %s failed (%s); %s", item["layer"], error.__class__.__name__,
                  "giving up" if final else "will retry")
        on_output(f"erase {item['layer']} -> {'failed' if final else 'retry'}")
        return erasure.latest(conn, item["layer"])
    erasure.finish(conn, item["id"], receipt, actor=item["requested_by"])
    on_output(f"erase {item['layer']} -> succeeded")
    return erasure.latest(conn, item["layer"])


def _may_be_pushed(item: dict, repo: str, branch: str) -> bool:
    """Only a request whose rewrite verifiably never reached the remote may fail for good: a failed
    one reopens the layer."""
    if item["pushed"]:
        return True
    if not item["new_head"]:
        return False
    try:
        return _reached(repo, branch, item["rewrite"])
    except Exception:  # noqa: BLE001 — unknown is treated as pushed
        return True


def erase(conn, item: dict, *, repo: str, branch: str, evidence, embedder_factory,
          scratch_root: str = "") -> dict:
    layer = item["layer"]
    folder = erasure.prefix(layer)
    names = item["names"] or erasure.record_names(conn, item["id"], _page_names(conn, layer))
    if not item["pushed"] and item["new_head"] and _reached(repo, branch, item["rewrite"]):
        erasure.mark_pushed(conn, item["id"])      # pushed before a stop; its map is stored
        item = {**item, "pushed": True}
    if not item["pushed"]:
        item = _rewrite_and_push(conn, item, repo=repo, branch=branch, folder=folder,
                                 scratch_root=scratch_root)
    rewrite = item["rewrite"]
    _reset_checkout(repo, branch, item["new_head"])
    if gitcmd.run("log", "--all", "--format=%H", "--", folder, cwd=repo,
                  timeout=GIT_TIMEOUT_S).stdout.strip():
        raise ErasureError("the knowledge repository holds the layer again after the rewrite")
    with conn.transaction():
        erasure.add_counts(conn, item["id"], _purge(
            conn, item["id"], evidence, repo=repo, layer=layer, folder=folder, names=names,
            commit_map=rewrite["commit_map"], new_head=item["new_head"]))
    counts = erasure.add_counts(conn, item["id"],
                                {"objects_deleted": _collect_garbage(conn, evidence, item["id"])})
    pages = _rebuild_index(conn, repo, embedder_factory, layer)
    commit_map = rewrite["commit_map"]
    return {
        "layer": layer,
        "requested_by": item["requested_by"],
        "old_head": item["old_head"],
        "new_head": item["new_head"],
        "commits_removed": sum(1 for new in commit_map.values() if new == ZERO),
        "commits_rewritten": sum(1 for old, new in commit_map.items() if new not in (ZERO, old)),
        "refs_rewritten": len(rewrite["refs"]),
        "pages_indexed": pages,
        **counts,
        "remaining": _remaining(rewrite["first_changed"]),
    }


# --- the knowledge repository ---------------------------------------------------------------

def _rewrite_and_push(conn, item: dict, *, repo: str, branch: str, folder: str,
                      scratch_root: str) -> dict:
    url = gitcmd.origin_url(repo)
    if not url:
        raise ErasureError("the writer's checkout has no origin to rewrite")
    scratch = gitcmd.scratch_path(repo, scratch_root)
    try:
        gitcmd.run(*bootstrap.credential_args(url), "clone", "--mirror", "--quiet", url, scratch,
                   timeout=GIT_TIMEOUT_S)
        for ref in _refs(scratch, "refs/pull"):
            gitcmd.run("update-ref", "-d", ref, cwd=scratch)
        foreign = [ref for ref in _refs(scratch, "refs")
                   if not ref.startswith(("refs/heads/", "refs/tags/"))]
        if foreign:
            raise ErasureError(f"the repository has refs the erasure cannot rewrite: {foreign[:3]}")
        before = _ref_values(scratch)
        old_head = before.get(f"refs/heads/{branch}", "")
        _filter(scratch, folder)
        left = gitcmd.run("log", "--all", "--format=%H", "--", folder, cwd=scratch,
                          timeout=GIT_TIMEOUT_S).stdout.split()
        if left:
            raise ErasureError(f"{len(left)} commit(s) still hold the layer after the rewrite")
        after = _ref_values(scratch)
        new_head = after.get(f"refs/heads/{branch}", "")
        changed = sorted(ref for ref in before if after.get(ref) != before[ref])
        rewrite = {"commit_map": _pairs(scratch, "commit-map"), "refs": changed,
                   "after": {ref: after.get(ref, "") for ref in changed},
                   "first_changed": sorted(_pairs(scratch, "first-changed-commits"))}
        erasure.record_rewrite(conn, item["id"], old_head=old_head, new_head=new_head,
                               rewrite=rewrite)
        if changed:
            # A lease per ref: a ref someone else moved since the mirror was taken fails the
            # whole atomic push instead of being overwritten.
            leases = [f"--force-with-lease={ref}:{before[ref]}" for ref in changed]
            specs = [f"{ref}:{ref}" if ref in after else f":{ref}" for ref in changed]
            pushed = gitcmd.run(*bootstrap.credential_args(url), "push", "--atomic", "--quiet", *leases,
                                url, *specs, cwd=scratch, check=False, timeout=GIT_TIMEOUT_S)
            if pushed.returncode != 0:
                raise ErasureError("the rewritten history was not pushed; nothing changed: "
                                   + gitcmd._scrub(pushed.stderr).strip()[:300])
        erasure.mark_pushed(conn, item["id"])
        return {**item, "old_head": old_head, "new_head": new_head, "rewrite": rewrite,
                "pushed": True}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _reached(repo: str, branch: str, rewrite: dict) -> bool:
    """Whether the remote already holds the stored rewrite: every changed ref at its rewritten value
    (or gone), the branch perhaps with later commits on top."""
    url = gitcmd.origin_url(repo)
    out = gitcmd.run(*bootstrap.credential_args(url), "ls-remote", url, "refs/heads/*", "refs/tags/*",
                     cwd=repo, timeout=GIT_TIMEOUT_S).stdout
    remote = dict(reversed(line.split()) for line in out.splitlines() if line.strip()
                  and not line.endswith("^{}"))
    target = f"refs/heads/{branch}"
    for ref, value in rewrite.get("after", {}).items():
        if ref != target and remote.get(ref, "") != value:
            return False
    head = rewrite.get("after", {}).get(target)
    if head is None or remote.get(target) == head:
        return True
    gitcmd.run("fetch", "--quiet", "origin", f"+{target}:refs/remotes/origin/{branch}", cwd=repo,
               timeout=GIT_TIMEOUT_S)
    known = gitcmd.run("cat-file", "-e", f"{head}^{{commit}}", cwd=repo, check=False)
    return known.returncode == 0 and gitcmd.run(
        "merge-base", "--is-ancestor", head, f"origin/{branch}", cwd=repo,
        check=False).returncode == 0


def _refs(repo: str, namespace: str) -> list[str]:
    out = gitcmd.run("for-each-ref", "--format=%(refname)", namespace, cwd=repo).stdout
    return [line for line in out.splitlines() if line]


def _ref_values(repo: str) -> dict[str, str]:
    out = gitcmd.run("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags",
                     cwd=repo).stdout
    return dict(line.split() for line in out.splitlines() if line)


def _filter(repo: str, folder: str) -> None:
    """Remove `folder` from every commit of every ref; commits left empty disappear. Sensitive-data
    mode also records the first changed commits a GitHub Support request needs."""
    process = subprocess.run(
        [sys.executable, "-c", "import sys, git_filter_repo; sys.exit(git_filter_repo.main())",
         "--path", folder, "--invert-paths", "--sensitive-data-removal", "--force", "--quiet"],
        cwd=repo, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
    if process.returncode != 0:
        raise ErasureError("the history rewrite failed: " + process.stderr.strip()[:300])


def _pairs(repo: str, name: str) -> dict[str, str]:
    """An `old new` file of filter-repo as `{old: new}`; a removed commit maps to forty zeros."""
    path = os.path.join(repo, "filter-repo", name)
    if not os.path.exists(path):
        return {}
    mapping = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) == 2 and len(parts[0]) == 40 and parts[0] != "old":
                mapping[parts[0]] = parts[1]
    return mapping


def _reset_checkout(repo: str, branch: str, new_head: str) -> None:
    """Bring the writer's checkout to the rewritten history, plus anything filed since, and drop
    every object of the old one."""
    gitcmd.run("fetch", "--quiet", "--prune", "--prune-tags", "--force", "origin",
               "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*",
               cwd=repo, timeout=GIT_TIMEOUT_S)
    if gitcmd.run("merge-base", "--is-ancestor", new_head, f"origin/{branch}", cwd=repo,
                  check=False).returncode != 0:
        raise ErasureError("the knowledge branch no longer holds the rewritten history")
    gitcmd.run("checkout", "--quiet", "--force", "-B", branch, f"origin/{branch}", cwd=repo)
    for ref in _refs(repo, "refs"):
        if ref != f"refs/heads/{branch}" and not ref.startswith(("refs/remotes/origin/", "refs/tags/")):
            gitcmd.run("update-ref", "-d", ref, cwd=repo)
    gitcmd.run("update-ref", "-d", "ORIG_HEAD", cwd=repo, check=False)
    gitcmd.run("worktree", "prune", cwd=repo)
    gitcmd.run("reflog", "expire", "--expire=now", "--all", cwd=repo, timeout=GIT_TIMEOUT_S)
    gitcmd.run("gc", "--quiet", "--prune=now", cwd=repo, timeout=GIT_TIMEOUT_S)


# --- rows and evidence ----------------------------------------------------------------------

def _purge(conn, erasure_id, evidence, *, repo: str, layer: str, folder: str, names: list[str],
           commit_map: dict, new_head: str) -> dict:
    """Every row change, in the caller's transaction: the layer's captures and what hangs from
    them, the change ledger, redaction, remapped commits, and the evidence objects left without a
    reference."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM capture_queue WHERE acl = %s::text[] FOR UPDATE", ([layer],))
        captures = [str(row[0]) for row in cursor.fetchall()]
    candidates = _capture_refs(conn, captures)
    counts = {"captures_deleted": 0, "operations_failed": 0, "rows_redacted": 0}
    patterns = [f"%{_like(folder)}%"] + [f"%{capture}%" for capture in captures]
    logged = patterns + [f"%{_like(name)}%" for name in names]
    with conn.cursor() as cursor:
        if captures:
            for table, column in (("capture_artifacts", "capture_id"), ("upload_sessions", "consumed_by"),
                                  ("slack_submissions", "submission_id")):
                if _exists(cursor, table):
                    cursor.execute(f"DELETE FROM {table} WHERE {column} = ANY(%s::uuid[])", (captures,))
            cursor.execute("DELETE FROM capture_queue WHERE id = ANY(%s::uuid[])", (captures,))
            counts["captures_deleted"] = cursor.rowcount
        # Other open work that names the layer cannot be done any more; it fails, then is redacted.
        cursor.execute(
            f"""UPDATE capture_queue SET status = '{schema.FAILED}', lease_started_at = NULL,
                    finished_at = now(), error_category = 'layer_erased',
                    error = 'it named an erased layer'
                WHERE status IN ('{schema.QUEUED}', '{schema.PROCESSING}')
                  AND (request::text LIKE ANY(%s) OR report::text LIKE ANY(%s))""",
            (patterns, patterns))
        counts["operations_failed"] = cursor.rowcount
    counts.update(_rewrite_ledger(conn, evidence, repo, folder, commit_map, candidates))
    with conn.cursor() as cursor:
        for table, columns, matching, extra in (
                ("capture_queue", ("request", "extraction", "report"), patterns, ""),
                ("job_runs", ("stats",), patterns, ""),
                ("audit_log", ("args", "result"), logged, ""),
                ("admin_actions", ("args",), logged, " AND action <> ALL(%s)")):
            if not _exists(cursor, table):
                continue
            for column in columns:
                cursor.execute(f"UPDATE {table} SET {column} = %s "
                               f"WHERE {column}::text ILIKE ANY(%s){extra}",
                               (Jsonb(REDACTED), matching, *([list(OWN_ACTIONS)] if extra else [])))
                counts["rows_redacted"] += cursor.rowcount
        for table in ("capture_queue", "job_runs"):
            cursor.execute(f"UPDATE {table} SET error = %s WHERE error ILIKE ANY(%s)",
                           (REDACTED["redacted"], patterns))
            counts["rows_redacted"] += cursor.rowcount
        removed = [old for old, new in commit_map.items() if new == ZERO]
        moved = {old: new for old, new in commit_map.items() if new not in (ZERO, old)}
        olds, news = list(moved), list(moved.values())
        for table, columns in (("capture_queue", ("commit_sha",)),
                               ("job_runs", ("base_commit_sha", "head_commit_sha"))):
            for column in columns:
                cursor.execute(f"UPDATE {table} SET {column} = '' WHERE {column} = ANY(%s)",
                               (removed,))
                _remap(cursor, table, column, olds, news)
        if _exists(cursor, "graph_measures"):
            # A snapshot holds counts only. The layer's own series goes; the others keep their
            # history, minus the snapshot now at the new head, so the rebuild measures it afresh.
            cursor.execute("DELETE FROM graph_measures WHERE scope = %s OR indexed_commit = ANY(%s)",
                           (layer, removed))
            _remap(cursor, "graph_measures", "indexed_commit", olds, news)
            cursor.execute("DELETE FROM graph_measures WHERE indexed_commit = %s", (new_head,))
    erasure.add_garbage(conn, erasure_id,
                        {ref for ref in candidates if not references.is_live(conn, ref)})
    return counts


def _capture_refs(conn, captures: list[str]) -> set[str]:
    """Every evidence object the captures point at, filed or not: originals, readable text, and
    the uploads they consumed."""
    if not captures:
        return set()
    with conn.cursor() as cursor:
        cursor.execute(
            """SELECT ref FROM capture_artifacts a
                   CROSS JOIN LATERAL (VALUES (a.original_ref), (a.readable_ref)) AS r(ref)
                   WHERE a.capture_id = ANY(%(ids)s::uuid[])
               UNION SELECT artifact->>'blob_ref' FROM capture_queue q,
                   jsonb_array_elements(coalesce(q.request->'artifacts', '[]')) AS artifact
                   WHERE q.id = ANY(%(ids)s::uuid[])
               UNION SELECT artifact->>'readable_ref' FROM capture_queue q,
                   jsonb_array_elements(coalesce(q.extraction->'artifacts', '[]')) AS artifact
                   WHERE q.id = ANY(%(ids)s::uuid[])
               UNION SELECT blob_ref FROM upload_sessions WHERE consumed_by = ANY(%(ids)s::uuid[])""",
            {"ids": captures})
        refs = {row[0] for row in cursor.fetchall() if row[0]}
        cursor.execute("SELECT id FROM upload_sessions WHERE consumed_by = ANY(%s::uuid[])",
                       (captures,))
        return refs | {uploads.staging_ref(row[0]) for row in cursor.fetchall()}


def _rewrite_ledger(conn, evidence, repo: str, folder: str, commit_map: dict,
                    garbage: set[str]) -> dict:
    """Drop the changes that only touched the layer, rebuild the patch of any that also touched
    another folder, and point every other change at its rewritten commit. A patch carries blob ids,
    never commit ids, so a change that did not touch the layer keeps its patch as it is. Replaced
    patches join `garbage`."""
    counts = {"changes_deleted": 0, "changes_rewritten": 0, "changes_remapped": 0}
    with conn.cursor() as cursor:
        cursor.execute("SELECT id, commit_sha, parent_commit_sha, manifest, exact_patch_ref, summary "
                       "FROM knowledge_changes ORDER BY created_at")
        rows = cursor.fetchall()
    for change_id, commit, parent, manifest, patch_ref, summary in rows:
        paths = [entry.get("path", "") for entry in manifest]
        inside = [path for path in paths if path.startswith(folder)]
        new_commit = commit_map.get(commit, commit)
        with conn.cursor() as cursor:
            if (inside and len(inside) == len(paths)) or new_commit == ZERO:
                cursor.execute("DELETE FROM knowledge_changes WHERE id = %s", (change_id,))
                garbage.add(patch_ref)
                counts["changes_deleted"] += 1
                continue
            new_parent = _parent(repo, new_commit)
            if inside:
                patch = diff.exact_patch(repo, new_parent, new_commit)
                reasons = {entry.get("path", ""): entry.get("reason", "") for entry in manifest}
                rebuilt = diff.build_manifest(repo, new_parent, new_commit, reasons=reasons,
                                              default_reason=summary)
                cursor.execute(
                    "UPDATE knowledge_changes SET commit_sha = %s, parent_commit_sha = %s, "
                    "manifest = %s, exact_patch_ref = %s, exact_patch_sha256 = %s, "
                    "exact_patch_bytes = %s WHERE id = %s",
                    (new_commit, new_parent,
                     Jsonb([entry.model_dump(mode="json") for entry in rebuilt]),
                     evidence.put(diff.compressed_patch(patch)), diff.patch_sha256(patch),
                     len(patch), change_id))
                garbage.add(patch_ref)
                counts["changes_rewritten"] += 1
            elif new_commit != commit or new_parent != parent:
                cursor.execute("UPDATE knowledge_changes SET commit_sha = %s, parent_commit_sha = %s "
                               "WHERE id = %s", (new_commit, new_parent, change_id))
                counts["changes_remapped"] += 1
    return counts


def _collect_garbage(conn, evidence, erasure_id) -> int:
    """Delete the listed evidence objects nothing references now; a capture filed since the list
    was made may have stored the same bytes again. A rerun after a stop deletes what is left."""
    deleted = sum(1 for ref in erasure.garbage(conn, erasure_id)
                  if not references.is_live(conn, ref) and evidence.delete(ref))
    erasure.clear_garbage(conn, erasure_id)
    return deleted


def _parent(repo: str, commit: str) -> str:
    process = gitcmd.run("rev-parse", "--verify", "--quiet", f"{commit}^", cwd=repo, check=False)
    return process.stdout.strip()


def _page_names(conn, layer: str) -> list[str]:
    """Virtual paths, titles and aliases of the layer's indexed pages. Readers name pages this way,
    so a log row that does is redacted too; one redacted because another layer shares a title loses
    nothing anyone needs. Names shorter than `MIN_NAME` match too much to redact by."""
    with conn.cursor() as cursor:
        if not _exists(cursor, "pages_index"):
            return []
        cursor.execute("SELECT path, title, aliases FROM pages_index WHERE acl = %s::text[]",
                       ([layer],))
        rows = cursor.fetchall()
    names = set()
    for path, title, aliases in rows:
        names.add(layers.virtual_path(path))
        names.update(name for name in (title, *(aliases or ())) if len(name or "") >= MIN_NAME)
    return sorted(names)


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _remap(cursor, table: str, column: str, olds: list[str], news: list[str]) -> None:
    if olds:
        cursor.execute(f"UPDATE {table} AS t SET {column} = m.new "
                       "FROM unnest(%s::text[], %s::text[]) AS m(old, new) "
                       f"WHERE t.{column} = m.old", (olds, news))


def _exists(cursor, table: str) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return bool(cursor.fetchone()[0])


# --- the index --------------------------------------------------------------------------------

def _rebuild_index(conn, repo: str, embedder_factory, layer: str) -> int:
    """Rebuild at the current head, then drop every cached vector no current fragment uses. A
    webhook delivery of the forced push may have measured the old index meanwhile; such snapshots go
    and the head is measured again."""
    meta = store.read_meta(conn) or {}
    embedder = embedder_factory()
    for attempt in range(REBUILD_ATTEMPTS):
        with conn.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            started = cursor.fetchone()[0]
        try:
            stats = build.rebuild(conn, repo, embedder, meta.get("fts_config") or "english")
            break
        except StigmergyIndexError:     # the webhook's delivery of the forced push moved the index
            if attempt == REBUILD_ATTEMPTS - 1:
                raise
    rows = corpus.load_pages(repo)
    whole = fragments.page_fragments(rows)
    long_entities = {row.path for row in rows if row.type == "entity"
                     and len(row.body.encode("utf-8")) > fragments.WHOLE_SAFE_BYTES}
    cut = fragments.page_fragments(rows, cut=frozenset(long_entities)) if long_entities else []
    used = sorted({piece.content_hash for piece in [*whole, *cut]})
    head = gitcmd.run("rev-parse", "HEAD", cwd=repo).stdout.strip()
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM embedding_cache WHERE NOT (content_hash = ANY(%s))", (used,))
        cursor.execute("DELETE FROM graph_measures WHERE scope = %s "
                       "OR (indexed_commit = %s AND taken_at < %s)", (layer, head, started))
    measures.record_snapshot(conn, head)
    return int(stats.get("pages", 0))


def _remaining(first_changed: list[str]) -> list[dict]:
    """What the platform cannot erase, with when it goes."""
    return [
        {"what": "GitHub's cached copies of the old commits, and pull requests that reach them",
         "until": "GitHub Support removes them", "link": GITHUB_PURGE_HELP,
         "first_changed_commits": first_changed},
        {"what": "Supabase backups of the database", "until": "their retention ends"},
        {"what": "clones of the knowledge repository made before the erasure",
         "until": "their owners delete them"},
    ]


def _safe(text: str) -> str:
    return gitcmd._scrub(" ".join(text.split()))[:300]
