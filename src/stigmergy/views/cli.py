"""`stigmergy-views` — the operator's front door onto per-entity view regeneration. One
subcommand, one required target:

    stigmergy-views regenerate --entity <id>
    stigmergy-views regenerate --stale
    stigmergy-views regenerate --all

Conventions are `stigmergy-entities`'/`stigmergy-queue`'s, so this tool never teaches an operator a
third dialect: exit 130 on Ctrl-C, `--json` emitting the machine-readable value first, one
sentence (no traceback) for a domain refusal, id-and-display-name pairing on every entity named
in output (`librarian.report._anchor_phrase`'s convention, reused unmodified).
"""
import argparse
import asyncio
import json
import os
import sys

from stigmergy.index import store
from stigmergy.kernel.registry import load_registry
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian.errors import LibrarianError
from stigmergy.views import regenerate
from stigmergy.views.errors import ViewError

_DUMP = {"ensure_ascii": False, "indent": 2}

EXIT_REFUSED = 1
EXIT_CANNOT_RUN = 2
EXIT_INTERRUPTED = 130

REGISTRY_RELPATH = "ops/entity-registry.json"


def _repo(args) -> str:
    repo = args.repo or os.environ.get(librarian_config.REPO_ENV) or librarian_config.REPO_DEFAULT
    path = os.path.abspath(repo)
    # `.git` is a DIRECTORY in an ordinary clone but a FILE (a `gitdir: ...` pointer) in a
    # `git worktree add` checkout, so the test is `exists`, not `isdir`: `isdir` would refuse a
    # genuine worktree with the "not a git checkout" message below. `exists` accepts both real
    # shapes and still refuses a plain, non-git directory.
    if not os.path.exists(os.path.join(path, ".git")):
        raise ViewError(
            f"{path} is not a git checkout — `--repo` (or ${librarian_config.REPO_ENV}) must "
            f"point at your clone of the knowledge repo, because this command commits to it")
    return path


def _registry(repo: str):
    return load_registry(os.path.join(repo, *REGISTRY_RELPATH.split("/")))


def _connect(args):
    return store.connect(args.dsn)


def _who(entity_id: str, name: str) -> str:
    """`Name (`id`)` — `librarian.report._anchor_phrase`'s pairing, reused unmodified."""
    return f"{name} (`{entity_id}`)"


# The one road to a withheld synthesis: the bounded agent ran out of budget before a draft
# existed. `_report_single` below carries the longer form of the same fact.
WITHHELD_SUMMARY = "synthesis withheld (ran out of budget before a draft was ready)"


def _outcome_line(o: regenerate.RegenOutcome) -> str:
    who = _who(o.entity_id, o.entity_name)
    if o.action == "unchanged":
        return f"  {who}"
    if o.action == "removed":
        return f"  {who}  no anchored pages remain — view removed — committed {o.commit[:12]}"
    if o.action == "written":
        shown = (f"{o.member_count} page(s), timeline showing the {o.timeline_shown} most recent "
                f"({o.timeline_total - o.timeline_shown} older not shown)"
                if o.timeline_total > o.timeline_shown else f"{o.member_count} page(s)")
        tail = WITHHELD_SUMMARY if not o.synthesis_shipped else "synthesis written"
        acl_note = ""
        if o.acl is not None and len(o.acl) == 0:
            acl_note = ("  acl: [] — its members' audiences have nothing in common, so this "
                       "view is visible to unrestricted clients only, nobody scoped")
        elif o.acl:
            acl_note = f"  acl: [{', '.join(o.acl)}]"
        return f"  {who}  {shown} — {tail} — committed {o.commit[:12]}{acl_note}"
    return f"  {who}  refused: {o.message}"


def _cmd_regenerate(conn, args) -> int:
    repo = _repo(args)
    registry = _registry(repo)

    if args.entity:
        # Routed through the SAME `regenerate.run` the batch flags use, rather than a second,
        # hand-rolled `job_run` block around `regenerate_entity`. A per-entity copy of that
        # bookkeeping gets three things wrong at once: it records nothing at all when the call
        # raises, it has no `refused` key in its stats, and it can no more see a
        # `KeyboardInterrupt` than `job_run` itself can. `run` over a single-element list has all
        # three already.
        result = asyncio.run(regenerate.run(repo, conn, [args.entity], registry=registry,
                                            branch=args.branch, force=args.force))
        return _report_single(result.outcomes[0], args)

    population = "with an existing view" if args.stale else "with at least one anchored page"
    # `--force` widens `--stale`'s population to every entity with an existing view, which is
    # what its help text promises — computing the stale-only population here would make the flag
    # silently do nothing for the natural spelling of the retry lever it exists to be. `--all`'s
    # population is ALREADY "every anchored entity" regardless of force, so it needs no widening.
    entity_ids = (sorted(regenerate.existing_view_ids(repo)) if (args.stale and args.force)
                 else regenerate.list_stale_entities(repo) if args.stale
                 else regenerate.list_all_anchored_entities(repo))
    checked_total = (len(regenerate.existing_view_ids(repo)) if args.stale
                    else len(regenerate.list_all_anchored_entities(repo)))

    result = asyncio.run(regenerate.run(repo, conn, entity_ids, registry=registry,
                                        branch=args.branch, force=args.force))
    return _report_batch(result, population=population, checked_total=checked_total, args=args)


def _report_single(o: regenerate.RegenOutcome, args) -> int:
    if args.json:
        print(json.dumps({
            "entity_id": o.entity_id, "action": o.action, "message": o.message,
            "member_count": o.member_count, "synthesis_shipped": o.synthesis_shipped,
            "acl": o.acl, "commit": o.commit, "path": o.path}, **_DUMP))
    elif o.action == "refused-unknown-entity":
        print(f'stigmergy-views: refusing to regenerate — "{o.entity_id}" is not a registered '
             f"entity. Check the id (`stigmergy-entities list` shows what's parked; the registry "
             f"itself is `ops/entity-registry.json` in the knowledge repo), or mint it first with "
             f"`stigmergy-entities create`/`approve` if it should exist.", file=sys.stderr)
    elif o.action == "refused-no-members":
        print(f"stigmergy-views: refusing to regenerate {_who(o.entity_id, o.entity_name)} — "
             f'no page anywhere in the repo declares entity: ["{o.entity_id}"] yet. A view is '
             f"built from its members; with none, there is nothing to build. Nothing was "
             f"written. This is not a bug — it just means nothing has been captured about this "
             f"entity yet.", file=sys.stderr)
    elif o.action == "unchanged":
        print(f"{_who(o.entity_id, o.entity_name)} is already up to date — {o.member_count} "
             f"page(s). Nothing was written; the member set has not changed since the last "
             f"regeneration.")
    elif o.action == "removed":
        print(f"removed {o.path} — the last page anchored to {_who(o.entity_id, o.entity_name)} "
             f"is gone (superseded or re-anchored elsewhere); nothing points at it anymore, so "
             f"there is nothing left to summarize.")
        print(f"  committed {o.commit[:12]} (steward: App bot), pushed to {args.branch}")
    else:
        shown = (f"timeline showing the {o.timeline_shown} most recent "
                f"({o.timeline_total - o.timeline_shown} older not shown)"
                if o.timeline_total > o.timeline_shown else "")
        print(f"regenerated {o.path} — {o.member_count} page(s) anchored"
             + (f", {shown}" if shown else ""))
        if not o.synthesis_shipped:
            print("  synthesis: WITHHELD — the agent's run exceeded its request/tool-call budget "
                 "before a draft was ready; the page ships with its skeleton only (timeline, "
                 "backlinks — both deterministic and unaffected). Full explanation on the page "
                 "itself.")
        if o.acl is not None and len(o.acl) == 0:
            print("  acl: [] — its members' audiences have nothing in common, so this view is "
                 "visible to unrestricted clients only, nobody scoped")
        elif o.acl:
            print(f"  acl: [{', '.join(o.acl)}]")
        print(f"  committed {o.commit[:12]} (steward: App bot), pushed to {args.branch}")
    return EXIT_REFUSED if o.action.startswith("refused") else 0


def _report_batch(result: regenerate.RunResult, *, population: str, checked_total: int, args) -> int:
    if args.json:
        print(json.dumps({"stats": result.stats,
                          "outcomes": [{"entity_id": o.entity_id, "action": o.action,
                                       "synthesis_shipped": o.synthesis_shipped, "acl": o.acl,
                                       "commit": o.commit} for o in result.outcomes]}, **_DUMP))
        return 0
    changed = [o for o in result.outcomes if o.action != "unchanged"]
    unchanged = [o for o in result.outcomes if o.action == "unchanged"]
    if not changed:
        print(f"checked {checked_total} entities {population} — all {checked_total} already "
             f"{'match their current member set' if args.stale else 'have an up-to-date view'}"
             f"; nothing regenerated, nothing committed.")
        return 0
    print(f"checked {checked_total} entities {population} — {len(changed)} stale, "
         f"{len(unchanged)} up to date\n")
    for o in changed:
        print(_outcome_line(o))
    if unchanged:
        names = ", ".join(_who(o.entity_id, o.entity_name) for o in unchanged)
        print(f"\n{len(unchanged)} up to date, unchanged: {names}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-views",
        description="Per-entity view regeneration: a deterministic skeleton (timeline, "
                    "backlinks) plus an agent-written synthesis, one file per entity under "
                    "views/, committed by the App bot.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--repo", default=None,
                    help=f"your clone of the knowledge repo (default: "
                         f"${librarian_config.REPO_ENV} or {librarian_config.REPO_DEFAULT})")
    ap.add_argument("--branch", default="main", help="branch to commit to (default: main)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command", required=True)

    p_regen = sub.add_parser("regenerate", help="regenerate one or more entities' views")
    target = p_regen.add_mutually_exclusive_group(required=True)
    target.add_argument("--entity", default=None,
                        help="regenerate exactly this entity's view, even if it is not stale")
    target.add_argument("--stale", action="store_true",
                        help="regenerate every entity whose view no longer matches its "
                             "current member set")
    target.add_argument("--all", action="store_true",
                        help="regenerate every entity with at least one anchored page, whether "
                             "or not it has a view yet (the backfill flag — use it to seed views "
                             "for the first time, or any time you don't trust the staleness "
                             "bookkeeping)")
    p_regen.add_argument("--force", action="store_true",
                         help="bypass the staleness check and re-attempt synthesis even against "
                              "an UNCHANGED member set — the one operator-triggerable retry for "
                              "a synthesis that was withheld for reasons unrelated to the member "
                              "set changing (most useful with --entity; --stale/--all accept it "
                              "too, and it widens their population to every checked entity)")
    p_regen.set_defaults(fn=_cmd_regenerate)
    return ap


def _interrupted() -> int:
    # `commit_and_push` commits LOCALLY, then pushes — a Ctrl-C landing between those two calls
    # leaves the entity genuinely committed but not yet pushed, so any message asserting it "was
    # NOT committed" would be false in exactly that window. The message names the range of states
    # instead of a specific one this code cannot know.
    print("stigmergy-views: interrupted while regenerating — entities already committed AND "
         "pushed before the interrupt are done (see the lines printed above this one); the entity "
         "being written when this happened may be anywhere from untouched to locally committed "
         "but not yet pushed (`git -C <repo> status`/`log` on your clone shows which). Re-run with "
         "the same flags: already-pushed entities no-op via the staleness hash, so this is safe to "
         "re-run over the whole original set regardless of exactly where the interrupt landed.",
         file=sys.stderr)
    return EXIT_INTERRUPTED


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    conn = None
    try:
        conn = _connect(args)
    except KeyboardInterrupt:
        return _interrupted()
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        print(f"stigmergy-views: cannot reach the queue database ({ex}); is Postgres up "
             f"(`make db-up`)?", file=sys.stderr)
        return EXIT_CANNOT_RUN
    try:
        return args.fn(conn, args)
    except (ViewError, LibrarianError) as ex:
        print(f"stigmergy-views: {ex}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        return _interrupted()
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
