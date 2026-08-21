"""`stigmergy-entities` — a steward's door into the identity registry from their own clone:
`pending` · `approve` · `decline` · `merge` · `create` · `regenerate`. Each subcommand is a thin
skin over the library, in `stigmergy-queue`'s dialect (exit 130 on Ctrl-C, `--json` emitting the
machine value first).

The librarian PROPOSES identities as it files (`approved_by: ""` on the page); this tool is one of
the three doors a steward decides them through (the console and `review_decide` over MCP are the
others), and all three land the same commit through `entities.decide.apply`. A decision is
recorded in the review ledger AFTER the push lands, attributed to the steward — the librarian
reads that ledger to refuse re-proposing a declined identity, which is why `decline` (and its
siblings) need the queue database and refuse to run without it: a decline nobody recorded is one
the librarian re-proposes on the next capture.

Exit codes: 0 the command did what it said; 1 it refused (a collision, a dirty clone, an id that
is not a proposal) or `--check` found drift; 2 the TOOL could not run (no repo, no database).
"""
import argparse
import datetime
import json
import sys

from stigmergy.capture import decisions, schema
from stigmergy.capture.errors import CaptureError
from stigmergy.entities import birth, clone, decide, generator
from stigmergy.entities import mint as mint_lib
from stigmergy.entities.errors import EntityError
from stigmergy.index import store
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian.errors import LibrarianError
from stigmergy.review_kinds import KIND_ALIAS_PROPOSAL, KIND_IDENTITY_PROPOSAL, alias_item_id

_DUMP = {"ensure_ascii": False, "indent": 2}

EXIT_REFUSED = 1
EXIT_CANNOT_RUN = 2
EXIT_INTERRUPTED = 130


def _repo(args) -> str:
    """The steward's clone. Same env var, default and checkout predicate as the librarian's
    `--repo` and `stigmergy-views`' — it is the same checkout, told to one tool once."""
    path = librarian_config.repo_path(args.repo)
    if not librarian_config.is_repo_checkout(path):
        raise EntityError(
            f"{path} is not a git checkout — `--repo` (or ${librarian_config.REPO_ENV}) must point "
            f"at your clone of the knowledge repo, because every command here commits to it with "
            f"your own git identity")
    return path


def _connect(args):
    conn = store.connect(args.dsn)
    schema.ensure_capture_schema(conn)
    # Without it, a decision against a database no server has started on would push and then fail
    # on the INSERT — after the irreversible half.
    decisions.ensure_decisions_schema(conn)
    return conn


def _aliases(values) -> list[str]:
    """`--aliases "A, B" --aliases C` -> `["A", "B", "C"]`. Repeatable AND comma-separated,
    because a steward types this once, by hand, next to a name that may itself contain spaces."""
    out = []
    for value in values or ():
        out += [part.strip() for part in str(value).split(",") if part.strip()]
    return out


# ── pending ───────────────────────────────────────────────────────────────────────────────────
def pending_in(repo: str) -> dict:
    """What a steward has to decide, read from the clone's own pages — the registry is derived
    from them, so the checkout is the inbox's source of truth."""
    entities = generator.read_entity_pages(repo)
    proposals = [{"id": e.canonical_id, "name": e.name, "entity_type": e.entity_type,
                  "aliases": list(e.aliases), "page": e.relpath}
                 for e in entities if e.proposed]
    aliases = [{"entity_id": e.canonical_id, "entity_name": e.name, "alias": alias,
                "page": e.relpath}
               for e in entities for alias in e.proposed_aliases]
    return {"entities": proposals, "aliases": aliases}


def _cmd_pending(conn, args) -> int:
    pending = pending_in(_repo(args))
    if args.json:
        print(json.dumps(pending, **_DUMP))
        return 0
    if not pending["entities"] and not pending["aliases"]:
        print("nothing pending — every identity and every spelling in this clone is confirmed")
        return 0
    if pending["entities"]:
        print(f"{len(pending['entities'])} proposed identit"
              f"{'y' if len(pending['entities']) == 1 else 'ies'}:\n")
        for item in pending["entities"]:
            listed = f" (aliases: {', '.join(item['aliases'])})" if item["aliases"] else ""
            print(f"  {item['id']:<28} {item['entity_type']:<13} {item['name']}{listed}")
        print("\n  stigmergy-entities approve <id>            confirm it")
        print("  stigmergy-entities merge <id> --into <id>  it is that registered entity")
        print("  stigmergy-entities decline <id>            it is not an entity this brain wants")
    if pending["aliases"]:
        print(f"\n{len(pending['aliases'])} proposed spelling"
              f"{'' if len(pending['aliases']) == 1 else 's'}:\n")
        for item in pending["aliases"]:
            print(f"  {item['entity_id']:<28} {item['alias']!r:<30} for {item['entity_name']}")
        print("\n  stigmergy-entities approve <id> --alias <spelling>   confirm it")
        print("  stigmergy-entities decline <id> --alias <spelling>   it is not a spelling of it")
    return 0


# ── approve / decline / merge ─────────────────────────────────────────────────────────────────
def _decide(conn, args, *, action, item_kind: str, item_id: str, verdict: str,
            extra: dict | None = None) -> dict:
    """Preflight with the STEWARD's own identity, land the decision, then record it — after the
    push, like a birth: a ledger row for a decision that never landed would tell the librarian a
    proposal was declined while the proposed page still stands."""
    repo = _repo(args)
    author = clone.preflight(repo, args.branch, action=args.command)
    result = decide.apply(repo, action=action, branch=args.branch, author=author,
                          on_output=lambda line: print(line, file=sys.stderr))
    actor = _steward_name(args)
    decisions.record_decision(
        conn, item_kind=item_kind, item_id=item_id, verdict=verdict, actor=actor,
        source=decisions.SOURCE_CLI, notes=getattr(args, "reason", "") or "",
        extra={"commit": result["commit"], **(extra or {})})
    return {**result, "actor": actor}


def _steward_name(args) -> str:
    """Who is deciding, as the page's `approved_by` and the ledger record it: `--by`, else the
    clone's git EMAIL — the same spelling the server door records for a steward, so one person's
    decisions read alike whichever door they came through."""
    if args.by:
        return args.by
    _name, email = clone.identity(_repo(args), action=args.command)
    return email


def _cmd_approve(conn, args) -> int:
    approver = _steward_name(args)
    if args.alias:
        result = _decide(
            conn, args, item_kind=KIND_ALIAS_PROPOSAL, item_id=alias_item_id(args.id, args.alias),
            verdict=decisions.APPROVE,
            action=lambda repo: decide.approve_alias(repo, entity_id=args.id, alias=args.alias,
                                                     approved_by=approver, today=args.today))
    else:
        result = _decide(
            conn, args, item_kind=KIND_IDENTITY_PROPOSAL, item_id=args.id,
            verdict=decisions.APPROVE,
            action=lambda repo: decide.approve_entity(repo, entity_id=args.id,
                                                      approved_by=approver, today=args.today))
    return _print_decision(result, args, verb="approved")


def _cmd_decline(conn, args) -> int:
    if args.alias:
        result = _decide(
            conn, args, item_kind=KIND_ALIAS_PROPOSAL, item_id=alias_item_id(args.id, args.alias),
            verdict=decisions.REJECT,
            action=lambda repo: decide.decline_alias(repo, entity_id=args.id, alias=args.alias,
                                                     today=args.today))
    else:
        result = _decide(
            conn, args, item_kind=KIND_IDENTITY_PROPOSAL, item_id=args.id,
            verdict=decisions.REJECT,
            action=lambda repo: decide.decline_entity(repo, entity_id=args.id, today=args.today))
    return _print_decision(result, args, verb="declined")


def _cmd_merge(conn, args) -> int:
    approver = _steward_name(args)
    result = _decide(
        conn, args, item_kind=KIND_IDENTITY_PROPOSAL, item_id=args.id, verdict=decisions.MERGE,
        extra={"into": args.into},
        action=lambda repo: decide.merge_entity(repo, entity_id=args.id, into=args.into,
                                                approved_by=approver, today=args.today))
    return _print_decision(result, args, verb="merged")


def _print_decision(result: dict, args, *, verb: str) -> int:
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    print(f"{verb} — {result['summary']}")
    if result["reanchored"]:
        print(f"  re-anchored: {', '.join(result['reanchored'])}")
    print(f"  committed as {result['commit'][:12]} (steward: {result['steward']}), pushed to "
          f"{result['branch']}; recorded in the review ledger as {result['actor']}")
    return 0


# ── create: a birth with no proposal behind it ────────────────────────────────────────────────
def _cmd_create(conn, args) -> int:
    repo = _repo(args)
    author = clone.preflight(repo, args.branch, action="create")
    result = mint_lib.mint(
        repo, entity_id=args.entity_id, name=args.name, entity_type=args.type,
        aliases=_aliases(args.aliases), role=args.role or "", branch=args.branch,
        today=args.today, author=author, approved_by=args.by or author[1],
        on_output=lambda line: print(line, file=sys.stderr))
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    print(f"created — {result['page']} ({result['entity_type']}), regenerated "
          f"{result['registry']}")
    print(f"  committed as {result['commit'][:12]} (steward: {result['steward']}), pushed to "
          f"{result['branch']}")
    return 0


# ── regenerate ────────────────────────────────────────────────────────────────────────────────
def _cmd_regenerate(conn, args) -> int:
    repo = _repo(args)
    outcome = generator.check(repo) if args.check else generator.regenerate(repo)
    if args.json:
        print(json.dumps({"drift": bool(outcome.divergences), "changed": outcome.changed,
                          "pages": outcome.page_count,
                          "divergences": [d.message for d in outcome.divergences]}, **_DUMP))
        return EXIT_REFUSED if (args.check and outcome.divergences) else 0

    if args.check:
        if not outcome.divergences:
            print(f"{generator.REGISTRY_RELPATH} matches {generator.ENTITIES_RELDIR}/*.md across "
                  f"{outcome.page_count} entity page(s) — no drift")
            return 0
        for divergence in outcome.divergences:
            print(f"drift: {divergence.message}", file=sys.stderr)
        return EXIT_REFUSED

    if not outcome.changed:
        print(f"{generator.REGISTRY_RELPATH} already matches {generator.ENTITIES_RELDIR}/*.md "
              f"({outcome.page_count} entity page(s)) — nothing to write")
        return 0
    # Written locally and NOT committed: the decisions and `create` are the governed push paths
    # here, and a self-pushing `regenerate` would be a second writer to `main` with different
    # safety properties. Drift is also a disagreement a human should look at before publishing
    # the resolution.
    print(f"regenerated {generator.REGISTRY_RELPATH} from {outcome.page_count} entity page(s) — "
          f"written locally, NOT committed")
    for divergence in outcome.divergences:
        print(f"  fixed: {divergence.message.split(' — run ')[0]}")
    print(f"  review it and commit it yourself:  git -C {repo} add "
          f"{generator.REGISTRY_RELPATH} && git -C {repo} commit -m 'chore(registry): regenerate'")
    return 0


# ── parser ────────────────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-entities",
        description="Govern the identities the librarian proposes: confirm, merge or decline "
                    "them from your own clone, in one commit signed by you.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--repo", default=None,
                    help=f"your clone of the knowledge repo (default: "
                         f"${librarian_config.REPO_ENV} or {librarian_config.REPO_DEFAULT})")
    ap.add_argument("--branch", default="main", help="branch to commit to (default: main)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command", required=True)

    p_pending = sub.add_parser("pending", help="the identities and spellings waiting on a steward")
    p_pending.set_defaults(fn=_cmd_pending, needs_db=False)

    p_approve = sub.add_parser("approve", help="confirm a proposed identity (or, with --alias, a "
                                               "proposed spelling of a registered one)")
    p_decline = sub.add_parser("decline", help="decline a proposed identity — its page goes and "
                                               "the pages anchored to it lose the anchor (or, "
                                               "with --alias, drop a proposed spelling)")
    for parser in (p_approve, p_decline):
        parser.add_argument("id", help="the entity's registry id, as `pending` lists it")
        parser.add_argument("--alias", default="",
                            help="decide this proposed SPELLING of the entity instead of the "
                                 "entity itself")
    p_approve.set_defaults(fn=_cmd_approve, needs_db=True)
    p_decline.add_argument("--reason", default="",
                           help="why, for the review ledger — never a secret or personal data")
    p_decline.set_defaults(fn=_cmd_decline, needs_db=True)

    p_merge = sub.add_parser("merge", help="a proposed identity IS a registered entity: its name "
                                           "and spellings become that entity's aliases, its page "
                                           "goes, the pages anchored to it move over")
    p_merge.add_argument("id", help="the proposed entity's registry id")
    p_merge.add_argument("--into", required=True, help="the registered entity's id")
    p_merge.set_defaults(fn=_cmd_merge, needs_db=True)

    p_create = sub.add_parser(
        "create", help="register a brand-new, already-confirmed entity nobody has proposed")
    p_create.add_argument("--id", dest="entity_id", required=True,
                          help="the canonical registry id. It must be the slug of --name: the "
                               "registry is DERIVED from the pages, so an id nothing regenerates "
                               "would vanish at the next regenerate")
    p_create.add_argument("--name", required=True,
                          help="the entity's name — its page title, its filename and the "
                               "wikilink every other page resolves it by")
    p_create.add_argument("--type", required=True, choices=birth.ENTITY_TYPES,
                          help="the page's `entity_type` and the registry's `type`")
    p_create.add_argument("--aliases", action="append", default=[],
                          help="other spellings that mean this entity (comma-separated, "
                               "repeatable). Every alias silently reassigns mentions to it, so an "
                               "alias that collides with another entity is refused")
    p_create.add_argument("--role", default="",
                          help="one line on what this entity is, for the page's `role` field")
    p_create.set_defaults(fn=_cmd_create, needs_db=False)

    for parser in (p_approve, p_decline, p_merge, p_create):
        parser.add_argument("--by", default=None,
                            help="who is deciding (default: your git identity). Attribution, not "
                                 "authorization")
        parser.add_argument("--today", default=None,
                            help=argparse.SUPPRESS)   # injectable clock: `created`/`updated`

    p_regen = sub.add_parser(
        "regenerate", help=f"rebuild {generator.REGISTRY_RELPATH} from "
                           f"{generator.ENTITIES_RELDIR}/*.md")
    p_regen.add_argument("--check", action="store_true",
                         help="change nothing; exit non-zero and name every divergence (the CI "
                              "gate, and idempotence's proof)")
    p_regen.set_defaults(fn=_cmd_regenerate, needs_db=False)
    return ap


def _interrupted(during: str) -> int:
    print(f"stigmergy-entities: interrupted during `{during}` — if a commit had already been made it "
          f"is in your local clone and was not pushed; `git -C <repo> status` and `git log -1` say "
          f"which. Nothing was written to the review ledger", file=sys.stderr)
    return EXIT_INTERRUPTED


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.today = getattr(args, "today", None) or datetime.date.today().isoformat()
    conn = None
    try:
        if getattr(args, "needs_db", False):
            conn = _connect(args)
    except KeyboardInterrupt:
        return _interrupted(args.command)
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        print(f"stigmergy-entities: cannot reach the queue database ({ex}); is Postgres up "
              f"(`make db-up`)? A decision is recorded in the review ledger, so it needs one",
              file=sys.stderr)
        return EXIT_CANNOT_RUN
    try:
        return args.fn(conn, args)
    except (EntityError, CaptureError, LibrarianError) as ex:
        # One sentence, no traceback — including for git faults, whose stderr `librarian.gitcmd`
        # has already scrubbed and truncated.
        print(f"stigmergy-entities: {ex}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        return _interrupted(args.command)
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
