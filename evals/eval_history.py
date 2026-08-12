"""`evals/history.ndjson` — the durable eval-score series.

Git-resident: the local Postgres is a disposable cache, so a series that must survive across
environments cannot live there. The file sits at the root of `evals/`, deliberately OUTSIDE
`evals/out/`, which is cleared freely.

Appended only by a golden runner that KNOWS it ran its real instrument — a run with a fake backend
or a fake embedder appends nothing, because a plumbing check has no quality number worth keeping.
The operator commits the new line with the change it measured.

An append failure warns and never fails the eval run: one lost line is recoverable by re-running,
minutes of real model spend are not.
"""
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HISTORY_PATH = Path(__file__).resolve().parent / "history.ndjson"
# Repo-relative, for the cleanliness probe's exclude pathspec (`resolve_git_sha`). A literal, not
# derived: the probe runs against whatever `root` the caller passes.
HISTORY_RELPATH = "evals/history.ndjson"
CORPUS_PATH = Path(__file__).resolve().parent / "corpus"


def corpus_provenance(repo_dir: str | None = None) -> dict:
    """`{corpus, stigmergy_sha}` — the entry's own answer to "what was this measured ON?".

    Read from the corpus's committed `PROVENANCE.json` rather than resolved at run time, so the
    recorded sha cannot drift under a live librarian pushing `main`. Best-effort: never fails for
    want of provenance.
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
    # A run against a LIVE knowledge repo has no PROVENANCE.json; its provenance is its own HEAD.
    # Without this fallback such an entry records a path and no sha at all.
    sha = resolve_git_sha(target)
    if sha:
        out["stigmergy_sha"] = sha
    return out


def resolve_git_sha(root: Path) -> str:
    """The checkout's current commit, with `-dirty` when the working tree does not match it. `''`
    on any failure — the sha is provenance, never a precondition for recording a measurement.

    Three rules the suffix depends on. A dirty run IS recorded: measuring before committing is
    normal, mislabelling it as the commit is not. `history.ndjson` is EXCLUDED from the probe, or
    one instrument's own append would stamp the next row `-dirty`. And an unprobeable tree reads
    as `-dirty`, never as clean — unknown must not be spelled the same as verified-clean.
    """
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

    Pure aside from the write: `git_sha` and `now` are the caller's, so this needs no subprocess
    and no clock to unit-test. Never raises — an `OSError` warns on stderr and is swallowed, so the
    run's exit code reflects what it measured rather than whether a file was writable.
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
    """Every line of the series, oldest first. `[]` for an absent file (an honest empty series, not
    an error); a corrupt line is skipped. Whether too little history means "nothing to compare" is
    the drift reader's decision, not this one's."""
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
