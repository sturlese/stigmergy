"""Rebuild the derived index and repository control snapshots from Git truth."""
import subprocess
from pathlib import Path

from stigmergy.index import corpus, health, store
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.server.controls import ControlError, validate_texts

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


def _control_files(repo_dir: str, committed: dict[str, bytes] | None = None) -> dict[str, str]:
    controls = {}
    root = Path(repo_dir)
    for relpath in store.OPS_FILE_RELPATHS:
        if committed is None:
            path = root.joinpath(*relpath.split("/"))
            if not path.is_file() or path.is_symlink():
                raise StigmergyIndexError(f"required control file is missing: {relpath}")
            data = path.read_bytes()
        else:
            data = committed.get(relpath)
            if data is None:
                raise StigmergyIndexError(f"required control file is missing: {relpath}")
        if len(data) > store.MAX_OPS_FILE_BYTES:
            raise StigmergyIndexError(
                f"required control file exceeds the size limit: {relpath}"
            )
        try:
            controls[relpath] = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise StigmergyIndexError(
                f"required control file is not UTF-8: {relpath}"
            ) from error
    return controls


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
) -> tuple[str, dict[str, bytes], list[tuple[str, str]]] | None:
    """Return index inputs from one committed tree, not a stale writer checkout."""
    commit = _repository_head(repo_dir)
    if not commit:
        return None
    tree = subprocess.Popen(
        ["git", "ls-tree", "-r", "-z", commit, "--", *corpus.ZONES,
         *store.OPS_FILE_RELPATHS],
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
    controls: dict[str, bytes] = {}
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
                indexable = corpus.is_indexable_page(path)
                control = path in store.OPS_FILE_RELPATHS
                if mode not in {b"100644", b"100755"} or object_type != b"blob" or not (
                    indexable or control
                ):
                    continue
                index_entries += 1
                if index_entries > MAX_COMMITTED_INDEX_ENTRIES:
                    raise StigmergyIndexError("the committed index entries exceed the limit")
                blobs.stdin.write(object_id + b"\n")
                blobs.stdin.flush()
                size = _read_blob(blobs.stdout, object_id)
                if indexable and size > MAX_INDEXABLE_MARKDOWN_BYTES:
                    raise StigmergyIndexError("an indexed Markdown page exceeds the size limit")
                if control and size > store.MAX_OPS_FILE_BYTES:
                    raise StigmergyIndexError("required control file exceeds the size limit")
                total += size
                if total > MAX_COMMITTED_INDEX_BYTES:
                    raise StigmergyIndexError("the committed index input exceeds the size limit")
                content = _read_exact(blobs.stdout, size)
                if control:
                    controls[path] = content
                else:
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
    return commit, controls, pages


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
        *corpus.ZONES,
        *store.OPS_FILE_RELPATHS,
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


def rebuild(
    conn,
    repo_dir: str,
    embedder,
    fts_config: str = "english",
    *,
    require_repository_head: bool = False,
) -> dict:
    """Atomically replace the page index and return rebuild statistics."""
    starting_health = _health_state(conn)
    expected_head = _checked_repository_head(repo_dir) if require_repository_head else ""
    expected_tracking_tip = (
        _checked_upstream_tip(repo_dir, expected_head) if require_repository_head else None
    )
    snapshot = _committed_snapshot(repo_dir)
    committed_snapshot = snapshot is not None
    if not committed_snapshot:
        commit_sha = ""
        controls = _control_files(repo_dir)
        rows = corpus.load_pages(repo_dir)
    else:
        commit_sha, committed, pages = snapshot
        controls = _control_files(repo_dir, committed)
        rows = corpus.load_page_texts(pages)
        del committed, pages, snapshot
    try:
        validate_texts(controls)
    except ControlError as error:
        raise StigmergyIndexError(f"invalid repository controls: {error}") from error

    hashes = [r.content_hash for r in rows]
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('embedding_cache')")
        cache_exists = cur.fetchone()[0] is not None
    cached = store.cached_embeddings(conn, embedder.model, hashes) if cache_exists else {}

    to_embed = [r for r in rows if r.content_hash not in cached]
    unique: dict[str, str] = {}
    for r in to_embed:
        unique.setdefault(r.content_hash, r.embed_text)
    fresh: dict[str, list[float]] = {}
    if unique:
        keys = list(unique)
        vectors = embedder.embed([unique[h] for h in keys])
        fresh = dict(zip(keys, vectors, strict=True))

    embeddings = {**cached, **fresh}
    dim = (
        len(next(iter(embeddings.values())))
        if embeddings
        else len(embedder.embed(["empty team wiki"])[0])
    )
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
            if _control_files(repo_dir) != controls:
                raise StigmergyIndexError("repository controls changed during the rebuild")
        store.init_schema(conn, dim=dim, model=embedder.model, fts_config=fts_config,
                          host=getattr(embedder, "host", ""))
        if fresh:
            store.store_embeddings(conn, embedder.model, fresh)
        store.insert_pages(conn, rows, embeddings, fts_config)
        store.create_search_indexes(conn)
        for relpath, text in controls.items():
            store.write_ops_file(conn, relpath, text, "rebuild")
        health.record_full_rebuild(conn, commit_sha, len(rows))

    zones: dict[str, int] = {}
    for r in rows:
        zones[r.zone] = zones.get(r.zone, 0) + 1
    return {"pages": len(rows), "zones": zones, "embedded": len(fresh), "cached": len(cached),
            "model": embedder.model, "dim": dim, "fts_config": fts_config,
            "commit_sha": commit_sha,
            "entity_registry": "written",
            "ops_files": {relpath: "written" for relpath in store.OPS_FILE_RELPATHS}}
