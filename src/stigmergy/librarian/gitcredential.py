"""`stigmergy-librarian-credential` — a git credential helper backed by the GitHub App.

**Why the worker needs one at all.** `gitcmd.base_ref` fetches `origin/<branch>` before every
item, which is what lets a capture see the entity a steward approved two minutes ago without a
restart. On a laptop that fetch authenticates with whatever the operator's own git is configured
with; in the container there is no such thing, and against a private repo an unauthenticated
fetch does not fail loudly — `base_ref` logs a warning and files against the clone-time snapshot
forever, which is the exact staleness the fetch exists to prevent.

**Why a helper rather than a token in the URL or in the config.** An installation token lives
about an hour and the worker runs for days, so anything persisted goes stale; and a token written
into `.git/config` or into a remote URL is a credential at rest in the container, next to the repo
the agent curates. The helper is the opposite shape: git asks for a credential exactly when it
needs one, this mints a fresh token, prints it down a pipe, and exits. Nothing reaches disk and
nothing reaches argv (which is world-readable through `ps` — the same reasoning that moved the
push's token out of the command line and into `githubapp.push_config`).

`librarian.bootstrap` is what points a clone at this, and only when the App is configured and the
remote is https. The composition's bare `git://` remote is anonymous and wants no credential at
all, which is a supported configuration (see `githubapp`'s docstring).
"""
import sys

from stigmergy.librarian import githubapp
from stigmergy.librarian.errors import LibrarianError

# git's own protocol: the helper reads `key=value` lines on stdin and, for `get`, writes back the
# fields it can supply. `x-access-token` as the username with the installation token as the
# password is GitHub's documented form for App authentication over https — the same pair
# `githubapp.push_config` base64-encodes into its `Authorization: Basic` header.
USERNAME = "x-access-token"

# The three operations git may ask for. Only `get` does anything: `store` and `erase` are about a
# credential CACHE, and this helper deliberately has none — every answer is minted fresh.
GET, STORE, ERASE = "get", "store", "erase"


def credential_lines(env: dict | None = None) -> list[str]:
    """The two lines git expects for a `get`, with a freshly minted installation token.

    `env` is injectable for the same reason every other seam in this package takes one: the whole
    path — App id, private key, JWT, the token exchange — can then be driven in a test against
    `githubapp.installation_token`'s own `opener` stub, with no GitHub and no environment.
    """
    return [f"username={USERNAME}", f"password={githubapp.installation_token(env)}"]


def main(argv=None) -> int:
    """Answer one `git credential` request on stdin/stdout.

    Exit non-zero on failure and print NOTHING on stdout: git treats a helper's output as the
    credential, so a diagnostic printed to the wrong stream would be handed to a remote as a
    password. The reason goes to stderr, where the operator reads it in `fly logs`.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    operation = argv[0] if argv else ""
    if operation != GET:
        # `store`/`erase` (and anything git grows later) are no-ops rather than errors: there is
        # nothing cached to write or forget, and a non-zero exit here would make git report a
        # failure for a request that was satisfied.
        return 0
    if not sys.stdin.closed:
        sys.stdin.read()        # drain git's request; the fields in it do not change the answer
    try:
        lines = credential_lines()
    except LibrarianError as ex:
        print(f"stigmergy-librarian-credential: {ex}", file=sys.stderr)
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
