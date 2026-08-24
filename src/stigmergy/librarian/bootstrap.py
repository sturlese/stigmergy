"""Prepare the deployed knowledge-repository checkout and exec the writer."""
import argparse
import os
import shutil
import sys

from stigmergy.librarian import config, gitcmd, githubapp
from stigmergy.librarian.errors import GitError, LibrarianConfigError, LibrarianError

EXIT_CONFIG = 2

DISALLOWED_PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "EMBED_API_KEY",
)

CREDENTIAL_HELPER = "!stigmergy-librarian-credential"


def credential_scope(url: str) -> str:
    """Return the HTTPS origin used to scope the Git credential helper."""
    if not url.startswith("https://"):
        return ""
    host = url[len("https://"):].split("/", 1)[0]
    return f"https://{host}" if host else ""


def credential_config_key(url: str) -> str:
    """The git config key that points one origin at the App credential helper."""
    scope = credential_scope(url)
    return f"credential.{scope}.helper" if scope else ""


def configure_credential_helper(repo: str, url: str, env: dict | None = None) -> str:
    """Configure the repository-scoped GitHub App helper when available."""
    key = credential_config_key(url)
    if not key or not githubapp.configured(env):
        return ""
    gitcmd.run("config", key, CREDENTIAL_HELPER, cwd=repo)
    return key


def is_checkout(repo: str) -> bool:
    """Return whether ``repo`` is a Git checkout."""
    return os.path.isdir(repo) and gitcmd.run(
        "rev-parse", "--git-dir", cwd=repo, check=False).returncode == 0


def same_remote(configured: str, actual: str) -> bool:
    """Compare Git remote URLs after normalizing trailing slash and ``.git``."""
    def normalized(url: str) -> str:
        out = str(url or "").strip().rstrip("/")
        return out[:-4] if out.endswith(".git") else out

    return normalized(configured) == normalized(actual)


def ensure_checkout(repo: str, *, url: str, branch: str, env: dict | None = None) -> None:
    """Clone the configured repository or fast-forward its existing checkout."""
    if not is_checkout(repo):
        if not url:
            raise LibrarianConfigError(
                f"there is no knowledge-repo checkout at {repo} and no ${config.REPO_URL_ENV} to "
                f"clone one from. The deployed worker starts with an empty container, so it needs "
                f"the repo's git URL (https://github.com/<owner>/<name>.git on staging; the "
                f"composition passes the bare remote's git:// URL)")
        parent = os.path.dirname(os.path.abspath(repo))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # The initial clone must receive the helper before repository config exists.
        pre = ([] if not credential_config_key(url) or not githubapp.configured(env)
               else ["-c", f"{credential_config_key(url)}={CREDENTIAL_HELPER}"])
        gitcmd.run(*pre, "clone", "--quiet", "--branch", branch, url, repo)
        configure_credential_helper(repo, url, env)
        return

    existing = gitcmd.origin_url(repo)
    if url and existing and not same_remote(url, existing):
        raise LibrarianConfigError(
            f"the checkout at {repo} has origin {gitcmd._scrub(existing)}, but ${config.REPO_URL_ENV}"
            f" names {gitcmd._scrub(url)} — this container would fetch, verify and file into a "
            f"different repository than the one it was configured for. Point the volume at the "
            f"right checkout, or remove it so the repo is cloned fresh")
    configure_credential_helper(repo, url or existing, env)
    gitcmd.run("fetch", "--quiet", "origin", branch, cwd=repo, check=False)
    gitcmd.run("merge", "--ff-only", f"origin/{branch}", cwd=repo, check=False)


def verify_checkout_at_base(repo: str, branch: str) -> gitcmd.BaseRef:
    """Require a clean checkout exactly at the remote branch tip."""
    try:
        base = gitcmd.base_ref(repo, branch)
        head = gitcmd.run("rev-parse", "HEAD", cwd=repo).stdout.strip()
        dirty = gitcmd.run("status", "--porcelain", cwd=repo).stdout.strip()
    except GitError as ex:
        raise LibrarianConfigError(
            f"the knowledge-repo checkout at {repo} could not be read ({ex})") from ex

    if not base.remote:
        raise LibrarianConfigError(
            f"the checkout at {repo} has no reachable `origin/{branch}` — its base resolved to the "
            f"local {base.ref} instead, which means the fetch failed (no remote, no network, or a "
            f"revoked App installation). A deployed worker that files against its own stale clone "
            f"files against a commit the remote moved past, so this run refuses instead")
    if head != base.sha:
        raise LibrarianConfigError(
            f"the checkout at {repo} is at {head[:12]} but the worktrees branch from "
            f"{base.describe()} — the deployed worker reads the ACL config, the entity registry "
            f"and the contract linter at that commit, so a checkout sitting somewhere else would "
            f"judge captures with inputs nobody can point at")
    if dirty:
        raise LibrarianConfigError(
            f"the checkout at {repo} has uncommitted changes ({len(dirty.splitlines())} path(s)) — "
            f"nothing in the fast lane writes there (the agent works in a throwaway worktree), so "
            f"this is a hand-edit or a fault, and neither is a state to start filing from")
    return base


def prepare(*, repo: str, url: str, branch: str, env: dict | None = None) -> gitcmd.BaseRef:
    """Prepare and verify the deployed writer checkout."""
    ensure_checkout(repo, url=url, branch=branch, env=env)
    return verify_checkout_at_base(repo, branch)


def worker_env(environ: dict | None = None) -> dict:
    """Return the writer environment without credentials for disallowed providers."""
    source = os.environ if environ is None else environ
    kept = {
        name: value
        for name, value in source.items()
        if name not in DISALLOWED_PROVIDER_ENV
    }
    return {**kept, config.REQUIRE_REMOTE_BASE_ENV: "1"}


def worker_command(worker_args=(), *, environ: dict | None = None) -> list[str]:
    """Build the writer-loop command for installed and source-tree environments."""
    found = shutil.which("stigmergy-librarian")
    head = [found] if found else [sys.executable, "-m", "stigmergy.librarian.cli"]
    return [*head, "run", *worker_args]


def worker_launch(worker_args=(), *, environ: dict | None = None) -> tuple[list[str], dict]:
    """Return the command and environment passed to ``exec``."""
    return worker_command(worker_args, environ=environ), worker_env(environ)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-librarian-boot",
        description="Prepare the deployed worker's knowledge-repo checkout, verify it is at the "
                    "base ref, then exec `stigmergy-librarian run`. Unrecognized arguments are "
                    "passed through to that command.")
    ap.add_argument("--repo", default=None,
                    help=f"where to keep the checkout (default: ${config.REPO_ENV} or "
                         f"{config.REPO_DEFAULT})")
    ap.add_argument("--url", default=None,
                    help=f"the git URL to clone from when there is no checkout yet (default: "
                         f"${config.REPO_URL_ENV})")
    ap.add_argument("--branch", default=None,
                    help="the branch the fast lane commits to (default: main)")
    ap.add_argument("--check-only", action="store_true",
                    help="prepare and verify, print the base ref, and exit without running the "
                         "worker (what a deploy smoke check wants)")
    return ap


def main(argv=None, *, execute=os.execvpe) -> int:
    """Prepare the checkout and replace the process with the writer loop."""
    args, worker_args = build_parser().parse_known_args(argv)
    repo = args.repo or os.environ.get(config.REPO_ENV) or config.REPO_DEFAULT
    url = args.url or os.environ.get(config.REPO_URL_ENV, "")
    branch = args.branch or os.environ.get("STIGMERGY_LIBRARIAN_BRANCH", "main")

    try:
        base = prepare(repo=repo, url=url, branch=branch)
    except LibrarianConfigError as ex:
        print(f"stigmergy-librarian-boot: {ex}", file=sys.stderr)
        return EXIT_CONFIG
    except LibrarianError as ex:
        print(f"stigmergy-librarian-boot: {ex}", file=sys.stderr)
        return EXIT_CONFIG

    print(f"stigmergy-librarian-boot: {repo} is at {base.describe()}", flush=True)
    if args.check_only:
        return 0

    argv_out, env_out = worker_launch(worker_args)
    stripped = sorted(n for n in DISALLOWED_PROVIDER_ENV if n in os.environ)
    if stripped:
        print(
            f"stigmergy-librarian-boot: not passing disallowed provider variables "
            f"{', '.join(stripped)} to the worker",
            flush=True,
        )
    execute(argv_out[0], argv_out, env_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
