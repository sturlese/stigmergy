"""Rebuild the derived index and repository control snapshots from a checkout."""
import subprocess
from pathlib import Path

from stigmergy.index import corpus, health, store
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.server.controls import ControlError, validate_texts


def _control_files(repo_dir: str) -> dict[str, str]:
    controls = {}
    root = Path(repo_dir)
    for relpath in store.OPS_FILE_RELPATHS:
        path = root.joinpath(*relpath.split("/"))
        if not path.is_file() or path.is_symlink():
            raise StigmergyIndexError(f"required control file is missing: {relpath}")
        data = path.read_bytes()
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


def rebuild(
    conn,
    repo_dir: str,
    embedder,
    fts_config: str = "english",
    *,
    require_repository_head: bool = False,
) -> dict:
    """Atomically replace the page index and return rebuild statistics."""
    expected_head = _checked_repository_head(repo_dir) if require_repository_head else ""
    controls = _control_files(repo_dir)
    try:
        validate_texts(controls)
    except ControlError as error:
        raise StigmergyIndexError(f"invalid repository controls: {error}") from error
    rows = corpus.load_pages(repo_dir)

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
    if require_repository_head:
        commit_sha = _checked_repository_head(repo_dir)
        if commit_sha != expected_head:
            raise StigmergyIndexError("repository HEAD changed during the rebuild")
        if _control_files(repo_dir) != controls:
            raise StigmergyIndexError("repository controls changed during the rebuild")
    else:
        git_head = _git(repo_dir, "rev-parse", "HEAD")
        commit_sha = git_head.stdout.strip() if git_head.returncode == 0 else ""
    with conn.transaction():
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
