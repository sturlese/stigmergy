"""`stigmergy-librarian-boot` — what the DEPLOYED worker runs before the worker loop.

The librarian is a second process group of the one Fly app, and a container starts with no
knowledge repo in it. Three things have to be true before the first claim: **there is a
checkout** (cloned with the GitHub App's own identity); **the checkout IS the base ref**
(`HEAD == origin/<branch>` — the worker reads the registry and the linter at
`base.sha`, so a checkout at another commit files against inputs nobody can point at); and
**the read path's secrets are not in the worker's environment** (Fly secrets are app-wide, and
the write path must not silently re-couple to the read path's embedder).

Then it **execs** the worker loop, replacing itself, so the container's PID 1 is the process
the platform's SIGTERM reaches — a supervisor shell between them would swallow the signal.

Local runs do not go through here: on a laptop `settings.repo` is a checkout a human maintains,
deliberately at whatever commit they are working on. The composition's `librarian` service runs
the identical entry point, so the container path is exercised locally.
"""
import argparse
import os
import shutil
import sys

from stigmergy.librarian import config, gitcmd, githubapp
from stigmergy.librarian.errors import GitError, LibrarianConfigError, LibrarianError

EXIT_CONFIG = 2         # same value `stigmergy-librarian` uses for "the tool cannot run"

# Secrets that belong to the READ path and must never reach the write path. Stripped rather
# than "not set" — Fly secrets are app-wide, so the exec'd environment is the only place this
# property can be made true. It makes `openai:` filing models unusable on the DEPLOYED worker
# by design: `OPENAI_API_KEY` is also `pydantic_backend.PROVIDER_KEY_ENV`'s credential, and the
# missing-key refusal names this dead end because "export it" is the one fix that cannot work
# there. `EMBED_API_KEY` is the same credential under the embedder's own name — the key for an
# `$EMBED_BASE_URL` host — and is NOT any provider family's key, so stripping it closes no model
# off. The intersection of this tuple with `PROVIDER_KEY_ENV` is pinned by
# `tests/librarian/test_pydantic_preflight.py::test_the_only_provider_key_the_deployed_worker_
# strips_is_the_read_paths_own`, so a second entry cannot silently make another provider family
# undeployable.
READ_PATH_ONLY_ENV = ("OPENAI_API_KEY", "EMBED_API_KEY")

# The credential helper a cloned checkout is pointed at, so the fetch before every claim and the
# push after it both authenticate as the App (see `gitcredential`). `!` is git's own "run this
# as a shell command" form; the name resolves on PATH inside the image.
CREDENTIAL_HELPER = "!stigmergy-librarian-credential"


def credential_scope(url: str) -> str:
    """`https://github.com` from `https://github.com/<owner>/stigmergy.git`, or `""`.

    Scoped to one origin rather than configured globally: a credential offered to every https
    host is a credential a redirect or a mistyped remote can walk off with. `""` for anything
    that is not https — the composition's `git://` remote is anonymous and wants no credential.
    """
    if not url.startswith("https://"):
        return ""
    host = url[len("https://"):].split("/", 1)[0]
    return f"https://{host}" if host else ""


def credential_config_key(url: str) -> str:
    """The git config key that points one origin at the App credential helper."""
    scope = credential_scope(url)
    return f"credential.{scope}.helper" if scope else ""


def configure_credential_helper(repo: str, url: str, env: dict | None = None) -> str:
    """Point this checkout's git at the App helper. Returns the key set, or `""` when none was.

    Two conditions, both necessary: the remote has to be https and the App has to be configured
    (otherwise the helper would be asked for a token it cannot mint, and every fetch would stall
    on a credential prompt nobody is there to answer). Both false is the composition's
    configuration, and it is a supported one.
    """
    key = credential_config_key(url)
    if not key or not githubapp.configured(env):
        return ""
    gitcmd.run("config", key, CREDENTIAL_HELPER, cwd=repo)
    return key


def is_checkout(repo: str) -> bool:
    """Is there already a git checkout at `repo`? The question that decides clone vs fetch."""
    return os.path.isdir(repo) and gitcmd.run(
        "rev-parse", "--git-dir", cwd=repo, check=False).returncode == 0


def same_remote(configured: str, actual: str) -> bool:
    """Do these two git URLs name the same remote? Compared after the two spellings git treats
    as equivalent are normalized away — a trailing `/` and a trailing `.git`.

    Deliberately narrow: anything beyond those two really is a different remote, and a
    comparison generous enough to forgive one would defeat the check it exists to make.
    """
    def normalized(url: str) -> str:
        out = str(url or "").strip().rstrip("/")
        return out[:-4] if out.endswith(".git") else out

    return normalized(configured) == normalized(actual)


def ensure_checkout(repo: str, *, url: str, branch: str, env: dict | None = None) -> None:
    """Clone the knowledge repo, or bring an existing container-local clone up to the remote.

    **Fast-forward only, never a reset**: a machine restarting with its filesystem intact comes
    back with `HEAD` behind `origin/<branch>`, and a genuinely diverged checkout is not silently
    discarded — it falls through to `verify_checkout_at_base`, which refuses it by name.

    **And the clone it updates has to be a clone of the RIGHT repo**: the update path fetches
    the EXISTING checkout's `origin`, so when `$STIGMERGY_LIBRARIAN_REPO_URL` is configured
    (kept on one line so it can be grepped) the two are compared — a checkout of something else
    would otherwise be fast-forwarded, verified against its own remote and filed into, every
    downstream check passing about the wrong repository. With no URL configured there is
    nothing to compare against — the laptop and the composition, both supported.
    """
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
        # The helper cannot be configured in a clone that does not exist yet, so the ONE
        # invocation that has to authenticate before there is a config gets it with `-c`.
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
    """Refuse unless the checkout IS the commit the worktrees will branch from. Returns that ref.

    Three DISTINCT failure modes, because telling them apart is the value of the check: the base
    did not come from the remote (`base.remote` is part of the assertion — a container whose
    credential has been revoked would otherwise pass against its own stale clone); HEAD is not
    that commit; the working tree is dirty (nothing in the fast lane ever writes there — the
    agent works in a throwaway worktree — so a dirty tree is a hand-edit or a bug).
    """
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
    """Clone-or-update, then verify. The whole of what the deployed worker needs before it claims."""
    ensure_checkout(repo, url=url, branch=branch, env=env)
    return verify_checkout_at_base(repo, branch)


# ── the constructed configuration the worker is exec'd with ────────────────────────────────────
def shared_credentials(environ: dict | None = None) -> list[str]:
    """The SURVIVING variable names whose values equal a stripped read-path secret's — the case
    the name-level strip cannot see (audit S2): one OpenRouter key doing both jobs is stripped
    as EMBED_API_KEY and survives as OPENROUTER_API_KEY. That may be exactly what a small
    deployment chose, so `main` says it out loud rather than refusing; giving the embed host its
    own key is what makes the separation real. Names only — no value may travel to a log."""
    source = os.environ if environ is None else environ
    stripped_values = {value for name, value in source.items()
                       if name in READ_PATH_ONLY_ENV and value}
    return sorted(name for name, value in source.items()
                  if name not in READ_PATH_ONLY_ENV and value and value in stripped_values)


def worker_env(environ: dict | None = None) -> dict:
    """The environment the deployed worker actually runs with: everything the app carries,
    MINUS the read path's secrets, PLUS the one fact only this module knows.

    Returned as a plain value on purpose: "the staging worker carries no OpenAI key" is a fact
    about a dict, assertable with no container, no Fly and no key. `REQUIRE_REMOTE_BASE_ENV` is
    the deployed half of `verify_checkout_at_base`, which runs only at startup while a token
    can expire after boot — this process knows it is containerized and the worker does not, so
    the fact is exported rather than inferred, and `processing.process_item` enforces it per
    item. Nothing is set on a laptop, where a local base is the correct answer.
    """
    source = os.environ if environ is None else environ
    kept = {name: value for name, value in source.items() if name not in READ_PATH_ONLY_ENV}
    return {**kept, config.REQUIRE_REMOTE_BASE_ENV: "1"}


def worker_command(worker_args=(), *, environ: dict | None = None) -> list[str]:
    """The argv the loop is exec'd with: `stigmergy-librarian run` plus whatever was passed
    through — the installed entry point when there is one, `python -m` from a bare checkout, so
    this works in the image, in the composition and from a source tree without three answers."""
    found = shutil.which("stigmergy-librarian")
    head = [found] if found else [sys.executable, "-m", "stigmergy.librarian.cli"]
    return [*head, "run", *worker_args]


def worker_launch(worker_args=(), *, environ: dict | None = None) -> tuple[list[str], dict]:
    """`(argv, env)` — the exact command and environment `main` replaces itself with."""
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
    """Prepare, verify, exec. Returns only when something refused, or under `--check-only`.

    `execute` is injectable: a function whose success is "this process is gone" cannot
    otherwise be tested, and what is worth asserting — the argv and the environment — is exactly
    what it is handed.
    """
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
    stripped = sorted(n for n in READ_PATH_ONLY_ENV if n in os.environ)
    if stripped:
        # Said out loud: the strip is a deliberate difference between the app's environment and
        # this process group's, and an operator debugging "why is the embedder not configured"
        # deserves to find the answer in the logs.
        print(f"stigmergy-librarian-boot: not passing {', '.join(stripped)} to the worker — the "
              f"write path does not use the read path's embedder", flush=True)
    shared = shared_credentials()
    if shared:
        print(f"stigmergy-librarian-boot: {', '.join(shared)} carries the same value as a "
              f"stripped read-path secret — a name-level strip cannot separate one credential "
              f"wearing two names; give the embed host its own key to make the separation real",
              flush=True)
    execute(argv_out[0], argv_out, env_out)
    return 0        # unreachable after a successful exec; a test's stub returns here


if __name__ == "__main__":
    raise SystemExit(main())
