"""`stigmergy-entities` — governed entity birth: list · show · approve · reject · create ·
regenerate. Each subcommand is a thin skin over the library, in `stigmergy-queue`'s dialect
(exit 130 on Ctrl-C, `--json` emitting the machine value first, shared renderings imported —
`format_age`, `_clean` — never re-implemented).

Everything `show` prints about a capture is UNTRUSTED: every value crosses `capture.cli._clean`
(the same seam `stigmergy-queue show` uses — two renderers disagreeing about trust is how one
ends up wrong), and the suggested approve command is built from a name only when `_suggestable`
allows it, quoted even then; otherwise the name is printed on its own inert line and the steward
types `--name` themselves. `reject` and `--requeue` ride `capture.dispositions`' own seams — a
second triage->rejected path would be a second set of state guards to keep in agreement.

Exit codes: 0 the command did what it said; 1 it refused (a collision, a dirty clone, a
non-situation row) or `--check` found drift; 2 the TOOL could not run (no repo, no database).

Order of operations in `approve` — a correctness property: the queue row is requeued only AFTER
the push lands, because a requeue that ran first would hand the librarian a capture whose entity
is not yet on the remote it fetches from, and the capture would park a second time.
"""
import argparse
import json
import os
import shlex
import sys

from stigmergy.capture import decisions, dispositions, schema
from stigmergy.capture.cli import _clean, format_age
from stigmergy.capture.errors import CaptureError
from stigmergy.entities import birth, clone, generator, situations
from stigmergy.entities import mint as mint_lib
from stigmergy.entities.errors import EntityError
from stigmergy.index import store
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian.errors import LibrarianError
from stigmergy.review_kinds import KIND_ENTITY_PROPOSAL

_DUMP = {"ensure_ascii": False, "indent": 2}

EXIT_REFUSED = 1
EXIT_CANNOT_RUN = 2
EXIT_INTERRUPTED = 130


def _repo(args) -> str:
    """The steward's clone. Same env var and default as the librarian's `--repo` — it is the
    same checkout, told to one tool once."""
    repo = args.repo or os.environ.get(librarian_config.REPO_ENV) or librarian_config.REPO_DEFAULT
    path = os.path.abspath(repo)
    if not os.path.isdir(os.path.join(path, ".git")):
        raise EntityError(
            f"{path} is not a git checkout — `--repo` (or ${librarian_config.REPO_ENV}) must point "
            f"at your clone of the knowledge repo, because every command here commits to it with "
            f"your own git identity")
    return path


def _connect(args):
    conn = store.connect(args.dsn)
    schema.ensure_capture_schema(conn)
    # The governance ledger too, the same startup pattern every other entry point that writes it
    # already follows (`server.service`, `transport_http`, `admin.routes`, the two cron CLIs).
    # Without it, `approve` on a database no server has ever started against would mint, push, and
    # then fail on the INSERT — after the irreversible half.
    decisions.ensure_decisions_schema(conn)
    return conn


def _aliases(values) -> list[str]:
    """`--aliases "A, B" --aliases C` -> `["A", "B", "C"]`. Repeatable AND comma-separated,
    because a steward types this once, by hand, next to a name that may itself contain spaces."""
    out = []
    for value in values or ():
        out += [part.strip() for part in str(value).split(",") if part.strip()]
    return out


# ── list / show ───────────────────────────────────────────────────────────────────────────────
def _cmd_list(conn, args) -> int:
    rows = situations.list_pending_situations(conn, limit=args.limit)
    if args.json:
        print(json.dumps(rows, **_DUMP, default=str))
        return 0
    if not rows:
        print("no pending entity situations — nothing is parked on an identity decision")
        return 0
    print(f"{len(rows)} pending entity situation(s)\n")
    for row in rows:
        subject = f'"{row["subject"]}"' if row["subject"] else "(nothing recorded)"
        asked = "asked" if row.get("asked_at") else ""
        print(f"  #{row['id']:<5} {row['situation']:<18} {subject:<34} {asked:<6} "
              f"parked {format_age(row.get('parked_age_ms'))}")
    print("\n  stigmergy-entities show <id>   the material, the agent's reading and the next command")
    return 0


# An ALLOW-list, never a deny-list: a deny-list of shell metacharacters must be kept complete
# forever, against a value that arrives from captured material. `str.isalnum()` lets accents and
# non-Latin scripts pass; the punctuation a real entity name needs is enumerated. `'` is
# deliberately absent: `shlex.quote` renders it `'L'"'"'Oreal'`, and a command a steward cannot
# read is a command they retype wrong.
_SUGGESTABLE_PUNCTUATION = frozenset(" .,&+-")
MAX_SUBJECT_CHARS = 120


def _cmd_show(conn, args) -> int:
    row = situations.get_situation(conn, args.id)
    if row is None:
        raise EntityError(f"submission {args.id} does not exist")
    if args.json:
        print(json.dumps(row, **_DUMP, default=str))
        return 0
    report = row.get("report") or {}
    situation, subject = row["situation"], row["subject"]
    # A row parked before `schema.SITUATION_KEY` existed records no subject at all; rendering `""`
    # would read as a bug in the tool. `open_question` is what those rows do carry, so it stands in.
    legacy = _clean(str(report.get("open_question") or "").strip(), 300)
    shown = _clean(subject, MAX_SUBJECT_CHARS)
    if situation == schema.SITUATION_UNRESOLVED_ENTITY:
        headline = (f'could not resolve the entity "{shown}"' if shown else
                    "could not resolve which entity the material is about")
    elif situation == schema.SITUATION_UNSUPPORTED_TYPE:
        headline = (f'parked as an unsupported type ("{shown}")' if shown else
                    "parked as a type the fast lane does not file"
                    + (f" — the librarian asked: {legacy}" if legacy else ""))
    else:
        headline = f"parked in {row['status']!r}, and not as an identity question"
    print(f"capture #{row['id']} — {headline}")
    print(f"  submitted by: {row['submitted_by']}, {row['created_at']} "
          f"(parked {format_age(row.get('parked_age_ms'))})")
    # Every value below was written from material this system did not author (module docstring).
    if report.get("agent_rationale"):
        print(f"  agent's reading: \"{_clean(report['agent_rationale'], 300)}\"")
    if row.get("hints"):
        print(f"  hint on the capture: {_clean(row['hints'], 200)}")
    if row.get("asked_at"):
        print(f"  asked the submitter: {row['asked_at']}"
              + (f" — they answered: \"{_clean(row['reply'], 500)}\"" if row.get("reply")
                 else " — no answer yet"))
    if row.get("excerpt"):
        print(f"  material: {_clean(row['excerpt'], 500)}")
    if row.get("withheld_reason"):
        print(f"  material: {_clean(row['withheld_reason'], 200)}")
    if not situation:
        return 0
    # The fallback cannot reach `subject_of`'s joined display string: that join runs only when
    # `subjects_of` answered something, and this `or` fires only when it answered `[]`. What it
    # reaches is the row's raw singular `SITUATION_NAME_KEY`, verbatim — pinned in
    # `tests/entities/test_situations.py`, because every entry of this list is pasted into a
    # printed `--name` a human is invited to run.
    _print_next_commands(row["id"], situation, row.get("subjects") or [subject])
    return 0


def _suggestable(name: str) -> bool:
    """Whether `name` may be pasted into a printed command at all.

    Not "whether it can be quoted" — `shlex.quote` can quote anything. The question is whether a
    human reading the line sees the same arguments the shell will parse. No WORD may start with
    `-` (a quoted `Acme --aliases <name>` is safe to RUN and still reads as three arguments; no
    real entity is called `-anything`). `schema.UNNAMED_ENTITY_PLACEHOLDER` is refused by VALUE:
    it is syntactically an ordinary name the librarian falls back to when nothing was named, and
    suggesting it ready-to-run would mint a garbage entity that then resolves for every future
    capture mentioning it.
    """
    value = str(name or "").strip()
    if not value or len(value) > MAX_SUBJECT_CHARS:
        return False
    if value == schema.UNNAMED_ENTITY_PLACEHOLDER:
        return False
    if any(word.startswith("-") for word in value.split()):
        return False
    return all(char.isalnum() or char in _SUGGESTABLE_PUNCTUATION for char in value)


def _print_next_commands(submission_id: int, situation: str, subjects: list) -> None:
    """The exact next command(s) — built only from values that are safe to print as one.

    A message containing a command is a promise: the flags printed are the flags `birth.prepare`
    accepts, including the derived `--id`, and running the line must do what it reads as doing.
    A name that cannot carry that promise is printed on its own inert line and the command becomes
    a template with `--name` left for the steward. `subjects` is a LIST (several, for an ordinary
    or meeting park naming more than one unresolved entity): each name gets its own block, checked
    against `_suggestable` INDEPENDENTLY, so a sibling unsafe name never blocks the others; only
    the last call passes `--requeue`, since `approve` requeues the whole submission (one row, not
    one per name).
    """
    unresolved = situation == schema.SITUATION_UNRESOLVED_ENTITY
    types = f"--type <{'|'.join(birth.ENTITY_TYPES)}>"
    tail = "--aliases \"...\" [--role \"...\"] [--requeue]"
    subjects = list(subjects) or [""]
    multi = unresolved and len(subjects) > 1

    if not unresolved:
        # `unsupported-type`: the subject is a TYPE, not a name — nothing untrusted reaches the line.
        print("\n  to approve it as a new entity:")
        print(f"    stigmergy-entities approve {submission_id} --id <canonical-id> "
              f"--name \"<Entity Name>\" {types} {tail}")
        print("  to decline it:")
        print(f"    stigmergy-entities reject {submission_id} --reason \"...\"")
        return

    for name in subjects:
        label = f' "{_clean(name, MAX_SUBJECT_CHARS)}"' if multi else ""
        if not _suggestable(name):
            print(f"\n  the name{label} on this capture cannot be put into a command safely — it "
                  f"came from the\n  captured material and contains characters a shell would act "
                  f"on. It is printed\n  here as plain text; read it, then type the --name you "
                  f"decide on yourself:\n")
            print(f"    {_clean(name, MAX_SUBJECT_CHARS)}")
            print(f"\n  to approve{label or ' it'} as a new entity:")
            print(f"    stigmergy-entities approve {submission_id} --id <canonical-id> "
                  f"--name \"<Entity Name>\" {types} {tail}")
        else:
            print(f"\n  to approve{label or ' it'} as a new entity:")
            print(f"    stigmergy-entities approve {submission_id} "
                  f"--id {generator.canonical_id_for(name)} --name {shlex.quote(name)} "
                  f"{types} {tail}")
    if multi:
        print("\n  (approve each name above separately; only the LAST call needs --requeue — "
              "there is one submission, not one per name)")
    print("\n  to decline the whole capture:" if multi else "  to decline it:")
    print(f"    stigmergy-entities reject {submission_id} --reason \"...\"")


# ── the birth path: approve / create ───────────────────────────────────────────────────────────
def _mint(repo: str, args, *, submission_id: int | None, on_output) -> dict:
    """A thin adapter over the shared `entities.mint.mint`: resolves the STEWARD's own identity
    from this clone's git config (`clone.preflight`) and hands it in as `author`. Every mint
    discipline lives in `mint()` itself, shared with the server-driven door so the two can never
    silently drift apart.
    """
    branch = args.branch
    action = "approve" if submission_id else "create"
    author = clone.preflight(repo, branch, action=action)
    return mint_lib.mint(
        repo, entity_id=args.entity_id, name=args.name, entity_type=args.type,
        aliases=_aliases(args.aliases), role=args.role or "", branch=branch, today=args.today,
        author=author, submission_id=submission_id, on_output=on_output)


def _print_birth(result: dict, *, verb: str) -> None:
    print(f"{verb} — created {result['page']} ({result['entity_type']}), regenerated "
          f"{result['registry']}")
    print(f"  committed as {result['commit'][:12]} (steward: {result['steward']}), pushed to "
          f"{result['branch']}")


def _cmd_approve(conn, args) -> int:
    repo = _repo(args)
    row = situations.require_situation(conn, args.id, action="approve")
    result = _mint(repo, args, submission_id=args.id,
                   on_output=lambda line: print(line, file=sys.stderr))
    # AFTER the push, like the requeue below and for the same reason: a ledger row for a mint that
    # then failed to land would claim an identity exists that nothing can resolve. Attribution is
    # `--by` or this clone's git identity — the same value the commit is authored with, so the two
    # records of one approval name one person (ADR 030 D2: attributed here, enforced on MCP).
    decisions.record_decision(
        conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(args.id), verdict=decisions.APPROVE,
        actor=args.by or result["steward"],
        extra={"entity_id": result["entity_id"], "commit": result["commit"], "door": "cli"})
    requeued = None
    if args.requeue:
        # AFTER the push, never before (module docstring). Through the drain's own seam, so the
        # state guard, the trace event and the `attempts` invariant are `stigmergy-queue requeue`'s.
        requeued = dispositions.requeue(
            conn, args.id, actor=args.by or result["steward"],
            note=f"entity {result['entity_id']} approved and pushed ({result['commit'][:12]})")
    payload = {**result, "submission_id": args.id,
               "requeued": bool(requeued), "was": row["status"]}
    if args.json:
        print(json.dumps(payload, **_DUMP))
        return 0
    _print_birth(result, verb="approved")
    if requeued:
        print(f"  capture #{args.id} requeued — the librarian will re-file it anchored to "
              f"{result['name']} on its next claim (attempts unchanged at {requeued['attempts']})")
    else:
        print(f"  capture #{args.id} was NOT requeued — it is still parked in "
              f"{schema.TRIAGE!r}. Re-run with --requeue, or `stigmergy-queue requeue {args.id} "
              f"--by <who>`, to send it back to the librarian")
    return 0


def _cmd_create(conn, args) -> int:
    result = _mint(_repo(args), args, submission_id=None,
                   on_output=lambda line: print(line, file=sys.stderr))
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    _print_birth(result, verb="created")
    return 0


def _cmd_reject(conn, args) -> int:
    situations.require_situation(conn, args.id, action="reject")
    actor = args.by or _steward(args)
    result = dispositions.reject(conn, args.id, actor=actor, reason=args.reason)
    # Refusing an identity is as much a governance decision as granting one, and the console
    # already records its own Reject for exactly that reason (`AdminService.queue_reject`). Left
    # out, this door would close the approve half of the gap and keep the reject half — and
    # "who decided this identity" would still answer from different tables depending on the
    # verdict. `require_situation` above has already established this row IS an entity situation.
    decisions.record_decision(conn, item_kind=KIND_ENTITY_PROPOSAL, item_id=str(args.id),
                              verdict=decisions.REJECT, actor=actor, notes=args.reason,
                              extra={"door": "cli"})
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    print(f"rejected #{result['id']} — reason recorded in the submitter's report; no entity was "
          f"created and nothing was committed")
    return 0


def _steward(args) -> str:
    """Who is answering, defaulting to the clone's own git identity — the signature this tool is
    premised on. Still overridable: attribution, not authorization."""
    name, email = clone.identity(_repo(args))
    return f"{name} <{email}>"


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
    # Written locally and NOT committed: `approve`/`create` are the one governed push path here,
    # and a self-pushing `regenerate` would be a second writer to `main` with different safety
    # properties. Drift is also a disagreement a human should look at before publishing the
    # resolution.
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
        description="Governed entity birth: propose -> a steward approves -> the "
                    "registry regenerates, in one commit signed by that steward.")
    ap.add_argument("--dsn", default=None,
                    help=f"Postgres DSN (default: ${store.DSN_ENV} or {store.DSN_DEFAULT})")
    ap.add_argument("--repo", default=None,
                    help=f"your clone of the knowledge repo (default: "
                         f"${librarian_config.REPO_ENV} or {librarian_config.REPO_DEFAULT})")
    ap.add_argument("--branch", default="main", help="branch to commit to (default: main)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="parked rows waiting on an identity decision")
    p_list.add_argument("--limit", type=int, default=situations.DEFAULT_LIST_LIMIT)
    p_list.set_defaults(fn=_cmd_list, needs_db=True)

    p_show = sub.add_parser("show", help="one situation, and the exact command that approves it")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(fn=_cmd_show, needs_db=True)

    p_approve = sub.add_parser(
        "approve", help="mint the entity, regenerate the registry, commit both as YOU, push")
    p_approve.add_argument("id", type=int, help="the parked capture this identity comes from")
    p_approve.add_argument("--requeue", action="store_true",
                           help="send the capture back to the librarian once the push lands, so it "
                                "re-files anchored to the entity you just created")
    p_approve.add_argument("--by", default=None,
                           help="who is answering for the requeue (default: your git identity). "
                                "Attribution, not authorization")
    p_approve.set_defaults(fn=_cmd_approve, needs_db=True)

    p_create = sub.add_parser(
        "create", help="the same birth with no capture behind it (a steward registering an entity "
                       "nobody has submitted about yet)")
    p_create.set_defaults(fn=_cmd_create, needs_db=False)

    for parser in (p_approve, p_create):
        # `dest="entity_id"`, NOT the default `id`: `approve` already has a POSITIONAL `id` (the
        # queue row), and argparse would resolve both to one attribute — `--id globex-corp` would
        # silently overwrite the submission id and the queue lookup would chase "globex-corp".
        parser.add_argument("--id", dest="entity_id", required=True,
                            help="the canonical registry id. It must be the slug of --name: the "
                                 "registry is DERIVED from the pages, so an id nothing regenerates "
                                 "would vanish at the next regenerate. Typed rather than inferred "
                                 "because approving an identity is the gesture being recorded")
        parser.add_argument("--name", required=True,
                            help="the entity's name — its page title, its filename and the "
                                 "wikilink every other page resolves it by")
        parser.add_argument("--type", required=True, choices=birth.ENTITY_TYPES,
                            help="the page's `entity_type` and the registry's `type`")
        parser.add_argument("--aliases", action="append", default=[],
                            help="other spellings that mean this entity (comma-separated, "
                                 "repeatable). Every alias silently reassigns mentions to it, so "
                                 "an alias that collides with another entity is refused")
        parser.add_argument("--role", default="",
                            help="one line on what this entity is, for the page's `role` field")
        parser.add_argument("--today", default=None,
                            help=argparse.SUPPRESS)   # injectable clock: `created`/`updated`

    p_reject = sub.add_parser(
        "reject", help="decline the identity — closes the capture as `rejected`, attributed")
    p_reject.add_argument("id", type=int)
    p_reject.add_argument("--reason", required=True,
                          help="why, in the SUBMITTER's own report, verbatim — never include a "
                               "secret or personal data here")
    p_reject.add_argument("--by", default=None,
                          help="who is answering for it (default: your git identity)")
    p_reject.set_defaults(fn=_cmd_reject, needs_db=True)

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
          f"which. Nothing was written to the queue", file=sys.stderr)
    return EXIT_INTERRUPTED


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.today = getattr(args, "today", None) or __import__("datetime").date.today().isoformat()
    conn = None
    try:
        if getattr(args, "needs_db", False):
            conn = _connect(args)
    except KeyboardInterrupt:
        return _interrupted(args.command)
    except Exception as ex:  # noqa: BLE001 — a local operator needs the real reason
        print(f"stigmergy-entities: cannot reach the queue database ({ex}); is Postgres up "
              f"(`make db-up`)?", file=sys.stderr)
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
