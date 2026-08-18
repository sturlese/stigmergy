"""`stigmergy-repair` — the operator's window on the repair loop:

    stigmergy-repair [--dsn DSN] [--repo PATH] [--json] propose
    stigmergy-repair [...] list
    stigmergy-repair [...] show <id>
    stigmergy-repair [...] delete <path>... --why "<reason>"

Four READ-or-PROPOSE commands and deliberately no fifth: **there is no `apply` here.** A proposal
is applied only through a door that decides who may authorize it, and a terminal knows who is
typing but not what they are allowed to approve. `show` renders what a proposal WOULD do so a
steward can read it before answering somewhere that can.

`delete` is the same authority level as `propose` — it inserts a PENDING row and nothing else — and
it exists as a command because a deletion is the one repair a MODEL may never propose (ADR 039's
second amendment). Judging that a page is stale is exactly the judgment that is not code's and not
a model's; computing what has to happen to the rest of the corpus afterwards is exactly the
judgment that is.

The only module in this package that reads the environment beyond one setting resolver, opens a
connection, or imports `stigmergy.index.store` — every other module takes `conn` and settings as
plain arguments.
"""
import argparse
import asyncio
import json
import sys

from stigmergy.capture import schema as capture_schema
from stigmergy.index import store as index_store
from stigmergy.librarian import config as librarian_config
from stigmergy.repair import deletion, proposer, schema, store
from stigmergy.repair.errors import RepairError
from stigmergy.repair.settings import RepairSettings
from stigmergy.server.errors import StartupError
from stigmergy.text import one_line, sanitize

_DUMP = {"ensure_ascii": False, "indent": 2}

EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

# How much of a rationale a list line shows before it is cut. A list is a scan, not a read.
LIST_RATIONALE_CHARS = 90

# The kind column, DERIVED from the vocabulary (`capture/cli.py::_KIND_WIDTH`'s own precedent) so
# a kind added to `schema.KINDS` widens the column instead of shifting every row after it.
KIND_WIDTH = max(len(kind) for kind in schema.KINDS)


def _err(message: str) -> None:
    print(f"stigmergy-repair: {message}", file=sys.stderr)


def _connect(args):
    conn = index_store.connect(args.dsn)
    # Both schemas, so a FRESH database is never a table short (`UndefinedTable`) — a gap a mature
    # database, and every test fixture, hides. `capture_queue`/`job_runs` first: this package
    # records a `job_runs` row per propose pass.
    capture_schema.ensure_capture_schema(conn)
    schema.ensure_repair_schema(conn)
    return conn


def _settings(args) -> RepairSettings:
    """The settings, plus the one predicate a command that READS the checkout owes: the proposer
    reads the pages, the registry and its own skill at a real clone, and a bare directory of
    markdown would answer every question against a corpus the apply will not commit into."""
    settings = RepairSettings.from_env(args)      # may raise StartupError — a bad bound
    if not librarian_config.is_repo_checkout(settings.repo):
        raise RepairError(
            f"{settings.repo} is not a git checkout — `--repo` (or ${librarian_config.REPO_ENV}) "
            f"must point at your clone of the knowledge repo, because a proposal is validated "
            f"against the pages that are actually committed there")
    return settings


# ── propose ───────────────────────────────────────────────────────────────────────────────────
def _cmd_propose(conn, args) -> int:
    settings = _settings(args)
    result = asyncio.run(proposer.propose_from_findings(conn, settings=settings))
    if args.json:
        print(json.dumps({"run_id": result.run_id, "proposal_ids": result.proposal_ids,
                          **result.stats}, **_DUMP))
        return 0
    print(f"{result.findings_seen} proposable finding(s) in the latest gardener run: "
          f"{result.proposed} proposed, {result.skipped_known} already reviewed, "
          f"{result.skipped_invalid} skipped")
    for reason in result.skip_reasons:
        print(f"  skipped: {reason}")
    if result.proposal_ids:
        print("\n  stigmergy-repair show <id>   what one proposal would change")
    return 0


# ── delete ────────────────────────────────────────────────────────────────────────────────────
def _cmd_delete(conn, args) -> int:
    """Compute the sweep for the pages named, and store it as one pending proposal.

    Everything a person could get wrong is refused BEFORE the row exists — an entity page, a path
    outside the corpus, a reference the sweep cannot rewrite, a plan over its ceiling, a deletion
    already waiting on somebody — because a stored proposal is a question a steward has to answer,
    and a question whose answer cannot be carried out is worse than a refusal here.
    """
    settings = _settings(args)
    why = one_line(str(args.why or ""), proposer.MAX_RATIONALE_CHARS)
    if not why:
        raise RepairError(
            "a deletion needs a reason: `--why \"<what makes this page stale>\"`. It is what a "
            "steward reads beside Approve, and what `git log` will carry afterwards")

    ops = deletion.plan(settings.repo, list(args.paths))
    oversize = deletion.oversize_reason(ops, settings.max_delete_plan_bytes)
    if oversize:
        raise RepairError(oversize)
    key = schema.content_key(ops, kind=schema.KIND_DELETE)
    waiting = next((row for row in store.pending_proposals(conn) if row["content_key"] == key),
                   None)
    if waiting is not None:
        raise RepairError(
            f"this exact deletion is already waiting on a steward as proposal #{waiting['id']} — "
            f"decide that one rather than adding a second question about the same pages")

    proposal_id = store.insert_proposal(
        conn, run_id=0, finding_ids=[], target_paths=schema.target_paths(ops), ops=ops,
        rationale=why, content_key=key, kind=schema.KIND_DELETE,
        # Empty on purpose, and the only kind for which it can be: no model proposed this, and
        # stamping one here would attribute a person's judgment to a model that was never asked.
        model_id="",
        # The deleted pages ARE the question, so they are what a later run recognises as already
        # asked. The scrubbed pages are a consequence and move with the corpus.
        finding_subjects=[deletion.deleted_paths(ops)])
    if args.json:
        print(json.dumps({"proposal_id": proposal_id, "deleted": deletion.deleted_paths(ops),
                          "scrubbed": deletion.scrubbed_paths(ops)}, **_DUMP))
        return 0
    _print_plan(ops)
    print(f"\nstored as proposal #{proposal_id}, waiting on a steward — nothing has changed in "
          f"the knowledge repo.")
    print(f"  stigmergy-repair show {proposal_id}   read it back")
    return 0


def _print_plan(ops) -> None:
    """The sweep in plain English, for the person who just typed the command."""
    removed, scrubbed = deletion.deleted_paths(ops), deletion.scrubbed_paths(ops)
    print(f"this would remove {len(removed)} page(s):")
    for path in removed:
        print(f"  {sanitize(path)}")
    if not scrubbed:
        print("nothing in the corpus refers to them, so no other page changes.")
        return
    print(f"and rewrite {len(scrubbed)} page(s) that refer to them, taking out the links and "
          f"leaving the rest of each page alone:")
    for path in scrubbed:
        print(f"  {sanitize(path)}")


# ── list ──────────────────────────────────────────────────────────────────────────────────────
def _cmd_list(conn, args) -> int:
    pending = store.pending_proposals(conn)
    decided = store.recent_decided(conn, limit=args.limit)
    if args.json:
        print(json.dumps({"pending": pending, "recent": decided}, **_DUMP, default=str))
        return 0
    if not pending:
        print("no proposals waiting on a steward")
    else:
        print(f"{len(pending)} proposal(s) waiting on a steward\n")
        for row in pending:
            print(f"  #{row['id']:<5} {row['kind']:<{KIND_WIDTH}} {len(row['ops'])} op(s) on "
                  f"{', '.join(row['target_paths']) or '(none)'}")
            print(f"        {_clip(row['rationale'], LIST_RATIONALE_CHARS)}")
    if decided:
        print(f"\nlast {len(decided)} decided:")
        for row in decided:
            tail = row["applied_commit"][:12] or row["error"] or row["notes"]
            print(f"  #{row['id']:<5} {row['status']:<9} {row['decided_by'] or '-':<24} "
                  f"{_clip(tail, LIST_RATIONALE_CHARS)}")
    return 0


# ── show ──────────────────────────────────────────────────────────────────────────────────────
def _cmd_show(conn, args) -> int:
    row = store.proposal(conn, args.id)
    if row is None:
        raise RepairError(f"proposal {args.id} does not exist")
    if args.json:
        print(json.dumps(row, **_DUMP, default=str))
        return 0
    print(f"proposal #{row['id']}  {row['status']}  ({row['kind']})")
    print(f"  from gardener run {row['run_id']}, findings "
          f"{', '.join(str(i) for i in row['finding_ids']) or 'none recorded'}")
    if row["model_id"]:
        print(f"  proposed by {row['model_id']}")
    if row["decided_by"]:
        print(f"  decided by {row['decided_by']} at {row['decided_at']}")
    if row["applied_commit"]:
        print(f"  applied as {row['applied_commit']}")
    if row["error"]:
        print(f"  error: {row['error']}")
    print(f"\n{row['rationale']}\n")
    print("what it would change:")
    for line in preview(row):
        print(f"  {line}")
    return 0


def preview(row: dict) -> list[str]:
    """A unified preview of one proposal, composed from the OPS ALONE — no git, no clone, no
    network. A steward reading this is deciding whether to authorize it, so it is rendered from
    exactly the stored fact the apply will act on, never from a re-derivation that could differ.

    Four shapes, because there are four kinds. The additive ops are additive by construction, so
    every line is a `+`: `backlink` adds one `related:` entry, and a callout kind adds that entry
    AND the callout block `page.with_callout` appends. An `entity-body` op REPLACES the body below
    the page's own `# Title`, so its preview says so with a `-` line and then shows the draft in
    full — for that kind the draft IS what a steward is judging, and a preview that summarised it
    would be hiding the only thing worth reading. A `delete` op's two shapes both say what STOPS
    being true, and the scrub deliberately does NOT show its planned bytes: they are the apply's
    contract with itself, not the thing a steward is judging, and a whole page per scrubbed page
    would bury the one line that matters — which pages cease to exist. An `entity-alias` op's four
    shapes say what each page BECOMES, and they hide their planned bytes for the deletion's reason:
    what a steward is judging is which identity absorbs which, and four whole files would bury it.
    """
    lines: list[str] = []
    for op in row.get("ops") or ():
        kind = str(op.get(schema.OP_KIND_KEY, ""))
        # Sanitized again HERE, though the proposer already did it on the way in: this function
        # renders a row read back from the database, and a row is not proof of the path it arrived
        # by. An ANSI escape in somebody's terminal is the cost of assuming.
        path, link, note = (sanitize(str(op.get(key) or "")) for key in ("path", "link", "note"))
        lines.append(f"--- {path}")
        if kind == schema.KIND_ENTITY_BODY:
            lines += _body_preview(op)
            continue
        if kind in _DELETE_PHRASES:
            lines.append(f"-   {_DELETE_PHRASES[kind]}")
            continue
        if kind in _MERGE_PHRASES:
            lines.append(f"~   {_MERGE_PHRASES[kind]}")
            continue
        lines.append(f"+   related: [[{link}]]")
        if kind in _CALLOUT_PHRASES:
            callout, phrase = _CALLOUT_PHRASES[kind]
            lines.append(f"+   > [!{callout}] {phrase} [[{link}]]")
            lines.append(f"+   > {' '.join(str(note).split())}")
    return lines


# What each of the `delete` kind's two ops does to one page, in the words a steward needs. Both are
# `-` lines: one page stops existing, and the other stops saying something it used to say.
_DELETE_PHRASES = {
    deletion.OP_DELETE: "(the whole page is removed)",
    deletion.OP_SCRUB: "(every link to the removed page(s) taken out; nothing else changes here)",
}

# What each of the `entity-alias` kind's four ops does to one file, in the words a steward needs.
# `~` rather than `+`/`-`: nothing is added and nothing is removed — each of these files is
# REWRITTEN, and a preview claiming otherwise would be describing a shape the gates never see.
# Spelled from `repair.schema`'s op names, which `entity_alias` re-exports, so this table cannot
# name an op the applier does not perform.
_MERGE_PHRASES = {
    schema.ALIAS_OP_NAME: ("(this identity SURVIVES: it takes the other's alternative names and "
                           "links to it)"),
    schema.RETIRE_OP_NAME: ("(this identity is ABSORBED: marked superseded by the survivor, its "
                            "alternative names moved. The page itself stays)"),
    schema.REANCHOR_OP_NAME: "(re-anchored from the absorbed entity to the survivor)",
    schema.REGISTRY_OP_NAME: ("(regenerated from the entity pages by `stigmergy-entities "
                              "regenerate`)"),
}


def _body_preview(op: dict) -> list[str]:
    """One drafted body, as the diff it is. The frontmatter and the `# Title` line are not shown
    because they do not change — the apply rewrites `updated:` and, when the page declares an
    empty one, `role:`, and nothing else."""
    role = " ".join(sanitize(str(op.get("role") or "")).split())
    lines = ["-   (the body below this page's `# Title` line, replaced)"]
    if role:
        lines.append(f'+   role: "{role}"')
    lines += [f"+   {line}" for line in sanitize(str(op.get("body_markdown") or "")).splitlines()]
    return lines


# Hand-mirrored from `librarian.page.CALLOUT_STYLES` rather than imported: importing the page
# policy for a PREVIEW would put the librarian's write-path module on the CLI's import graph for
# two strings. A preview that drifts from the applier shows a steward the wrong thing, so if that
# table ever changes, change this with it — `tests/repair/test_cli.py` pins the pair.
_CALLOUT_PHRASES = {
    "overlap": ("NOTE", "Overlaps with"),
    "contradiction": ("WARNING", "Contradiction with"),
}


def _clip(text: str, width: int) -> str:
    """`stigmergy.text.one_line`: control characters stripped, whitespace collapsed, clamped at a
    word boundary. A list line is one line, and every value on it is model- or steward-written."""
    return one_line(text, width)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stigmergy-repair",
        description="The governed repair loop: an agent proposes a concrete additive repair for a "
                    "gardener finding, code validates it, and a steward approves it elsewhere — "
                    "nothing is applied from this terminal.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${index_store.DSN_ENV} or "
                         f"{index_store.DSN_DEFAULT})")
    ap.add_argument("--repo", default=None,
                    help=f"your clone of the knowledge repo, which proposals are validated against "
                         f"(default: ${librarian_config.REPO_ENV} or "
                         f"{librarian_config.REPO_DEFAULT})")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser(
        "propose", help="read the latest gardener run's findings and propose repairs for them")
    p_propose.set_defaults(fn=_cmd_propose)

    p_list = sub.add_parser("list", help="proposals waiting on a steward, plus recent decisions")
    p_list.add_argument("--limit", type=int, default=20,
                        help="how many decided proposals to show (default: 20)")
    p_list.set_defaults(fn=_cmd_list)

    p_show = sub.add_parser("show", help="one proposal, and what it would change")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(fn=_cmd_show)

    p_delete = sub.add_parser(
        "delete",
        help="propose removing one or more corpus pages, with every reference to them swept out")
    p_delete.add_argument("paths", nargs="+",
                          help="repo-relative page paths, e.g. 'wiki/notes/Old Memo.md'")
    p_delete.add_argument("--why", required=True,
                          help="why these pages should go — a steward reads it beside Approve, and "
                               "the commit carries it afterwards")
    p_delete.set_defaults(fn=_cmd_delete)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    conn = None
    try:
        conn = _connect(args)
    except KeyboardInterrupt:
        _err("interrupted while connecting to the queue database")
        return EXIT_INTERRUPTED
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        _err(f"cannot reach the queue database ({ex}); is Postgres up (`make db-up`)?")
        return EXIT_CONFIG

    try:
        return args.fn(conn, args)
    except (RepairError, StartupError) as ex:
        _err(str(ex))
        return EXIT_CONFIG
    except KeyboardInterrupt:
        _err("interrupted — nothing was proposed or applied; re-run when ready.")
        return EXIT_INTERRUPTED
    except Exception as ex:  # noqa: BLE001 — an honest failure; class name only, never str(ex):
        # a query failure quotes the statement, and these statements carry page paths.
        _err(f"the command failed ({ex.__class__.__name__}) — see job_runs for this run's "
            f"recorded outcome.")
        return EXIT_ERROR
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
