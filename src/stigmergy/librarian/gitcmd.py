"""Everything the librarian does with git: ephemeral worktrees, the diff, the commit, the push.

One module on purpose. Worktree creation, the diff the gates veto over, and the push are the
same conversation with the same tool, and splitting them would put three different ideas of how
we invoke git in three files. Same subprocess shape as everywhere else in this repo: binary from
configuration, `capture_output=True, text=True`, non-zero return code raises with a TRUNCATED
stderr.

**The worktree is the blast radius.** Each item gets a fresh `git worktree add --detach` of the
knowledge repo. The agent reads the whole graph through it and writes only inside it; code then
diffs it against its base commit and runs the gates over that diff. It is removed in a
`finally`, and leftovers from a crash are reaped at startup — never reused, because a reused
worktree carries the previous item's uncommitted work into this item's diff.

`--detach` rather than a branch is deliberate: the knowledge repo has `main` checked out in the
operator's own working copy, and `git worktree add` refuses to check out a branch that is already
checked out elsewhere. A detached worktree at the same commit sidesteps that entirely and cannot
move that branch.

**The token never appears in a message, and never in an argument.** The installation token
reaches git through `GIT_CONFIG_*` in the child's environment (`githubapp.push_config`), never as
argv — argv is world-readable through `ps` and `/proc/<pid>/cmdline`, so a token passed
positionally is a token published to every other process on the box. `_scrub` stays regardless:
it is what keeps a credential out of stderr, out of a log line and out of an exception message,
which are different surfaces from argv and still need covering.
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
# The prefix every librarian worktree directory carries. Startup reaping matches on it, so a
# crash's leftovers are identifiable without keeping state anywhere.
WORKTREE_PREFIX = "stigmergy-librarian-"

# `<prefix><repo key>-<creating pid>-<uuid>`. Both middle fields are load-bearing and both were
# absent: the reap swept anything under the temp root whose name merely started with the prefix, and
# the default `worktree_root` IS the shared system temp dir. So two librarians on one machine
# destroyed each other's in-flight worktrees — including `stigmergy-librarian once` (what `make
# librarian-walk` runs) beside a `run` loop, which is a documented pairing. Both halves reap with
# `ignore_errors=True` / `check=False`, so nothing was logged on the victim's side either; its item
# surfaced as a `GitError` and lost.
#
#   * the REPO KEY scopes the sweep to worktrees of the checkout being reaped, so two librarians on
#     different repos cannot see each other's directories at all;
#   * the PID is what distinguishes "a crashed run's leftover" from "a live sibling's worktree" for
#     two librarians on the SAME repo, which no amount of scoping can answer. A pid that is still
#     alive and is not ours is left alone.
#
# A recycled pid is the residual: a leftover directory survives one extra run. That is the safe
# direction to be wrong in — the alternative loses a live item — and the OS temp cleaner collects it.
_WORKTREE_NAME_RE = re.compile(
    rf"^{re.escape(WORKTREE_PREFIX)}(?P<key>[0-9a-f]{{8}})-(?P<pid>\d+)-[0-9a-f]+$")

# Two credential shapes, because a URL can carry a token either way and both reach a log:
# `https://user:token@host` (what `push_url` used to build) and a bare `https://token@host`
# (what a hand-configured remote or a `credential.helper` line looks like).
_TOKEN_IN_URL = re.compile(r"https://[^@\s/]+:[^@\s/]+@")
_BARE_TOKEN_IN_URL = re.compile(r"https://[^@\s/:]+@")

# What ANY subprocess needs to run at all, and nothing beyond it. Here rather than in `agent.py`
# because it is a fact about launching a child process, which is this module's own subject, and
# because two callers need it without depending on each other: `agent.AGENT_ENV_PASSTHROUGH` builds
# on it (adding the CLI's credentials and the proxies), and `gates.gate_contract` uses it ALONE — the
# contract linter is a Python script out of the repo the librarian curates, and such a thing must
# not inherit the GitHub App private key or the queue DSN just because it happens to be our gate.
# Retyping the list in either place is how the two would drift.
SUBPROCESS_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TZ", "LANG",
                       "LC_ALL", "TERM")


def base_env(environ: dict | None = None) -> dict:
    """`SUBPROCESS_BASE_ENV` and nothing else, from `environ` (default `os.environ`)."""
    source = os.environ if environ is None else environ
    return {name: source[name] for name in SUBPROCESS_BASE_ENV if source.get(name)}


def _scrub(text: str) -> str:
    """Remove any credential-bearing URL from text headed for a log or an error.

    Covers `https://user:token@host` and the bare `https://token@host` form. Order matters: the
    two-part shape is substituted first, so its own `@` cannot be re-matched by the bare pattern.
    """
    return _BARE_TOKEN_IN_URL.sub("https://***@", _TOKEN_IN_URL.sub("https://***@", text or ""))


def run(*args: str, cwd: str | None = None, check: bool = True,
        env: dict | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run one git command. Raises `GitError` with scrubbed, truncated stderr on failure.

    `timeout` is None for the WORKER's own git, which runs in a loop with a lease behind it: a
    stall there is recovered by the visibility timeout, and cutting a push off mid-flight would be
    worse than waiting. It is REQUIRED of any caller running git inside an HTTP request — a
    server-driven mint (ADR 030) clones and pushes over the network while a request holds a worker,
    so an unbounded stall there is a pinned worker, not a delayed item."""
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
    """The checkout's `origin` URL, or `""` when it has no remote.

    One reader, two callers: `processing._repo_slug` derives the App's push URL from it, and
    `worker.startup_checks` uses it to decide whether this run is about to write to GitHub — which
    is the condition under which the App credentials stop being optional. Both were asking the
    same question of the same command and only one of them was asking it.
    """
    return run("remote", "get-url", "origin", cwd=repo, check=False).stdout.strip()


@dataclass(frozen=True)
class BaseRef:
    """The commit a worktree branches from, **and which ref it came from**.

    The `ref` half is not decoration. A service working from the canonical remote is correct; a
    service diverging SILENTLY from the human's local branch is not — and that divergence cost a
    walk: a skill commit existed locally and not on the remote, the startup check read the local
    checkout, the run read the worktree (built from `origin/main`), and the item burned both its
    agent attempts discovering the file was not there. So the ref is resolved once, reported by
    every surface that files (`worker.startup_checks`, `stigmergy-librarian once`), and is what the
    startup check reads the skill out of.
    """
    sha: str
    ref: str            # "origin/main" or "main"
    remote: bool        # did it come from the remote's tip?

    def describe(self) -> str:
        return f"{self.ref}@{self.sha[:12]}"


def base_ref(repo: str, branch: str) -> BaseRef:
    """Resolve the commit a new worktree branches from: the remote's tip when there is a remote,
    the local branch otherwise.

    Fetching first matters for correctness, not freshness: two captures filed in a row must see
    each other, and the second one only does if its worktree starts from the commit the first
    one pushed. A fetch failure is not fatal — an offline run (or the bare local remote the e2e
    uses) simply files against the local branch and the push path handles the race.
    """
    if run("remote", cwd=repo, check=False).stdout.strip():
        fetched = run("fetch", "--quiet", "origin", branch, cwd=repo, check=False)
        if fetched.returncode != 0:
            log.warning("git fetch failed; basing the worktree on the local %s branch", branch)
        else:
            remote = run("rev-parse", "--verify", "--quiet", f"origin/{branch}",
                         cwd=repo, check=False)
            if remote.returncode == 0 and remote.stdout.strip():
                return BaseRef(remote.stdout.strip(), f"origin/{branch}", True)
    return BaseRef(run("rev-parse", "--verify", branch, cwd=repo).stdout.strip(), branch, False)


def base_commit(repo: str, branch: str) -> str:
    """The sha alone, for callers that do not report the ref."""
    return base_ref(repo, branch).sha


def blob_size(repo: str, commit: str, path: str) -> int:
    """The size of one blob AT a commit, or -1 when the commit has no such path.

    Exists so a ceiling can be applied before the content is read, exactly as the on-disk path
    does with `os.path.getsize` — a cap applied after reading is decoration.
    """
    proc = run("cat-file", "-s", f"{commit}:{path}", cwd=repo, check=False)
    try:
        return int(proc.stdout.strip()) if proc.returncode == 0 else -1
    except ValueError:
        return -1


def show(repo: str, commit: str, path: str) -> str:
    """One file's content AT a commit. Raises `GitError` when the commit does not carry it."""
    return run("show", f"{commit}:{path}", cwd=repo).stdout


def worktree_key(repo: str) -> str:
    """Eight hex characters identifying ONE repo checkout, embedded in its worktree directory names.

    `realpath` first, so the same checkout reached through a symlinked path (or through `/var` vs
    `/private/var` on darwin) produces one key rather than two.
    """
    return hashlib.sha256(os.path.realpath(repo).encode("utf-8")).hexdigest()[:8]


def reapable(name: str, *, key: str, pid: int | None = None) -> bool:
    """Is this directory name a worktree of THIS repo, left behind by a process that is gone?

    A pure function of a name, so the reaping rule is testable without creating a worktree — and
    without needing a second librarian process to prove the rule that protects one.
    """
    match = _WORKTREE_NAME_RE.match(name)
    if not match or match.group("key") != key:
        return False
    owner = int(match.group("pid"))
    return owner == (os.getpid() if pid is None else pid) or not _pid_alive(owner)


def _pid_alive(pid: int) -> bool:
    """Is there still a process with this id? `PermissionError` counts as alive — it is somebody
    else's process, which is exactly the case that must not be reaped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def reap(repo: str, root: str = "") -> int:
    """Remove worktrees a crashed run of THIS repo left behind. Called ONCE at startup.

    Two halves, because a crash can leave either or both: git's own registration (pruned with
    `worktree prune`, plus an explicit `remove --force` for any still-registered librarian
    worktree) and the directory on disk. Returns how many were removed.

    **Scoped by repo and by creating pid** (`reapable`). Unscoped, this deleted anything under the
    shared system temp root whose name began with the prefix — so `stigmergy-librarian once` in one
    terminal destroyed the in-flight worktree of a `run` loop in another, silently, and that loop's
    item was lost to a `GitError`. Two librarians on the same repo AND the same worktree root are
    still not a supported configuration (see `worker.py`); what this makes safe is the pairing the
    runbook documents, and the cross-repo case entirely.
    """
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
    """A fresh detached worktree at `commit`, removed however the block exits.

    The directory name carries the prefix `reap` matches, THIS repo's key, THIS process's pid and a
    uuid — so two workers (or a worker and a leftover) can never collide on a path, and no worker's
    reap can mistake another's live worktree for a crashed run's leftovers.
    """
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
# Every diff invocation carries both of these, and neither is optional.
#
# `--text` forces git to diff content it would otherwise call binary. Without it a SINGLE NUL
# byte anywhere in a written page makes `git diff` emit `Binary files ... differ` and no `+`/`-`
# lines at all — which the secrets gate, the PII gate, the body-rewrite gate and the additive
# half of the trace gate each read as "nothing to object to". A page carrying a credential was
# committed unexamined that way, and an existing human page could be emptied and replaced
# wholesale with every gate silent. `--text` is also what defeats a `.gitattributes` carrying
# `* -diff`, which reproduces the same blindness permanently and with no NUL byte needed.
#
# `core.quotePath=false` stops git C-quoting non-ASCII paths (`"wiki/notes/caf\303\251.md"`).
# Quoted, a page titled "Café" matches no `wiki/...` prefix test, so it was refused as a
# system fault — and the contract linter's findings for it were silently dropped, because the
# linter reports the unquoted path and the gate's `touched` set held the quoted one.
_DIFF_TEXT = ("--text",)
_QUOTE_PATH_OFF = ("-c", "core.quotePath=false")

# The mode a page is allowed to have. Anything else — an executable bit, a symlink (`120000`), a
# gitlink (`160000`) — is not a page, whatever its path says.
REGULAR_FILE_MODE = "100644"
ABSENT_MODE = "000000"


@dataclass(frozen=True)
class DiffEntry:
    """One path the agent touched, with the modes git recorded for it.

    Status and mode come from ONE `git diff --raw` parse rather than from two invocations, so a
    gate can never judge a path's status against another invocation's idea of its mode.

    `blob` is the content hash of the file AS IT IS ON DISK at the moment this entry was read —
    NOT the `new_sha` column of `git diff --raw`, which is all zeros for anything not staged for
    real, i.e. for every write in these flows (`diff_entries` stages `--intent-to-add`, which
    records the path and not the content; verified against real git). It is what makes the gated
    commit content-scoped rather than merely path-scoped: see `commit`.
    """
    status: str
    path: str
    old_mode: str = ""
    new_mode: str = ""
    blob: str = ""

    @property
    def is_regular_file(self) -> bool:
        """Does this end as an ordinary file? A deletion has no new mode and is judged elsewhere."""
        return self.new_mode in (REGULAR_FILE_MODE, ABSENT_MODE, "")


def diff_entries(worktree: str) -> list[DiffEntry]:
    """THE shared base for the diff's shape: `[DiffEntry, ...]` for everything the agent did,
    staged or not, tracked or not.

    `add --intent-to-add` first so NEW files appear in `diff` at all — an untracked file is
    invisible to `git diff` otherwise, and a gate that cannot see a new file is a gate that
    cannot refuse one. `--find-renames=0`: a rename is a delete plus an add as far as the zone
    gate is concerned, and collapsing them would hide the delete.

    `--raw -z` rather than `--name-status`: it carries the modes (so a page replaced by a symlink
    is visible as the typechange it is) and `-z` removes the path-quoting question entirely.
    """
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


def _worktree_blobs(worktree: str, paths: list[str]) -> dict[str, str]:
    """`git hash-object` for each path that is a regular file on disk right now.

    One invocation for the whole set: `hash-object` accepts many paths and emits one hash per
    line, in order. A path with no hash here (a deletion, a symlink, a directory) simply has no
    entry, and comparing "absent" against "absent" is the correct answer for it — while a path
    that goes from absent to present, or the reverse, changes the pair and is caught.
    """
    present = [p for p in paths
               if os.path.isfile(os.path.join(worktree, p))
               and not os.path.islink(os.path.join(worktree, p))]
    if not present:
        return {}
    # `strict=True`: git emits exactly one hash per path or fails outright (and `run` raises on a
    # non-zero exit), so a length mismatch is a broken assumption, not an input to tolerate — and
    # silently zipping short would drop a page out of the comparison this exists to make.
    hashes = run("hash-object", "--", *present, cwd=worktree).stdout.split()
    return dict(zip(present, hashes, strict=True))


def tracked_paths(worktree: str) -> frozenset[str]:
    """Every path tracked in the worktree's checkout — i.e. every file that ALREADY EXISTS.

    The input to `agent.confined_write`'s "and it must not exist yet" half. `-z` so a page titled
    with a space or an accent needs no quoting decision at all, matching `diff_entries`.
    """
    out = run("ls-files", "-z", cwd=worktree).stdout
    return frozenset(path for path in out.split("\0") if path)


def changed_files(worktree: str) -> list[tuple[str, str]]:
    """`[(status, path), ...]` — the status-only view of `diff_entries`, for callers that do not
    care about modes."""
    return [(entry.status, entry.path) for entry in diff_entries(worktree)]


def diff_text(worktree: str) -> str:
    """The unified diff the secret scanner and the trace gate read.

    `--text` is load-bearing, not cosmetic — see `_DIFF_TEXT` above. Without it this function
    returns a diff with no content lines for exactly the pages an attacker controls.
    """
    return run(*_QUOTE_PATH_OFF, "diff", *_DIFF_TEXT, "--find-renames=0", "HEAD",
               cwd=worktree).stdout


def header_path(line: str, marker: str) -> str:
    """The path out of a `--- a/` or `+++ b/` unified-diff header line.

    **git appends a TAB after the path when the path contains a space**, so the header stays
    unambiguous. That tab was being carried into the path, and since essentially every page here
    is titled like "Existing Note.md", the paths parsed out of the diff text matched none of the
    paths parsed out of `--raw` — which silently disabled `gate_body_rewrite` for exactly those
    pages, i.e. almost all of them. One parser, in one place, so the two can no longer disagree.
    """
    return line[len(marker):].removesuffix("\t")


# The new-file half of a hunk header: `@@ -<old> +<start>[,<count>] @@`. `count` is absent for a
# single-line hunk, which means 1.
_HUNK_RE = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")

# What an UNPARSEABLE `@@` line falls back to. Deliberately huge rather than zero: zero would put
# the parser back in metadata mode for the whole hunk, so every added line in it would go unscanned
# — exactly the blind spot `added_lines` was rewritten to remove. Over-attributing (a later
# metadata line read as content) is the safe direction: it is a false positive a human reads, not a
# gate that went quiet.
_UNBOUNDED_HUNK = 1 << 30


def added_lines(worktree: str) -> list[tuple[str, int, str]]:
    """`[(path, line_number, text), ...]` for every ADDED line in the diff.

    The unit the secret and PII gates actually want: a secret that was already in the repo is
    not this capture's doing, and flagging it would refuse someone's work for a pre-existing
    condition. Line numbers are in the NEW file, so the refusal can name a real location.

    **Hunk-aware on purpose, and this is a security property, not tidiness.** git prefixes every
    content line with a single `+`/`-`, so a page line spelled `++ b/wiki/notes/X.md` renders
    EXACTLY like a `+++ b/` file header. A prefix parser read it as one and re-attributed every
    later added line to a path of the attacker's choosing — which took `gate_secrets`, `gate_pii`
    out of the run at once (they scope themselves to the paths the diff claims),
    while the `unscanned-diff` backstop stayed quiet because one innocuous line before the forged
    header is enough to keep the list non-empty. The same parser dropped any added line beginning
    with `++` entirely, so that one was never scanned at all.

    Inside a hunk, the `@@` counts say how many lines belong to it; nothing in there is metadata
    whatever it looks like. Only `+`, context and the `\\ No newline` marker can appear, and only
    the first two consume a NEW-file line — a `-` line consumes none.

    **`split("\\n")`, not `splitlines()`**: `str.splitlines` also breaks on `\\x0b`, `\\x0c`,
    `\\x1c`-`\\x1e`, `\\x85`, `\\u2028` and `\\u2029`, none of which git treats as a line ending.
    A page carrying one of those would split one rendered diff line into two, desynchronize the
    hunk accounting, and hand back the forgery this parser exists to refuse — a `\\u2028` followed
    by `@@ …` is a hunk header no content line could otherwise spell.
    """
    out: list[tuple[str, int, str]] = []
    path, lineno, remaining = "", 0, 0
    for line in diff_text(worktree).split("\n"):
        if remaining <= 0:
            # Between hunks: the only things worth reading are the file header and the next `@@`.
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
    """Author AND committer for one git invocation. Empty when no identity was given, so a caller
    that has none keeps git's own behaviour rather than committing as the empty string."""
    if not author_name or not author_email:
        return {}
    return {"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email}


class GatedDiffChangedError(GitError):
    """The worktree stopped matching the diff the gates approved, between the gates and the commit.

    Deliberately its own class rather than a bare `GitError`, and deliberately NOT repairable by a
    corrective agent pass: the only producer that can write here during the window is a gate's own
    subprocess — `gate_contract` runs the knowledge repo's `.claude/tools/stigmergy_lint.py` with
    the worktree path as an argument — so this is a worker defect, a linter that writes, or
    outside interference. None of the three is something the agent could draft differently, which
    is the same reasoning `zone/meeting-edit-refused` is terminal for. An agent retry over a
    worktree somebody else is writing to would be a second run against the same interference.
    """


def commit(worktree: str, *, message: str, author_name: str, author_email: str,
          gated_entries=None) -> str:
    """Commit the worktree as the librarian App. Returns the new sha.

    **`gated_entries` closes the TOCTOU window between the gates and the commit.** A gated caller
    runs the eight gates over `diff_entries(worktree)` and then arrives here. Staging the worktree
    with `add --all` would commit *whatever was on disk at commit time*, not what the gates had
    judged — and in between, the gate subprocesses (the contract linter, gitleaks) run with the
    worktree on disk, so anything written into it in that window would be committed **ungated**.
    Eight code gates over a diff are worth exactly what the guarantee that the diff cannot change
    afterwards is worth.

    Passing `gated_entries` — the `DiffEntry` list the gates actually judged — does two things,
    and the second is the one that matters:

    1. **Stages exactly those paths** — the index is reset first and then only the enumerated
       paths are added, so an interloping write cannot ride along even if everything else fails.
       (The naive `add -- <paths>` this started as did NOT hold that for deletions; see the
       implementation comment for what real git actually does with `--intent-to-add`.)
    2. **Re-reads the diff and refuses on any difference** (`GatedDiffChangedError`). Excluding
       an unexpected file silently would leave a write nobody authorized sitting in an ephemeral
       worktree, unreported — and the fact that something wrote there is more important than the
       file. A refusal is what tells an operator.

    Both, rather than either — they answer different questions: (1) is what the commit contains,
    (2) is whether anyone should be told. **Narrowing the window is not an option**: a race made
    narrower is still a race, and this one is reachable by any gate that shells out — which
    `gate_contract` does, into a script the knowledge repo supplies.

    **The comparison is (path, content), not path alone.** It used to be a set of paths, and an
    in-place rewrite of an ALREADY-GATED page therefore landed ungated: same path, same status,
    equal sets, different bytes. That is not a hypothetical window — `gate_contract` is 7th of 8,
    so every content gate has already read the files by the time it hands the worktree to a
    repo-supplied linter. `DiffEntry.blob` is the content hash read at gate time, and comparing
    the pairs is what makes "the diff the gates approved is the diff that lands" a statement
    about bytes.

    **What `gated_entries` must be, and the mistake to not repeat**: the entries the gates were
    HANDED, threaded from the caller — never a fresh `diff_entries()` taken at commit time. A
    second read happens after the gate subprocesses have run, so an interloper would be on both
    sides of the comparison and the check would pass on exactly the input it exists to catch.

    `gated_entries=None` keeps the plain `add --all` behaviour, and every `None` caller passes
    through **no gates at all**: view regeneration (`views.writer`) and entity birth
    (`entities.clone`), both of which own their whole commit by design. A gated caller passing
    `None` is a bug — both of them pass their entries.

    Author AND committer are set per-invocation rather than in the worktree's config, so nothing
    about the operator's own git identity leaks into a librarian commit and nothing the
    librarian sets survives into the operator's checkout.
    """
    if gated_entries is None:
        run("add", "--all", cwd=worktree)
    else:
        expected = {entry.path: entry.blob for entry in gated_entries}
        # The fresh read FIRST, before anything is staged: `diff_entries` is the same function the
        # gates were handed, so this compares like with like. It also `--intent-to-add`s new files,
        # which is what makes an unexpected UNTRACKED file visible here at all — the exact shape of
        # write this window admits.
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
            # `reset` FIRST, then `add -A -- <paths>`. Both halves are load-bearing, and the
            # obvious version of this is wrong — verified against real git rather than reasoned
            # about:
            #
            #  * `diff_entries` above runs `add --intent-to-add --all`, and for a DELETION that
            #    stages it *for real* — not as an intent. So `add -- <gated paths>` alone left an
            #    interloping deletion sitting in the index, and `git commit` carried it. Measured:
            #    a commit named `A added.md` and `D victim.md` where only the first was gated.
            #    `reset` clears the index back to HEAD so nothing survives from that scan.
            #  * `-A` (not a bare `add`) is what lets a gated path that is itself a DELETION be
            #    staged at all: `git add -- <deleted path>` fails outright with
            #    `fatal: pathspec ... did not match any files` (rc 128). Unreachable through the
            #    lanes today — `gate_zone` vetoes every `D` entry — but this is a general
            #    primitive, and a primitive that errors on a legitimate input is a trap for the
            #    next caller.
            #
            # `--` so a path that looks like an option cannot become one. Verified against real git
            # that `[`, `]`, `*`, spaces and accents all stage literally under `add -A --`.
            run("reset", "--quiet", cwd=worktree)
            run("add", "-A", "--", *sorted(expected), cwd=worktree)
    args = ["commit", "--quiet", "--no-verify", "-m", message]
    run(*args, cwd=worktree, env=_identity_env(author_name, author_email))
    return run("rev-parse", "HEAD", cwd=worktree).stdout.strip()


# How many times a push may lose the race before the item is given up on, and how long it waits in
# between. **Six with real backoff, not three with a flat 0.2s** — a fix the docker e2e forced.
# The contract is "`pull --rebase` before push, retry on a race, and a GENUINE CONFLICT fails the
# item": a race is explicitly not supposed to fail anything. With three attempts 0.2s and 0.4s
# apart, a branch that kept moving exhausted the budget and failed captures whose pages had no
# conflict with anything — the race failing the item, which is what the retry exists to prevent.
# The backoff doubles (0.2, 0.4, 0.8, 1.6, 2.0s capped) for ~5s of patience, which is well inside
# `config.GATE_BUDGET_S` and therefore inside the lease.
PUSH_ATTEMPTS = 6
PUSH_BACKOFF_BASE_S = 0.2
PUSH_BACKOFF_CAP_S = 2.0


def push(worktree: str, *, branch: str, remote_url: str = "", config_env: dict | None = None,
         author_name: str = "", author_email: str = "", attempts: int = PUSH_ATTEMPTS) -> str:
    """Push the worktree's HEAD to `branch`, rebasing onto the remote on a race. **Returns the sha
    that actually landed**, which is not always the one that was committed.

    That return value is a fix, found by the docker e2e. A rebase REWRITES the commit, so after a
    non-fast-forward retry the local sha `commit()` handed back names an object that exists in no
    reachable history — and that string is `result_ref`, the submitter's report, and the sha
    `git show <sha>` has to be able to display. With two workers racing, three of twelve pages
    were reported at shas the remote had never heard of; `git show` on them said `bad object`.

    Reachable with ONE worker too: git is a shared mutable surface, and a human editing the repo
    by hand while the librarian pushes to the same branch makes a non-fast-forward expected rather
    than exceptional. So: fetch, rebase, retry. A GENUINE conflict fails the item rather than
    being resolved — nothing gives the librarian merge judgment, and a librarian that resolves
    conflicts is a librarian that can silently drop a human's edit.

    **`remote_url` carries no credential.** It is a plain `https://github.com/<slug>.git`. The
    installation token travels in `config_env` (`githubapp.push_config`) as a `GIT_CONFIG_*`
    triple setting an `http.<url>.extraheader`, which reaches git through the child's ENVIRONMENT
    rather than through argv. argv is readable by every process on the machine via `ps` and
    `/proc/<pid>/cmdline`; a token passed positionally is a token published, and the librarian
    runs on a laptop that also runs other agents. The environment of another user's process is
    not readable the same way, and nothing is written to disk either way.

    Both the push and the retry `fetch` carry `config_env` — a fetch that authenticated
    differently from the push it retries would fail in a way no message could explain.
    """
    target = remote_url or "origin"
    env = dict(config_env or {})
    for attempt in range(1, attempts + 1):
        proc = run("push", target, f"HEAD:refs/heads/{branch}", cwd=worktree, check=False,
                   env=env)
        if proc.returncode == 0:
            # Re-read HEAD rather than trusting the caller's pre-push sha: a rebase on an earlier
            # iteration has already moved it, and this is the value that becomes `result_ref`.
            return run("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        stderr = _scrub(proc.stderr).strip()
        if attempt == attempts:
            # Distinct wording from the conflict case below, because it is a distinct fault with a
            # distinct fix: nothing conflicted, the branch simply never stopped moving. An operator
            # reading "conflicts with a change" here would go looking for an overlap that is not
            # there, and the answer is contention (or a hung push), not a merge.
            raise GitError(
                f"push to {branch} lost the race {attempts} times in a row and gave up — the branch "
                f"kept moving under it, and nothing about the page conflicted: {stderr[:STDERR_LIMIT]}")
        log.warning("push rejected (attempt %d/%d), rebasing and retrying", attempt, attempts)
        run("fetch", target, branch, cwd=worktree, check=False, env=env)
        # The SAME per-invocation identity `commit()` uses, and for the same reason: a rebase
        # REWRITES commits, so it needs a committer and git will take one from wherever it can find
        # it. With no identity passed here, a rebase on an operator's laptop silently stamps the
        # OPERATOR as committer — precisely what `commit()`'s docstring promises never happens —
        # and on a machine with no global git config at all (a CI runner, the Fly container) git
        # refuses outright, which this function then reported as "the page conflicts with a change
        # made on the branch". That message sent readers looking for an overlap that was not there.
        # Found by CI, which is the one environment that had no global identity to fall back on.
        rebase = run("rebase", "FETCH_HEAD", cwd=worktree, check=False, env=_identity_env(
            author_name, author_email))
        if rebase.returncode != 0:
            run("rebase", "--abort", cwd=worktree, check=False)
            raise GitError(
                "the page conflicts with a change made on the branch since this item started; "
                "the librarian does not resolve conflicts")
        time.sleep(min(PUSH_BACKOFF_CAP_S, PUSH_BACKOFF_BASE_S * (2 ** (attempt - 1))))
