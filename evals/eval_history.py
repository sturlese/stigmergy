"""`evals/history.ndjson` — the durable eval-score series.

**Git-resident, because git is this system's durable store.** The local Postgres is a disposable
cache by design (`db-down` wipes volumes; `make db-up` returns empty), so a score series that has
to survive across environments and across time cannot live there — and a local run writing into
staging's own database would cross environments neither side expects. `evals/history.ndjson` sits
at the root of `evals/`, deliberately OUTSIDE `evals/out/`, which holds disposable report output
and is cleared freely — this file must survive that.

**Appended only by the real-instrument golden runners** (`run_qa.py`, `run_retrieval.py`), and only
when each one KNOWS it ran its own real instrument: a keyless self-check of either runner (a
`--llm fake` QA run, a `--embedder fake` retrieval run) never appends, because a plumbing check has
no quality number worth keeping in a durable series. The operator commits the new line with the
change it measured — the score lands in the same PR as the code, which is what makes two entries
diffable at all.

**An append failure warns and never fails the eval run**: the measurement itself — minutes of real
model/embedder spend — is the thing that must not be thrown away over a disk hiccup or a read-only
checkout; losing one history line is recoverable by re-running, losing the whole eval result is
not.
"""
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HISTORY_PATH = Path(__file__).resolve().parent / "history.ndjson"
# Repo-relative, for the cleanliness probe's exclude pathspec — see `resolve_git_sha`. A literal
# rather than derived from HISTORY_PATH: the probe runs against whatever `root` the caller passes,
# and this is the path git knows the file by inside the repo.
HISTORY_RELPATH = "evals/history.ndjson"
CORPUS_PATH = Path(__file__).resolve().parent / "corpus"


def corpus_provenance(repo_dir: str | None = None) -> dict:
    """`{corpus, stigmergy_sha}` for the measured corpus — the entry's own answer to "what was
    this measured ON?", which requires the knowledge repo's SHA in the entry.

    Reading it from the corpus's committed `PROVENANCE.json` rather than resolving it at run
    time is the point: the frozen fixture carries its own origin, so the recorded SHA cannot
    drift under a live librarian pushing `main`. Best-effort in the same spirit as
    `resolve_git_sha` — a run measured against some other directory simply reports that
    directory and no SHA, and never fails for want of provenance.
    """
    target = Path(repo_dir) if repo_dir else CORPUS_PATH
    out: dict = {"corpus": str(target)}
    try:
        data = json.loads((target / "PROVENANCE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and data.get("stigmergy_sha"):
        out["stigmergy_sha"] = data["stigmergy_sha"]
        out["corpus_frozen_at"] = data.get("frozen_at", "")
        return out
    # A run against a LIVE git checkout (`--repo` pointed at a real knowledge repo) has no
    # PROVENANCE.json — its provenance is its own HEAD, resolved the same best-effort way as the
    # platform sha (`-dirty` suffix and all). Without this fallback such entries record the repo
    # path with no sha at all: an entry that cannot answer "what was this measured on?", the one
    # question the series exists to answer.
    sha = resolve_git_sha(target)
    if sha:
        out["stigmergy_sha"] = sha
    return out


def resolve_git_sha(root: Path) -> str:
    """The checkout's current commit, best-effort, with `-dirty` appended when the working tree
    does not match it. `''` on any failure (no git binary, a source tarball, a shallow clone with
    a dangling HEAD) — never raised: the sha is a nice-to-have provenance field, not a
    precondition for recording the measurement itself.

    **Why the suffix**: this series exists so an entry always
    says what it was measured on, and a bare `rev-parse HEAD` breaks that promise for the most
    common way an eval is actually run — against a working tree with the change still uncommitted.
    Rows have been recorded naming a commit whose code they had not measured. A `-dirty` marker
    is the honest minimum: it cannot say WHAT differed, but it stops a row from claiming a
    precision it does not have. A dirty run is still recorded — measuring before committing is
    normal and useful; silently mislabelling it is not.

    **The series file is EXCLUDED from the probe**, and that is not a convenience. `append_run`
    writes `history.ndjson`, which is tracked — so on a clean checkout, running one instrument
    dirties the tree for the next one, and the second row would be stamped `-dirty` over the first
    row's own bookkeeping. That happened in this instrument's first session (the `126286a` qa row).
    "Dirty" here has to mean *the measured code differs from the commit*, so the one file the
    measurement itself writes cannot count as evidence against it.

    **A failed probe reads as `-dirty`**, not as a third spelling. Unknown cleanliness must never
    be spelled the same as verified-clean — a `git status` that times out under contention would
    otherwise launder a dirty tree into a clean-looking row — and "assume dirty when unsure" says
    that with one concept instead of two."""
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, timeout=5, check=True)
        sha = result.stdout.strip()
    except Exception:  # noqa: BLE001 — any failure here means: no sha to report, not a fatal error
        return ""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", ".", f":(exclude){HISTORY_RELPATH}"],
            cwd=root, capture_output=True, text=True, timeout=5, check=True)
    except Exception:  # noqa: BLE001 — unprobeable; assume dirty, never imply clean
        return f"{sha}-dirty"
    return f"{sha}-dirty" if status.stdout.strip() else sha


def append_run(*, suite: str, metrics: dict, git_sha: str, path: Path | None = None,
              now: datetime | None = None) -> None:
    """Append one line — `{ts, git_sha, suite, **metrics}` — to `path` (default `HISTORY_PATH`).

    Pure aside from the one write: `git_sha`/`now` are supplied by the caller (`resolve_git_sha`/
    the wall clock are each a separate, injectable seam) rather than resolved here, so this
    function itself needs no subprocess and no clock to unit-test.

    Never raises. An `OSError` (a read-only checkout, a missing parent directory) is reported to
    stderr and swallowed — see the module docstring for why: the eval run's own exit code must
    keep reflecting what the run itself measured, not whether a history file happened writable.
    """
    line = {"ts": (now or datetime.now(UTC)).isoformat(), "git_sha": git_sha, "suite": suite,
           **metrics}
    target = path or HISTORY_PATH
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as ex:
        print(f"warning: could not append to {target} ({ex.__class__.__name__}) — the eval "
              f"result above is unaffected", file=sys.stderr)


def read_history(path: Path | None = None) -> list[dict]:
    """Every line of the series, oldest first — `[]` when the file is absent (a fresh checkout, or
    one before any real-instrument run ever committed a line, which is an honest empty series, not
    an error) or unreadable/malformed (a corrupt line is skipped, counted nowhere here — the reader
    that computes drift is what decides whether too little history means "nothing to compare")."""
    target = path or HISTORY_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
