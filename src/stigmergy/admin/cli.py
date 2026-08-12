"""`stigmergy-admin-token` — mint the console's one credential.

Prints the plaintext ONCE and the `STIGMERGY_ADMIN_TOKEN_HASH=` line to configure the server
with; nothing is written anywhere. Lost token = set a new hash. No email argument: the console
has ONE credential, and `admin_actions.actor` carries attribution per action instead.
"""
import argparse
import secrets

from stigmergy.admin.settings import TOKEN_HASH_ENV
from stigmergy.server.identity import hash_token

TOKEN_BYTES = 32   # 256 bits of entropy


def main(argv=None) -> int:
    argparse.ArgumentParser(
        prog="stigmergy-admin-token",
        description="Issue the admin console's bearer token: prints the plaintext once and the "
                    "hash line to configure the server with. Nothing is stored.").parse_args(argv)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    print("# admin console token — shown ONCE, copy it now; it is what you paste into the login:")
    print(token)
    print()
    print("# configure the server with its hash (locally in .env; staging via `fly secrets set`):")
    print(f"{TOKEN_HASH_ENV}={hash_token(token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
