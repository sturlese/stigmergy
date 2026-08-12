"""`stigmergy-issue-token <email>`: generate one random per-user bearer token, print the
PLAINTEXT once, plus the sha256 hash for the server-side token store (`$STIGMERGY_TOKEN_STORE` /
`$STIGMERGY_TOKEN_STORE_FILE`, `{"<sha256hex>": "<email>"}`). The plaintext is never written to
disk, a log or a repo; rotate/revoke are ops steps over the same store.
"""
import argparse
import secrets
import sys

from stigmergy.server.identity import hash_token

TOKEN_BYTES = 32   # 256 bits of entropy; secrets.token_urlsafe base64-encodes it (~43 chars)


def issue(email: str) -> tuple[str, str]:
    """A fresh (plaintext, sha256hex) pair; side-effect-free."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="stigmergy-issue-token",
        description="Issue a new per-user HTTP bearer token. Add the "
                    "printed sha256 line to the server's token store; send the token line to "
                    "the user over a channel you trust (it is a bearer credential).")
    parser.add_argument("email", help="the user's email — must match a key in "
                                      "ops/identities.json (knowledge repo)")
    args = parser.parse_args(argv)

    if "@" not in args.email:
        print(f"stigmergy-issue-token: {args.email!r} does not look like an email "
              "(ops/identities.json is keyed by email)", file=sys.stderr)
        return 2

    token, digest = issue(args.email)
    print(f"# token for {args.email} — shown ONCE, copy it now, send it over a trusted channel:")
    print(token)
    print()
    print("# add this line to the server's token store ({sha256hex: email}):")
    print(f'"{digest}": "{args.email}"')
    print()
    print("the plaintext above is never stored by this command — if it is lost, revoke the hash "
          "and issue a new token (operator runbook: rotate/revoke).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
