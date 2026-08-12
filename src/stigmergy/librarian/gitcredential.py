"""`stigmergy-librarian-credential` — a git credential helper backed by the GitHub App.

The worker fetches `origin/<branch>` before every item (`gitcmd.base_ref`), and in the
container an unauthenticated fetch against a private repo does not fail loudly — `base_ref`
warns and files against the clone-time snapshot forever, the exact staleness the fetch exists
to prevent. A helper rather than a persisted token: an installation token lives about an hour
while the worker runs for days, and a token in `.git/config` or a remote URL is a credential at
rest in the container. Git asks exactly when it needs one; this mints a fresh token, prints it
down a pipe, and exits — nothing reaches disk and nothing reaches argv.

`librarian.bootstrap` points a clone at this, and only when the App is configured and the
remote is https; the composition's bare `git://` remote is anonymous and supported.
"""
import sys

from stigmergy.librarian import githubapp
from stigmergy.librarian.errors import LibrarianError

# git's own protocol: the helper reads `key=value` lines on stdin and, for `get`, writes back
# the fields it can supply. `x-access-token` as the username with the installation token as the
# password is GitHub's documented form for App authentication over https — the same pair
# `githubapp.push_config` base64-encodes into its `Authorization: Basic` header.
USERNAME = "x-access-token"

# The three operations git may ask for. Only `get` does anything: `store` and `erase` are about
# a credential CACHE, and this helper deliberately has none — every answer is minted fresh.
GET, STORE, ERASE = "get", "store", "erase"

# The ONE origin this helper will mint for. `bootstrap.credential_scope` already scopes it in
# git's config, but that is a property of the CALLER — this is a console script on `PATH` inside
# the worker image, and anything running as the worker's uid can execute it directly and read a
# fresh `contents:write` token off stdout. Checking the request means an unscoped or mis-scoped
# invocation gets nothing.
ALLOWED_HOST = "github.com"
ALLOWED_PROTOCOL = "https"


def _request_fields(text: str) -> dict[str, str]:
    """git's credential protocol: `key=value` lines, terminated by a blank line. Unknown keys
    are ignored rather than refused — git is free to add fields, and a helper that failed on one
    would break on a git upgrade."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def credential_lines(env: dict | None = None) -> list[str]:
    """The two lines git expects for a `get`, with a freshly minted installation token.

    `env` is injectable so the whole path — App id, private key, JWT, the token exchange — can
    be driven in a test against `githubapp.installation_token`'s own `opener` stub.
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
    request = "" if sys.stdin.closed else sys.stdin.read()
    fields = _request_fields(request)
    host, protocol = fields.get("host", ""), fields.get("protocol", "")
    if host != ALLOWED_HOST or protocol != ALLOWED_PROTOCOL:
        # Never name the token, never hint at what would have been accepted beyond the one
        # origin this helper serves — and print nothing on stdout, which git would take AS the
        # credential.
        print(f"stigmergy-librarian-credential: refusing to mint for {protocol or '(none)'}://"
              f"{host or '(none)'} — this helper answers for {ALLOWED_PROTOCOL}://{ALLOWED_HOST} "
              f"only", file=sys.stderr)
        return 1
    try:
        lines = credential_lines()
    except LibrarianError as ex:
        print(f"stigmergy-librarian-credential: {ex}", file=sys.stderr)
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
