"""Git operations used by the serialized knowledge writer."""
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
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

def _scrub(text: str) -> str:
    """Strip credentials from URLs before they reach logs or errors."""
    return _BARE_TOKEN_IN_URL.sub("https://***@", _TOKEN_IN_URL.sub("https://***@", text or ""))


def run(*args: str, cwd: str | None = None, check: bool = True,
        env: dict | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run Git and raise with bounded credential-scrubbed stderr."""
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
    """A resolved branch base and its local or remote origin."""
    sha: str
    ref: str            # "origin/main" or "main"
    remote: bool        # did it come from the remote's tip?

    def describe(self) -> str:
        return f"{self.ref}@{self.sha[:12]}"


def base_ref(repo: str, branch: str, *, timeout_s: float | None = None) -> BaseRef:
    """Resolve the fetched remote tip, falling back to the local branch."""
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


def show(repo: str, commit: str, path: str) -> str:
    """One file's content AT a commit. Raises `GitError` when the commit does not carry it."""
    return run("show", f"{commit}:{path}", cwd=repo).stdout


def worktree_key(repo: str) -> str:
    """Return the stable key used to scope crash cleanup to one checkout."""
    return hashlib.sha256(os.path.realpath(repo).encode("utf-8")).hexdigest()[:8]


def reapable(name: str, *, key: str, pid: int | None = None) -> bool:
    """Return whether a named worktree belongs to this checkout and a dead owner."""
    match = _WORKTREE_NAME_RE.match(name)
    if not match or match.group("key") != key:
        return False
    owner = int(match.group("pid"))
    return owner == (os.getpid() if pid is None else pid) or not _pid_alive(owner)


def _pid_alive(pid: int) -> bool:
    """Treat an inaccessible process as alive so it is never reaped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def reap(repo: str, root: str = "") -> int:
    """Remove registered and unregistered worktrees left by dead writers."""
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
    """Create an isolated detached worktree and always remove it."""
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


_QUOTE_PATH_OFF = ("-c", "core.quotePath=false")

# Anything else — an executable bit, a symlink, a gitlink — is not a page, whatever its path says.
REGULAR_FILE_MODE = "100644"
ABSENT_MODE = "000000"


@dataclass(frozen=True)
class DiffEntry:
    """One changed path and its on-disk content hash at gate time."""
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
    """Return every changed path without collapsing renames or quoting names."""
    run("add", "--intent-to-add", "--all", cwd=worktree)
    out = run(*_QUOTE_PATH_OFF, "diff", "--raw", "-z", "--no-renames", "HEAD",
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


def _identity_env(author_name: str, author_email: str) -> dict:
    """Author AND committer for one invocation; empty when none was given, so git's own behaviour
    stands rather than a commit authored by the empty string."""
    if not author_name or not author_email:
        return {}
    return {"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email}


class GatedDiffChangedError(GitError):
    """The worktree changed after its candidate diff passed the gates."""


def commit(worktree: str, *, message: str, author_name: str, author_email: str,
          gated_entries=None) -> str:
    """Commit all changes or the exact path-and-content snapshot accepted by the gates."""
    if gated_entries is None:
        run("add", "--all", cwd=worktree)
    else:
        expected = {entry.path: entry.blob for entry in gated_entries}
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
            )
        if expected:
            run("reset", "--quiet", cwd=worktree)
            run("add", "-A", "--", *sorted(expected), cwd=worktree)
    args = ["commit", "--quiet", "--no-verify", "-m", message]
    run(*args, cwd=worktree, env=_identity_env(author_name, author_email))
    return run("rev-parse", "HEAD", cwd=worktree).stdout.strip()


def push(
    worktree: str,
    *,
    branch: str,
    remote_url: str = "",
    config_env: dict | None = None,
) -> str:
    """Push the gated commit without rebasing it onto unverified state."""
    target = remote_url or "origin"
    env = dict(config_env or {})
    proc = run(
        "push",
        target,
        f"HEAD:refs/heads/{branch}",
        cwd=worktree,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise GitError(
            f"push to {branch} was rejected; the gated commit was not rebased and nothing landed: "
            f"{_scrub(proc.stderr).strip()[:STDERR_LIMIT]}"
        )
    return run("rev-parse", "HEAD", cwd=worktree).stdout.strip()
