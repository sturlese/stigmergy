"""Everything the librarian does with git: ephemeral worktrees, the diff the gates veto over,
the commit, the push. One subprocess shape: `capture_output=True, text=True`, non-zero raises
with truncated, credential-scrubbed stderr.
"""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace

from stigmergy.librarian.errors import GitError, LibrarianConfigError, WorktreeError

log = logging.getLogger(__name__)

STDERR_LIMIT = 600
# Startup reaping matches on this, so a crash's leftovers are identifiable with no stored state.
WORKTREE_PREFIX = "stigmergy-librarian-"

# `<prefix><repo key>-<creating pid>-<uuid>`. The KEY scopes the sweep to this checkout; the PID
# tells a crashed run's leftover from a live sibling's worktree on the SAME repo.
_WORKTREE_NAME_RE = re.compile(
    rf"^{re.escape(WORKTREE_PREFIX)}(?P<key>[0-9a-f]{{8}})-(?P<pid>\d+)-[0-9a-f]+$")

# Both shapes a URL can carry a token in, because both reach a log.
_TOKEN_IN_URL = re.compile(r"https://[^@\s/]+:[^@\s/]+@")
_BARE_TOKEN_IN_URL = re.compile(r"https://[^@\s/:]+@")

# What ANY subprocess needs and nothing beyond: `gates.gate_contract` runs a script out of the repo
# the librarian curates, which must not inherit the GitHub App key or the queue DSN.
SUBPROCESS_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TZ", "LANG",
                       "LC_ALL", "TERM")


def base_env(environ: dict | None = None) -> dict:
    """`SUBPROCESS_BASE_ENV` and nothing else, from `environ` (default `os.environ`)."""
    source = os.environ if environ is None else environ
    return {name: source[name] for name in SUBPROCESS_BASE_ENV if source.get(name)}


def _scrub(text: str) -> str:
    """Strip credential-bearing URLs from text headed for a log. The two-part shape goes first, so
    its own `@` cannot re-match the bare pattern."""
    return _BARE_TOKEN_IN_URL.sub("https://***@", _TOKEN_IN_URL.sub("https://***@", text or ""))


def run(*args: str, cwd: str | None = None, check: bool = True,
        env: dict | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run one git command; raises `GitError` with scrubbed, truncated stderr. `timeout` may be
    None for the WORKER's git but is REQUIRED inside an HTTP request."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              env={**os.environ, **(env or {})}, timeout=timeout)
    except subprocess.TimeoutExpired as ex:
        raise GitError(f"`git {' '.join(_scrub(a) for a in args)}` exceeded its {timeout}s "
                       f"budget — the remote did not answer in time") from ex
    if check and proc.returncode != 0:
        raise GitError(f"`git {' '.join(_scrub(a) for a in args)}` rc={proc.returncode}: "
                       f"{_scrub(proc.stderr).strip()[:STDERR_LIMIT]}")
    return proc


def ensure_repo(repo: str) -> str:
    """Validate the knowledge-repo checkout ONCE at startup. Returns its absolute path."""
    path = os.path.abspath(repo)
    if not os.path.isdir(path):
        raise LibrarianConfigError(f"knowledge repo {path} does not exist — set --repo or "
                                   f"$STIGMERGY_REPO to the checkout the librarian files into")
    proc = run("rev-parse", "--git-dir", cwd=path, check=False)
    if proc.returncode != 0:
        raise LibrarianConfigError(f"knowledge repo {path} is not a git checkout "
                                   f"({_scrub(proc.stderr).strip()[:200]})")
    return path


def origin_url(repo: str) -> str:
    """The checkout's `origin` URL, or `""` when it has no remote."""
    return run("remote", "get-url", "origin", cwd=repo, check=False).stdout.strip()


@dataclass(frozen=True)
class BaseRef:
    """The commit a worktree branches from, and which ref it came from — the `ref` half is
    load-bearing, and every surface that files reports it."""
    sha: str
    ref: str            # "origin/main" or "main"
    remote: bool        # did it come from the remote's tip?

    def describe(self) -> str:
        return f"{self.ref}@{self.sha[:12]}"


def base_ref(repo: str, branch: str, *, timeout_s: float | None = None) -> BaseRef:
    """The remote's tip when there is a remote, the local branch otherwise. Fetching first is
    correctness, not freshness: two captures filed in a row must see each other. A fetch failure is
    not fatal — the push path handles the race.

    `timeout_s` bounds every leg, and is `None` for the worker (a lease, and all night). It is not
    optional for a caller inside an HTTP request: a server-side reader runs this fetch
    inside an AUTHORIZATION check, where an unreachable remote must fail rather than stall.
    """
    if run("remote", cwd=repo, check=False, timeout=timeout_s).stdout.strip():
        fetched = run("fetch", "--quiet", "origin", branch, cwd=repo, check=False,
                      timeout=timeout_s)
        if fetched.returncode != 0:
            log.warning("git fetch failed; basing the worktree on the local %s branch", branch)
        else:
            remote = run("rev-parse", "--verify", "--quiet", f"origin/{branch}",
                         cwd=repo, check=False, timeout=timeout_s)
            if remote.returncode == 0 and remote.stdout.strip():
                return BaseRef(remote.stdout.strip(), f"origin/{branch}", True)
    return BaseRef(run("rev-parse", "--verify", branch, cwd=repo,
                       timeout=timeout_s).stdout.strip(), branch, False)


def base_commit(repo: str, branch: str) -> str:
    """The sha alone, for callers that do not report the ref."""
    return base_ref(repo, branch).sha


def blob_size(repo: str, commit: str, path: str) -> int:
    """The size of one blob AT a commit, or -1, so a ceiling applies BEFORE the content is read."""
    proc = run("cat-file", "-s", f"{commit}:{path}", cwd=repo, check=False)
    try:
        return int(proc.stdout.strip()) if proc.returncode == 0 else -1
    except ValueError:
        return -1


def show(repo: str, commit: str, path: str) -> str:
    """One file's content AT a commit. Raises `GitError` when the commit does not carry it."""
    return run("show", f"{commit}:{path}", cwd=repo).stdout


def worktree_key(repo: str) -> str:
    """Eight hex characters identifying ONE repo checkout; `realpath` first, so a symlinked spelling
    yields one key."""
    return hashlib.sha256(os.path.realpath(repo).encode("utf-8")).hexdigest()[:8]


def reapable(name: str, *, key: str, pid: int | None = None) -> bool:
    """Is this a worktree of THIS repo left behind by a process that is gone? A pure function of a
    name, so the rule is testable without creating a worktree."""
    match = _WORKTREE_NAME_RE.match(name)
    if not match or match.group("key") != key:
        return False
    owner = int(match.group("pid"))
    return owner == (os.getpid() if pid is None else pid) or not _pid_alive(owner)


def _pid_alive(pid: int) -> bool:
    """Is there still a process with this id? `PermissionError` counts as ALIVE — somebody else's
    process is exactly what must not be reaped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def reap(repo: str, root: str = "") -> int:
    """Remove worktrees a crashed run of THIS repo left behind, at startup. Two halves, because a
    crash can leave either git's registration or the directory. Scoped by repo and creating pid,
    which is what makes `once` beside a `run` loop on one repo safe."""
    removed = 0
    key = worktree_key(repo)
    listing = run("worktree", "list", "--porcelain", cwd=repo, check=False).stdout
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):].strip()
        if reapable(os.path.basename(path), key=key):
            run("worktree", "remove", "--force", path, cwd=repo, check=False)
            removed += 1
    run("worktree", "prune", cwd=repo, check=False)

    for parent in {root or tempfile.gettempdir()}:
        if not os.path.isdir(parent):
            continue
        for name in os.listdir(parent):
            if reapable(name, key=key):
                shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
                removed += 1
    return removed


@contextmanager
def ephemeral_worktree(repo: str, commit: str, root: str = ""):
    """A fresh detached worktree at `commit`, removed however the block exits — never reused, since
    a reused worktree carries the previous item's writes into this item's diff. The name carries the
    reap prefix, repo key, pid and a uuid, so no two workers collide and no reap takes a live one."""
    parent = root or tempfile.gettempdir()
    os.makedirs(parent, exist_ok=True)
    path = os.path.join(
        parent,
        f"{WORKTREE_PREFIX}{worktree_key(repo)}-{os.getpid()}-{uuid.uuid4().hex[:12]}")
    try:
        run("worktree", "add", "--detach", "--quiet", path, commit, cwd=repo)
    except GitError as ex:
        raise WorktreeError(f"could not create a worktree at {path}: {ex}") from ex
    try:
        yield path
    finally:
        run("worktree", "remove", "--force", path, cwd=repo, check=False)
        shutil.rmtree(path, ignore_errors=True)
        run("worktree", "prune", cwd=repo, check=False)


# ── the diff: the veto surface ────────────────────────────────────────────────────────────────
# Every diff invocation carries both, and neither is optional. `--text`: without it one NUL byte
# (or a `.gitattributes` carrying `* -diff`) makes git print `Binary files differ` with no content
# lines, which every content gate reads as "nothing to object to". `core.quotePath=false`: quoted,
# a non-ASCII path matches no `wiki/...` prefix test and the linter's findings for it are dropped.
_DIFF_TEXT = ("--text",)
_QUOTE_PATH_OFF = ("-c", "core.quotePath=false")

# Anything else — an executable bit, a symlink, a gitlink — is not a page, whatever its path says.
REGULAR_FILE_MODE = "100644"
ABSENT_MODE = "000000"


@dataclass(frozen=True)
class DiffEntry:
    """One path the agent touched, with its modes, all from ONE `git diff --raw` parse. `blob` is
    the content hash ON DISK when the entry was read, NOT `--raw`'s `new_sha` (all zeros under
    `--intent-to-add`); it is what makes `commit`'s gated comparison content-scoped."""
    status: str
    path: str
    old_mode: str = ""
    new_mode: str = ""
    blob: str = ""

    @property
    def is_regular_file(self) -> bool:
        """Does this end as an ordinary file? A deletion is judged elsewhere."""
        return self.new_mode in (REGULAR_FILE_MODE, ABSENT_MODE, "")


def diff_entries(worktree: str) -> list[DiffEntry]:
    """THE shared base for the diff's shape: everything the agent did, staged or not, tracked or
    not. `add --intent-to-add` first, or an untracked NEW file is invisible to `git diff`;
    `--find-renames=0`, or a rename collapses and hides its delete; `--raw -z` for the modes."""
    run("add", "--intent-to-add", "--all", cwd=worktree)
    out = run(*_QUOTE_PATH_OFF, "diff", "--raw", "-z", "--find-renames=0", "HEAD",
              cwd=worktree).stdout
    # `-z` emits `:<old_mode> <new_mode> <old_sha> <new_sha> <status>\0<path>\0` per entry.
    fields = out.split("\0")
    entries = []
    for index in range(0, len(fields) - 1, 2):
        meta, path = fields[index], fields[index + 1]
        if not meta.startswith(":") or not path:
            continue
        parts = meta[1:].split()
        if len(parts) < 5:
            continue
        entries.append(DiffEntry(status=parts[4].strip()[:1], path=path,
                                 old_mode=parts[0], new_mode=parts[1]))
    blobs = _worktree_blobs(worktree, [entry.path for entry in entries])
    return [replace(entry, blob=blobs.get(entry.path, "")) for entry in entries]


# How much of a diff is worth keeping as TEXT. A repair's diff is stored so a person can read
# afterwards what nobody read before it landed (ADR 044) — and a diff nobody can scroll is not a
# reading either, while a multi-megabyte one in a JSON column is a page that never loads. Clipped
# with a line saying so, never silently truncated: a diff that stops mid-hunk and says nothing
# reads as the whole change.
DIFF_TEXT_CEILING_BYTES = 200_000
DIFF_CLIPPED_NOTE = "\n… diff clipped at {ceiling} bytes; `git show` in the knowledge repo has all of it\n"


def working_diff(worktree: str, *, ceiling: int = DIFF_TEXT_CEILING_BYTES) -> str:
    """The unified diff of everything not yet committed in `worktree`, as text.

    Read BEFORE the commit and never after: `git diff HEAD` answers nothing once the tree is
    clean. `--intent-to-add` for the reason `diff_entries` runs it — an untracked new file is
    invisible to `git diff` without it — and `--no-color`/`--find-renames=0` so the stored text is
    the same shape whatever the reader's git config says.
    """
    run("add", "--intent-to-add", "--all", cwd=worktree)
    out = run(*_QUOTE_PATH_OFF, "diff", "--no-color", "--find-renames=0", "HEAD",
              cwd=worktree).stdout
    if len(out.encode("utf-8")) <= ceiling:
        return out
    return out.encode("utf-8")[:ceiling].decode("utf-8", "ignore") + DIFF_CLIPPED_NOTE.format(
        ceiling=ceiling)


def _worktree_blobs(worktree: str, paths: list[str]) -> dict[str, str]:
    """`git hash-object` per path that is a regular file on disk now. A deletion, symlink or
    directory has no entry, and a path that CHANGES presence changes the pair."""
    present = [p for p in paths
               if os.path.isfile(os.path.join(worktree, p))
               and not os.path.islink(os.path.join(worktree, p))]
    if not present:
        return {}
    # `strict=True`: zipping short would silently drop a page out of the comparison.
    hashes = run("hash-object", "--", *present, cwd=worktree).stdout.split()
    return dict(zip(present, hashes, strict=True))


def tracked_paths(worktree: str) -> frozenset[str]:
    """Every tracked path — the input to `agent.confined_write`'s "must not exist yet" half."""
    out = run("ls-files", "-z", cwd=worktree).stdout
    return frozenset(path for path in out.split("\0") if path)


def changed_files(worktree: str) -> list[tuple[str, str]]:
    """`[(status, path), ...]` — the status-only view of `diff_entries`."""
    return [(entry.status, entry.path) for entry in diff_entries(worktree)]


def diff_text(worktree: str) -> str:
    """The unified diff the secret scanner and the trace gate read; `--text` is load-bearing."""
    return run(*_QUOTE_PATH_OFF, "diff", *_DIFF_TEXT, "--find-renames=0", "HEAD",
               cwd=worktree).stdout


def header_path(line: str, marker: str) -> str:
    """The path out of a `--- a/` or `+++ b/` header line. git appends a TAB after a path containing
    a space; carrying it into the path silently disables `gate_body_rewrite` for that page."""
    return line[len(marker):].removesuffix("\t")


# The new-file half of a hunk header: `@@ -<old> +<start>[,<count>] @@`. `count` is absent for a
# single-line hunk, which means 1.
_HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")

# Huge rather than zero: zero returns the parser to metadata mode and leaves the hunk unscanned.
_UNBOUNDED_HUNK = 1 << 30


def added_lines(worktree: str) -> list[tuple[str, int, str]]:
    """`[(path, line_number, text), ...]` for every ADDED line — the unit the secret and PII gates
    want, since a secret already in the repo is not this capture's doing. Line numbers are in the
    NEW file.

    Hunk-aware as a SECURITY property: a page line spelled `++ b/...` renders like a `+++ b/`
    header, and a prefix parser would re-attribute every later added line to a path of the
    attacker's choosing. Inside a hunk the `@@` counts rule; nothing there is metadata whatever it
    looks like. `split("\\n")`, not `splitlines()`, which also breaks on `\\x0b`/`\\u2028` and would
    desynchronize accounting git itself does not.
    """
    out: list[tuple[str, int, str]] = []
    path, lineno, remaining = "", 0, 0
    for line in diff_text(worktree).split("\n"):
        if remaining <= 0:
            # Between hunks: only the file header and the next `@@` matter.
            if line.startswith("+++ b/"):
                path, lineno = header_path(line, "+++ b/"), 0
            elif line.startswith("@@"):
                match = _HUNK_RE.match(line)
                lineno = int(match.group(1)) - 1 if match else 0
                remaining = int(match.group(2) or 1) if match else _UNBOUNDED_HUNK
            continue
        if line.startswith("+"):
            lineno += 1
            remaining -= 1
            out.append((path, lineno, line[1:]))
        elif line.startswith("-") or line.startswith("\\"):
            continue        # a removal, or `\ No newline at end of file`: no NEW-file line
        else:
            lineno += 1     # context
            remaining -= 1
    return out


# ── the commit and the push ───────────────────────────────────────────────────────────────────
def _identity_env(author_name: str, author_email: str) -> dict:
    """Author AND committer for one invocation; empty when none was given, so git's own behaviour
    stands rather than a commit authored by the empty string."""
    if not author_name or not author_email:
        return {}
    return {"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email}


class GatedDiffChangedError(GitError):
    """The worktree stopped matching the diff the gates approved. NOT repairable by a corrective
    pass: only a gate's own subprocess writes in that window, so this is a worker defect, a linter
    that writes, or interference — and a retry meets the same interference."""


def commit(worktree: str, *, message: str, author_name: str, author_email: str,
          gated_entries=None) -> str:
    """Commit the worktree as the librarian App. Returns the new sha.

    `gated_entries` is the `DiffEntry` list the gates were HANDED, threaded from the caller and
    never re-read here (a second read would put an interloper on both sides). It closes the
    gates-to-commit TOCTOU window with TWO guarantees: the index is reset and exactly the
    enumerated paths staged, and the diff is re-read and compared as (path, content-hash) pairs,
    refusing on any difference — so an in-place rewrite is caught, not just an appearance.

    `gated_entries=None` keeps plain `add --all`; every `None` caller passes through no gates at
    all and owns its whole commit, so a gated caller passing `None` is a bug. Author AND committer
    are per-invocation, so no operator identity leaks into a librarian commit or the reverse.
    """
    if gated_entries is None:
        run("add", "--all", cwd=worktree)
    else:
        expected = {entry.path: entry.blob for entry in gated_entries}
        # The fresh read FIRST, before anything is staged: its `--intent-to-add` is what makes an
        # unexpected UNTRACKED file visible here at all.
        actual = {entry.path: entry.blob for entry in diff_entries(worktree)}
        if actual != expected:
            appeared = sorted(set(actual) - set(expected))
            vanished = sorted(set(expected) - set(actual))
            rewritten = sorted(p for p in set(expected) & set(actual)
                               if expected[p] != actual[p])
            raise GatedDiffChangedError(
                "the worktree no longer matches the diff the gates approved, so nothing was "
                "committed. "
                + (f"appeared after the gates ran: {', '.join(appeared)}. " if appeared else "")
                + (f"gated but now absent: {', '.join(vanished)}. " if vanished else "")
                + (f"changed after the gates ran: {', '.join(rewritten)}. " if rewritten else "")
                + "The only producer that can write here after the gates run is a gate's own "
                  "subprocess, so this is a worker defect, a contract linter that writes, or "
                  "interference with the worktree — not anything about the material that was "
                  "submitted.")
        if expected:
            # `reset` FIRST: the scan above stages a DELETION for real, so without it an
            # interloping deletion rides into the commit. `-A` is what lets a gated path that is
            # itself a deletion be staged at all; `--` so a path cannot become an option.
            run("reset", "--quiet", cwd=worktree)
            run("add", "-A", "--", *sorted(expected), cwd=worktree)
    args = ["commit", "--quiet", "--no-verify", "-m", message]
    run(*args, cwd=worktree, env=_identity_env(author_name, author_email))
    return run("rev-parse", "HEAD", cwd=worktree).stdout.strip()


# Only a GENUINE CONFLICT may fail an item, never a race. The capped backoff stays well inside
# `config.GATE_BUDGET_S` and therefore inside the lease.
PUSH_ATTEMPTS = 6
PUSH_BACKOFF_BASE_S = 0.2
PUSH_BACKOFF_CAP_S = 2.0


def push(worktree: str, *, branch: str, remote_url: str = "", config_env: dict | None = None,
         author_name: str = "", author_email: str = "", attempts: int = PUSH_ATTEMPTS,
         timeout_s: float | None = None, rebase: bool = True) -> str:
    """Push the worktree's HEAD to `branch`, rebasing on a race. **Returns the sha that actually
    landed**: a rebase REWRITES the commit, and the pre-push sha becomes `result_ref` while naming
    an object no reachable history holds. A genuine conflict FAILS the item rather than being
    resolved — a librarian with merge judgment can silently drop a human's edit. `remote_url`
    carries no credential: the token travels in `config_env`, argv being world-readable, and the
    retry `fetch` carries it too or fails differently from the push it retries.

    `rebase=False` turns a rejected push into a clean failure instead: nothing lands, and the
    caller recomputes from the new state. It exists for the non-additive repair kinds, whose
    apply proved its diff against ONE base — a rebase replays that diff onto a base the gates
    never judged, and once it has happened the commit is already on main, so no after-the-fact
    check could refuse it without marking a landed change failed. A filed page has no such
    proof-against-a-base (its gates ran on content, not position), which is why the default
    stays a rebase.

    `timeout_s` bounds EVERY leg of the loop, not only the first push: a retry that reached an
    unbounded fetch would be a budget with a hole in it. `None` is the worker's shape (a lease, and
    all night); `repair.apply` passes its own, because a deletion pushes inside an HTTP request.
    """
    target = remote_url or "origin"
    env = dict(config_env or {})
    for attempt in range(1, attempts + 1):
        proc = run("push", target, f"HEAD:refs/heads/{branch}", cwd=worktree, check=False,
                   env=env, timeout=timeout_s)
        if proc.returncode == 0:
            # Re-read HEAD: a rebase on an earlier iteration already moved it.
            return run("rev-parse", "HEAD", cwd=worktree, timeout=timeout_s).stdout.strip()
        stderr = _scrub(proc.stderr).strip()
        if not rebase:
            raise GitError(
                f"the branch moved while this change was being applied, and this caller never "
                f"rebases — what was approved was judged against the OLD tip, so nothing landed; "
                f"approve it again once it has been re-proposed against the current corpus: "
                f"{stderr[:STDERR_LIMIT]}")
        if attempt == attempts:
            # Distinct wording from the conflict case: an operator must not hunt for an overlap.
            raise GitError(
                f"push to {branch} lost the race {attempts} times in a row and gave up — the branch "
                f"kept moving under it, and nothing about the page conflicted: {stderr[:STDERR_LIMIT]}")
        log.warning("push rejected (attempt %d/%d), rebasing and retrying", attempt, attempts)
        # A rejected push is not always a lost race: an unreachable remote fails the fetch too and
        # the submitter would be told "conflict" about a fault that is not one.
        fetched = run("fetch", target, branch, cwd=worktree, check=False, env=env,
                      timeout=timeout_s)
        if fetched.returncode != 0:
            raise GitError(
                f"could not reach the remote to rebase onto {branch} after a rejected push — this "
                f"is not a conflict with anyone's change; the push and the fetch both failed: "
                f"{_scrub(fetched.stderr or stderr)[:STDERR_LIMIT]}")
        # The SAME per-invocation identity `commit()` uses: a rebase needs a committer, or git
        # takes the operator's own — or refuses, which would misreport as a conflict.
        rebase = run("rebase", "FETCH_HEAD", cwd=worktree, check=False, timeout=timeout_s,
                     env=_identity_env(author_name, author_email))
        if rebase.returncode != 0:
            run("rebase", "--abort", cwd=worktree, check=False, timeout=timeout_s)
            raise GitError(
                "the page conflicts with a change made on the branch since this item started; "
                "the librarian does not resolve conflicts")
        time.sleep(min(PUSH_BACKOFF_CAP_S, PUSH_BACKOFF_BASE_S * (2 ** (attempt - 1))))
