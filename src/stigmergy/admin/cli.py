"""`stigmergy-admin`: bootstrap the first platform operator or a workspace's first administrator,
register a workspace, import a legacy file roster, or mint a break-glass token. Every command needs
direct database access; a token is printed once and never stored in plaintext."""
import argparse
import os
import sys

from stigmergy.index import store
from stigmergy.kernel import tenancy
from stigmergy.server import identity, platform, roster
from stigmergy.server.errors import IdentityError
from stigmergy.server.settings import TENANT_ENV

ACTOR = "stigmergy-admin"


def _print_token(grant: roster.TokenGrant, *, opens: str) -> None:
    expiry = f"; it expires {grant.expires_at[:10]}" if grant.expires_at else ""
    print(f"# token for {grant.subject}, shown once{expiry}; it opens {opens}:")
    print(grant.token)


def _bootstrap(conn, args) -> int:
    _print_token(roster.bootstrap_admin(conn, args.email, args.name, actor=ACTOR),
                 opens="MCP and the backoffice")
    return 0


def _mint(conn, args) -> int:
    try:
        who = roster.principal(conn, args.email)
    except IdentityError:
        who = None
    if who is None or not who.administers:
        print("stigmergy-admin: mint only issues tokens to an enabled administrator",
              file=sys.stderr)
        return 2
    _print_token(roster.issue_token(conn, who.subject, args.label, actor=ACTOR),
                 opens="MCP and the backoffice")
    return 0


def _import(conn, args) -> int:
    with open(args.identities, encoding="utf-8") as file:
        principals = identity.principals_from_text(file.read(), origin=args.identities)
    with open(args.channels, encoding="utf-8") as file:
        channels = identity.channel_map_from_text(file.read(), origin=args.channels)
    raw_store = os.environ.get(identity.TOKEN_STORE_ENV) or None
    token_store = identity.load_token_store(raw_store, args.token_store_file)
    summary = roster.import_roster(
        conn, principals=principals, channels=channels, token_store=token_store,
        admins=set(args.admin), agents=set(args.agent), actor=ACTOR)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ops_file_snapshot")
    print(f"imported {summary['principals']} identities, {summary['groups']} groups, "
          f"{summary['tokens']} tokens, and {summary['channels']} Slack channels")
    return 0


def _tenant_create(conn, args) -> int:
    created = platform.create_tenant(conn, slug=args.slug, name=args.name, repo=args.repo,
                                     actor=ACTOR, status=args.status,
                                     slack_team_id=args.slack_team_id or None)
    platform.record_action(conn, actor=ACTOR, action="tenant.create", tenant_slug=created.slug,
                           outcome="ok", args={"repo": created.repo, "status": created.status})
    print(f"workspace {created.slug} ({created.id}) is {created.status}")
    return 0


def _operator_bootstrap(conn, args) -> int:
    _print_token(platform.bootstrap_operator(conn, args.email, args.name, actor=ACTOR),
                 opens="the master console")
    return 0


def _operator_mint(conn, args) -> int:
    _print_token(platform.mint_operator_token(conn, args.email, actor=ACTOR),
                 opens="the master console")
    return 0


_TENANT_COMMANDS = {"bootstrap": _bootstrap, "mint": _mint, "import": _import}
_PLATFORM_COMMANDS = {("tenant", "create"): _tenant_create,
                      ("operator", "bootstrap"): _operator_bootstrap,
                      ("operator", "mint"): _operator_mint}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stigmergy-admin", description=__doc__)
    parser.add_argument("--tenant", default=None,
                        help=f"workspace slug for roster commands (fallback: ${TENANT_ENV}; the "
                             "only active workspace when there is one)")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="create a workspace's first administrator")
    bootstrap.add_argument("email")
    bootstrap.add_argument("--name", required=True, help="the administrator's display name")
    mint = commands.add_parser("mint",
                               help="issue a new token to an existing administrator")
    mint.add_argument("email")
    mint.add_argument("--label", default="break-glass")
    legacy = commands.add_parser("import", help="load a legacy file roster once")
    legacy.add_argument("--identities", required=True)
    legacy.add_argument("--channels", required=True)
    legacy.add_argument("--token-store-file", default=None,
                        help=f"token digests, when ${identity.TOKEN_STORE_ENV} is not set")
    legacy.add_argument("--admin", action="append", default=[], required=True)
    legacy.add_argument("--agent", action="append", default=[])

    tenant = commands.add_parser("tenant", help="register a workspace")
    tenant_commands = tenant.add_subparsers(dest="action", required=True)
    create = tenant_commands.add_parser("create", help="register a workspace and its repository")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--repo", required=True, help="owner/name of its knowledge repository")
    create.add_argument("--status", choices=tenancy.STATUSES, default="provisioning",
                        help="active for a repository that already exists")
    create.add_argument("--slack-team-id", default="")

    operator = commands.add_parser("operator", help="platform operators")
    operator_commands = operator.add_subparsers(dest="action", required=True)
    first = operator_commands.add_parser("bootstrap", help="create the first operator")
    first.add_argument("email")
    first.add_argument("--name", required=True)
    again = operator_commands.add_parser("mint", help="issue a new token to an operator")
    again.add_argument("email")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    with store.connect() as conn:
        platform.ensure_platform_schema(conn)
        try:
            platform_command = _PLATFORM_COMMANDS.get((args.command, getattr(args, "action", None)))
            if platform_command is not None:
                return platform_command(conn, args)
            chosen = platform.resolve_tenant(conn, args.tenant or os.environ.get(TENANT_ENV))
            tenancy.set_tenant(conn, chosen.id)
            roster.ensure_roster_schema(conn)
            return _TENANT_COMMANDS[args.command](conn, args)
        except (roster.RosterRefused, platform.PlatformRefused, IdentityError, OSError) as error:
            print(f"stigmergy-admin: {error}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
