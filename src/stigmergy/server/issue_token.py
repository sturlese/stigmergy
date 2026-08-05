"""Operator token issuance: a tiny CLI that generates one long random per-user bearer token,
prints the PLAINTEXT once — the operator pastes it straight into the client config
(`docs/reference/server.md`, "HTTP transport") — and the sha256 hex hash the operator adds to the
server-side token store (`$STIGMERGY_TOKEN_STORE` / `$STIGMERGY_TOKEN_STORE_FILE`, shape
`{"<sha256hex>": "<email>"}`).

This command never writes the plaintext to disk, a log, or a repo — after it exits, the
operator's terminal scrollback is the only place it exists, and `git grep` must never find a token
or a hash in any repo. Rotation and revocation are both ops steps over the SAME store: rotate =
issue a new token for the email and drop the old hash; revoke = drop the hash with no replacement
(`docs/reference/operator-runbook.md`).

Run:
    stigmergy-issue-token ana@example.com
"""
import argparse
import secrets
import sys

from stigmergy.server.identity import hash_token

TOKEN_BYTES = 32   # 256 bits of entropy; secrets.token_urlsafe base64-encodes it (~43 chars)


def issue(email: str) -> tuple[str, str]:
    """A fresh (plaintext, sha256hex) pair. Pure and side-effect-free — the CLI below is the only
    place the plaintext is ever printed."""
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
