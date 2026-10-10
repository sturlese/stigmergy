"""Rebuild the derived index from Git truth."""
import subprocess
from pathlib import Path

import httpx

from stigmergy.index import corpus, fragments, health, measures, store
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.kernel import tenancy

# The rebuild limit must not couple the index to capture implementation details.
MAX_INDEXABLE_MARKDOWN_BYTES = 50 * 1024 * 1024
MAX_COMMITTED_INDEX_BYTES = 512 * 1024 * 1024
MAX_COMMITTED_TREE_ENTRIES = 500_000
MAX_COMMITTED_INDEX_ENTRIES = 250_000
MAX_COMMITTED_TREE_RECORD_BYTES = 16 * 1024
_TREE_READ_CHUNK_BYTES = 64 * 1024

_HEALTH_STATE_SQL = """
    SELECT xmin::text, repository_head_sha, indexed_commit_sha, dirty, dirty_since,
           last_incremental_at, last_full_rebuild_at, indexed_rows, updated_at
    FROM index_health WHERE singleton
"""


def _git(repo_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
    )


def _repository_head(repo_dir: str) -> str:
    head = _git(repo_dir, "rev-parse", "--verify", "HEAD")
    return head.stdout.strip() if head.returncode == 0 else ""


def _health_state(conn) -> tuple:
    health.ensure_schema(conn)
    with conn.cursor() as cursor:
        cursor.execute(_HEALTH_STATE_SQL)
        row = cursor.fetchone()
    if row is None:
        raise StigmergyIndexError("the index health row is missing")
    return tuple(row)


def _locked_health_state(conn, cursor) -> tuple:
    health.ensure_schema(conn, cursor=cursor)
    cursor.execute(f"{_HEALTH_STATE_SQL} FOR UPDATE")
    row = cursor.fetchone()
    if row is None:
        raise StigmergyIndexError("the index health row is missing")
    return tuple(row)


def _read_blob(stream, object_id: bytes) -> bytes:
    header = stream.readline(MAX_COMMITTED_TREE_RECORD_BYTES + 1)
    if len(header) > MAX_COMMITTED_TREE_RECORD_BYTES:
        raise StigmergyIndexError("the committed repository tree is invalid")
    fields = header.rstrip(b"\n").split()
    if len(fields) != 3 or fields[0] != object_id or fields[1] != b"blob":
        raise StigmergyIndexError("the committed repository tree is invalid")
    try:
        size = int(fields[2])
    except ValueError as error:
        raise StigmergyIndexError("the committed repository tree is invalid") from error
    return size


def _read_exact(stream, size: int) -> bytes:
    content = bytearray()
    while len(content) < size:
        block = stream.read(size - len(content))
        if not block:
            raise StigmergyIndexError("the committed repository tree is incomplete")
        content.extend(block)
    if stream.read(1) != b"\n":
        raise StigmergyIndexError("the committed repository tree is incomplete")
    return bytes(content)


def _committed_snapshot(
    repo_dir: str,
) -> tuple[str, list[tuple[str, str]]] | None:
    """Return index inputs from one committed tree, not a stale writer checkout."""
    commit = _repository_head(repo_dir)
    if not commit:
        return None
    tree = subprocess.Popen(
        ["git", "ls-tree", "-r", "-z", commit, "--", *corpus.TREE_ROOTS],
        cwd=repo_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    blobs = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=repo_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    pages: list[tuple[str, str]] = []
    tree_entries = 0
    index_entries = 0
    total = 0
    pending = b""
    try:
        if tree.stdout is None or blobs.stdin is None or blobs.stdout is None:
            raise StigmergyIndexError("could not read the committed repository tree")
        while chunk := tree.stdout.read(_TREE_READ_CHUNK_BYTES):
            records = pending + chunk
            start = 0
            while (end := records.find(b"\0", start)) >= 0:
                entry = records[start:end]
                start = end + 1
                tree_entries += 1
                if (
                    tree_entries > MAX_COMMITTED_TREE_ENTRIES
                    or len(entry) > MAX_COMMITTED_TREE_RECORD_BYTES
                ):
                    raise StigmergyIndexError("the committed repository tree exceeds the limit")
                try:
                    metadata, raw_path = entry.split(b"\t", 1)
                    mode, object_type, object_id = metadata.split()
                    path = raw_path.decode("utf-8")
                except (UnicodeDecodeError, ValueError) as error:
                    raise StigmergyIndexError("the committed repository tree is invalid") from error
                if (
                    mode not in {b"100644", b"100755"}
                    or object_type != b"blob"
                    or not corpus.is_indexable_page(path)
                ):
                    continue
                index_entries += 1
                if index_entries > MAX_COMMITTED_INDEX_ENTRIES:
                    raise StigmergyIndexError("the committed index entries exceed the limit")
                blobs.stdin.write(object_id + b"\n")
                blobs.stdin.flush()
                size = _read_blob(blobs.stdout, object_id)
                if size > MAX_INDEXABLE_MARKDOWN_BYTES:
                    raise StigmergyIndexError("an indexed Markdown page exceeds the size limit")
                total += size
                if total > MAX_COMMITTED_INDEX_BYTES:
                    raise StigmergyIndexError("the committed index input exceeds the size limit")
                content = _read_exact(blobs.stdout, size)
                try:
                    pages.append((path, content.decode("utf-8")))
                except UnicodeDecodeError as error:
                    raise StigmergyIndexError("an indexed repository page is not UTF-8") from error
            pending = records[start:]
            if len(pending) > MAX_COMMITTED_TREE_RECORD_BYTES:
                raise StigmergyIndexError("the committed repository tree exceeds the limit")
        if pending:
            raise StigmergyIndexError("the committed repository tree is invalid")
        if tree.wait() != 0:
            raise StigmergyIndexError("could not enumerate the committed repository tree")
        blobs.stdin.close()
        if blobs.wait() != 0:
            raise StigmergyIndexError("could not read the committed repository tree")
    finally:
        for process in (tree, blobs):
            if process.poll() is None:
                process.kill()
            process.wait()
    return commit, pages


def _checked_repository_head(repo_dir: str) -> str:
    root = Path(repo_dir).resolve()
    top = _git(str(root), "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
        raise StigmergyIndexError("--repo must be the root of a Git checkout")
    head = _git(str(root), "rev-parse", "--verify", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise StigmergyIndexError("the knowledge repository has no HEAD commit")
    status = _git(
        str(root),
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *corpus.TREE_ROOTS,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise StigmergyIndexError("the indexed corpus must match repository HEAD")
    return head.stdout.strip()


def _checked_upstream_tip(repo_dir: str, head: str) -> tuple[str, str] | None:
    """Return the current branch's local upstream tip after requiring it matches HEAD."""
    branch = _git(repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch.returncode != 0 or not (branch_name := branch.stdout.strip()):
        return None
    upstream = _git(
        repo_dir,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    if upstream.returncode != 0 or not (upstream_ref := upstream.stdout.strip()):
        remote = _git(repo_dir, "config", "--get", f"branch.{branch_name}.remote")
        merge = _git(repo_dir, "config", "--get", f"branch.{branch_name}.merge")
        if remote.returncode == 1 and merge.returncode == 1:
            return None
        raise StigmergyIndexError("the configured repository upstream cannot be resolved")
    tip = _git(repo_dir, "rev-parse", "--verify", "--quiet", upstream_ref)
    if tip.returncode != 0 or not (tip_sha := tip.stdout.strip()):
        raise StigmergyIndexError("the configured repository upstream cannot be resolved")
    if tip_sha != head:
        relation = _git(repo_dir, "merge-base", "--is-ancestor", head, tip_sha)
        if relation.returncode == 0:
            raise StigmergyIndexError(f"repository HEAD is behind {upstream_ref}")
        raise StigmergyIndexError(f"repository HEAD does not match {upstream_ref}")
    return upstream_ref, tip_sha


def embed_fragments(conn, embedder, rows, *, model: str = "") -> tuple[list, set, dict]:
    """`(fragments, content hashes already cached, freshly embedded vectors by hash)` for the page
    rows. The cache, keyed by `model` (default the embedder's), answers first and each distinct text is
    embedded once. A whole entity page above `fragments.WHOLE_SAFE_BYTES` is sent alone; if the model
    refuses it, that page is cut like a note. Every other text, cut pieces included, fits within
    those bytes and goes in one batch, where any refusal fails the call."""
    model = model or embedder.model
    pieces, cached = _pieces_and_cache(conn, rows, model, cut=frozenset())
    fresh: dict[str, list[float]] = {}
    refused: set[str] = set()
    for piece in pieces:
        if (piece.content_hash in cached or piece.content_hash in fresh or piece.type != "entity"
                or len(piece.embed_text.encode("utf-8")) <= fragments.WHOLE_SAFE_BYTES):
            continue
        try:
            fresh[piece.content_hash] = embedder.embed([piece.embed_text])[0]
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 400:
                raise
            refused.add(piece.path)
    if refused:
        pieces, cached = _pieces_and_cache(conn, rows, model, cut=frozenset(refused))
    unique = {piece.content_hash: piece.embed_text for piece in pieces
              if piece.content_hash not in cached and piece.content_hash not in fresh}
    if unique:
        keys = list(unique)
        fresh.update(zip(keys, embedder.embed([unique[key] for key in keys]), strict=True))
    return pieces, cached, fresh


def _pieces_and_cache(conn, rows, model: str, *, cut: frozenset) -> tuple[list, set]:
    pieces = fragments.page_fragments(rows, cut=cut)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('embedding_cache')")
        cache_exists = cur.fetchone()[0] is not None
    hashes = sorted({piece.content_hash for piece in pieces})
    return pieces, (store.cached_hashes(conn, model, hashes) if cache_exists else set())


def rebuild(
    conn,
    repo_dir: str,
    embedder,
    fts_config: str = "english",
    *,
    require_repository_head: bool = False,
) -> dict:
    """Atomically replace the connection's tenant's page index and return rebuild statistics."""
    tenancy.require_tenant(conn)
    starting_health = _health_state(conn)
    expected_head = _checked_repository_head(repo_dir) if require_repository_head else ""
    expected_tracking_tip = (
        _checked_upstream_tip(repo_dir, expected_head) if require_repository_head else None
    )
    snapshot = _committed_snapshot(repo_dir)
    committed_snapshot = snapshot is not None
    if not committed_snapshot:
        commit_sha = ""
        rows = corpus.load_pages(repo_dir)
    else:
        commit_sha, pages = snapshot
        rows = corpus.load_page_texts(pages)
        del pages, snapshot

    pieces, cached, fresh = embed_fragments(conn, embedder, rows)
    if fresh:
        dim = len(next(iter(fresh.values())))
    elif cached:
        dim = store.cached_dimension(conn, embedder.model, next(iter(cached)))
    else:
        dim = len(embedder.embed(["empty team wiki"])[0])
    with conn.transaction(), conn.cursor() as cursor:
        if _locked_health_state(conn, cursor) != starting_health:
            raise StigmergyIndexError("index health changed during the rebuild")
        if committed_snapshot and _repository_head(repo_dir) != commit_sha:
            raise StigmergyIndexError("repository HEAD changed during the rebuild")
        if require_repository_head:
            final_head = _checked_repository_head(repo_dir)
            final_tracking_tip = _checked_upstream_tip(repo_dir, final_head)
            if (
                final_head != expected_head
                or commit_sha != expected_head
                or final_tracking_tip != expected_tracking_tip
            ):
                raise StigmergyIndexError("repository HEAD changed during the rebuild")
        store.init_schema(conn, dim=dim, model=embedder.model, fts_config=fts_config,
                          host=getattr(embedder, "host", ""))
        store.clear_tenant_index(conn)
        if fresh:
            store.store_embeddings(conn, embedder.model, fresh)
        store.insert_pages(conn, rows, fts_config)
        store.replace_fragments(conn, (), pieces, embedder.model)
        store.create_search_indexes(conn)
        health.record_full_rebuild(conn, commit_sha, len(rows))
    measures.record_snapshot(conn, commit_sha)

    zones: dict[str, int] = {}
    for r in rows:
        zones[r.zone] = zones.get(r.zone, 0) + 1
    return {"pages": len(rows), "zones": zones, "fragments": len(pieces), "embedded": len(fresh),
            "cached": len(cached),
            "model": embedder.model, "dim": dim, "fts_config": fts_config,
            "commit_sha": commit_sha}
