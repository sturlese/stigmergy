"""`stigmergy-views` — the operator's front door onto per-entity view regeneration. One
subcommand, one required target:

    stigmergy-views regenerate --entity <id>
    stigmergy-views regenerate --stale
    stigmergy-views regenerate --all
    stigmergy-views regenerate --sweep

Exit 130 on Ctrl-C, `--json` first, one sentence (no traceback) per domain refusal,
id-and-display-name pairing on every entity named in output.
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


def _repo(args) -> str:
    # The worktree-tolerant predicate this command was already right about lives in
    # `librarian.config.is_repo_checkout` now — `stigmergy-entities` had its own, stricter copy.
    path = librarian_config.repo_path(args.repo)
    if not librarian_config.is_repo_checkout(path):
        raise ViewError(
            f"{path} is not a git checkout — `--repo` (or ${librarian_config.REPO_ENV}) must "
            f"point at your clone of the knowledge repo, because this command commits to it")
    return path


def _registry(repo: str):
    return load_registry(os.path.join(repo, *librarian_config.REGISTRY_RELPATH.split("/")))


def _connect(args):
    return store.connect(args.dsn)


def _who(entity_id: str, name: str) -> str:
    """`Name (`id`)` — `librarian.report._anchor_phrase`'s pairing."""
    return f"{name} (`{entity_id}`)"


WITHHELD_SUMMARY = "synthesis withheld (ran out of budget before a draft was ready)"

# `--sweep`'s population, in the report line's grammar. Named because the reason it is a UNION is
# the whole point of the target (see `views.staleness.list_sweep_entities`).
SWEEP_POPULATION = "with an anchored page or an existing view"


def _timeline_phrase(o: regenerate.RegenOutcome) -> str:
    """The capped-timeline note, or `""` when nothing was capped — one sentence for both the batch
    line and the single-entity report, which must not drift into two."""
    if o.timeline_total <= o.timeline_shown:
        return ""
    return (f"timeline showing the {o.timeline_shown} most recent "
            f"({o.timeline_total - o.timeline_shown} older not shown)")


def _acl_note(o: regenerate.RegenOutcome) -> str:
    """The view's audience, or `""` when it carries no ACL. `acl: []` is a DELIBERATE empty
    audience, not an absent one, so it gets its own sentence rather than the listing."""
    if o.acl is not None and len(o.acl) == 0:
        return ("  acl: [] — its members' audiences have nothing in common, so this view is "
               "visible to unrestricted clients only, nobody scoped")
    if o.acl:
        return f"  acl: [{', '.join(o.acl)}]"
    return ""


def _outcome_line(o: regenerate.RegenOutcome) -> str:
    who = _who(o.entity_id, o.entity_name)
    if o.action == "unchanged":
        return f"  {who}"
    if o.action == "removed":
        # `o.message`, never a sentence composed here: there are two roads to a removal and only
        # `regenerate` knows which one was taken.
        return f"  {who}  view removed: {o.message} — committed {o.commit[:12]}"
    if o.action == "written":
        phrase = _timeline_phrase(o)
        shown = f"{o.member_count} page(s), {phrase}" if phrase else f"{o.member_count} page(s)"
        tail = WITHHELD_SUMMARY if not o.synthesis_shipped else "synthesis written"
        return f"  {who}  {shown} — {tail} — committed {o.commit[:12]}{_acl_note(o)}"
    return f"  {who}  refused: {o.message}"


def _cmd_regenerate(conn, args) -> int:
    repo = _repo(args)
    registry = _registry(repo)

    if args.entity:
        # Routed through the same `regenerate.run` the batch flags use — a hand-rolled per-entity
        # `job_run` block records nothing on a raise, lacks the `refused` stats key, and cannot
        # see a `KeyboardInterrupt`.
        result = asyncio.run(regenerate.run(repo, conn, [args.entity], registry=registry,
                                            branch=args.branch, force=args.force))
        return _report_single(result.outcomes[0], args)

    if args.sweep:
        # The union population, through `regenerate.sweep` — the SAME entry point the librarian
        # worker's idle pass uses, so an operator running this by hand and the unattended pass can
        # never converge different sets. `job=` is the ONE thing that differs, and deliberately:
        # three roads, three `job_runs.job` names. This is an operator's run whatever target it
        # was given, so it records itself as one; `views-sweep` means the unattended pass.
        result = asyncio.run(regenerate.sweep(repo, conn, registry=registry, branch=args.branch,
                                              force=args.force, job=regenerate.JOB_NAME))
        return _report_batch(result, population=SWEEP_POPULATION,
                             current_phrase="already match the corpus",
                             checked_total=result.population, args=args)

    population = "with an existing view" if args.stale else "with at least one anchored page"
    current_phrase = ("match their current member set" if args.stale
                     else "have an up-to-date view")
    # `--force` widens `--stale`'s population to every entity with an existing view (its help
    # text's promise); `--all`'s population needs no widening.
    entity_ids = (sorted(regenerate.existing_view_ids(repo)) if (args.stale and args.force)
                 else regenerate.list_stale_entities(repo) if args.stale
                 else regenerate.list_all_anchored_entities(repo))
    checked_total = (len(regenerate.existing_view_ids(repo)) if args.stale
                    else len(entity_ids))

    result = asyncio.run(regenerate.run(repo, conn, entity_ids, registry=registry,
                                        branch=args.branch, force=args.force))
    return _report_batch(result, population=population, current_phrase=current_phrase,
                         checked_total=checked_total, args=args)


def _report_single(o: regenerate.RegenOutcome, args) -> int:
    if args.json:
        print(json.dumps({
            "entity_id": o.entity_id, "action": o.action, "message": o.message,
            "member_count": o.member_count, "synthesis_shipped": o.synthesis_shipped,
            "acl": o.acl, "commit": o.commit, "path": o.path}, **_DUMP))
    elif o.action == "refused-unknown-entity":
        print(f'stigmergy-views: refusing to regenerate — "{o.entity_id}" is not a registered '
             f"entity. Check the id (`stigmergy-entities list` shows what's parked; the registry "
             f"itself is `{librarian_config.REGISTRY_RELPATH}` in the knowledge repo), or mint it first with "
             f"`stigmergy-entities create`/`approve` if it should exist.", file=sys.stderr)
    elif o.action == "refused-unusable-id":
        print(f"stigmergy-views: refusing to regenerate — {o.message}.", file=sys.stderr)
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
        # `o.message` is the WHOLE explanation (see `_RemovalCause`): each road carries its own,
        # and there is no sentence true of both to close with. The tail that used to be appended
        # here — "nothing anchors it any more" — contradicted the de-registration road's own
        # message, where the pages still anchor the entity and only the registry stopped
        # governing them.
        print(f"removed {o.path} — {o.message}.")
        print(f"  committed {o.commit[:12]} (steward: App bot), pushed to {args.branch}")
    else:
        shown = _timeline_phrase(o)
        print(f"regenerated {o.path} — {o.member_count} page(s) anchored"
             + (f", {shown}" if shown else ""))
        if not o.synthesis_shipped:
            print("  synthesis: WITHHELD — the agent's run exceeded its request/tool-call budget "
                 "before a draft was ready; the page ships with its skeleton only (timeline, "
                 "backlinks — both deterministic and unaffected). Full explanation on the page "
                 "itself.")
        acl_note = _acl_note(o)
        if acl_note:
            print(acl_note)
        print(f"  committed {o.commit[:12]} (steward: App bot), pushed to {args.branch}")
    return EXIT_REFUSED if o.action.startswith("refused") else 0


def _report_batch(result: regenerate.RunResult, *, population: str, current_phrase: str,
                  checked_total: int, args) -> int:
    """`population` names WHICH entities were looked at and `current_phrase` closes the
    nothing-to-do sentence for that same population — passed in as a pair rather than branched on
    a flag here, so a new target states its own two halves instead of adding a third arm to a
    conditional that already had two."""
    if args.json:
        print(json.dumps({"stats": result.stats,
                          "outcomes": [{"entity_id": o.entity_id, "action": o.action,
                                       "synthesis_shipped": o.synthesis_shipped, "acl": o.acl,
                                       "commit": o.commit} for o in result.outcomes]}, **_DUMP))
        return 0
    changed = [o for o in result.outcomes if o.action != "unchanged"]
    unchanged = [o for o in result.outcomes if o.action == "unchanged"]
    if not checked_total and result.skip_reasons:
        # A run that examined NOTHING and gave a reason reports the reason alone. "all 0 already
        # match the corpus" is vacuously true and reads as success, which is the opposite of what
        # a sweep that never started needs to say.
        _print_skip_reasons(result, lead="")
        return 0
    if not changed:
        print(f"checked {checked_total} entities {population} — all {checked_total} already "
             f"{current_phrase}; nothing regenerated, nothing committed.")
        _print_skip_reasons(result)
        return 0
    print(f"checked {checked_total} entities {population} — {len(changed)} stale, "
         f"{len(unchanged)} up to date\n")
    for o in changed:
        print(_outcome_line(o))
    if unchanged:
        names = ", ".join(_who(o.entity_id, o.entity_name) for o in unchanged)
        print(f"\n{len(unchanged)} up to date, unchanged: {names}")
    _print_skip_reasons(result)
    return 0


def _print_skip_reasons(result: regenerate.RunResult, *, lead: str = "\n") -> None:
    """What the run did NOT do, if anything: the per-run ceiling (which no operator target sets),
    an id no view can be named from, a branch that moved mid-batch, another sweep already holding
    the lock. Printed off the list rather than branched on here — a run that grows a new way to
    stop must not have to remember to add its own reporting. `lead` is blank when these are the
    ONLY lines, so a refusal does not open with an empty one."""
    for reason in result.skip_reasons:
        print(f"{lead}{reason}")


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
    target.add_argument("--sweep", action="store_true",
                        help="converge views/ to the corpus: the UNION of --stale and --all, "
                             "which is the only population that both CREATES a missing view and "
                             "REMOVES an orphaned one (--all misses a view whose members have all "
                             "gone, --stale misses an entity that never had a view). The same "
                             "pass the librarian worker runs periodically — run it by hand when "
                             "you do not want to wait for the interval")
    p_regen.add_argument("--force", action="store_true",
                         help="bypass the staleness check and re-attempt synthesis even against "
                              "an UNCHANGED member set — the one operator-triggerable retry for "
                              "a synthesis that was withheld for reasons unrelated to the member "
                              "set changing (most useful with --entity; --stale/--all accept it "
                              "too, and it widens their population to every checked entity)")
    p_regen.set_defaults(fn=_cmd_regenerate)
    return ap


def _interrupted() -> int:
    # `commit_and_push` commits locally, then pushes — a Ctrl-C between the two leaves the entity
    # committed but unpushed, so the message names the range of states, not one it cannot know.
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
