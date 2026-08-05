"""`stigmergy-entities` — governed entity birth, and the registry's derived view.

Six subcommands, each a thin skin over the library (nothing CLI-only — the same discipline
`stigmergy-queue` and `stigmergy-librarian` follow):

    stigmergy-entities list                  what is parked on an identity decision
    stigmergy-entities show <queue-id>       one situation: the material, the agent's reading, the name
    stigmergy-entities approve <queue-id>    mint the entity, regenerate the registry, ONE commit, push
    stigmergy-entities reject <queue-id>     decline it — reuses the steward drain's own transition
    stigmergy-entities create                the same birth with no capture behind it
    stigmergy-entities regenerate [--check]  the derived view, rebuilt or verified

Conventions are `stigmergy-queue`'s, because the three tools sit side by side in one operator's
terminal and must not speak different dialects: exit 130 on Ctrl-C, `--json` emitting the
machine-readable value FIRST, local and specific errors, and every shared rendering IMPORTED
(`format_age`, `_clean`) rather than re-implemented.

**Everything `show` prints about a capture is UNTRUSTED and is treated as such.** The
excerpt, the agent's rationale, the submitter's reply, the hint and the unresolved name are all
written from material somebody else wrote, and they land on a terminal that holds this operator's
push identity for `main`, the queue DSN and — on the operator machine — the App key path. Two
consequences, and they are different sizes:

- every one of those values crosses `capture.cli._clean` on the way out, the same seam
  `stigmergy-queue show` puts them through. Two renderers of one value disagreeing about whether it
  is untrusted is how one of them ends up being the wrong one;
- and `show`'s **suggested approve command is not built from any of them unless the name is safe
  to put in a shell**. A printed command is an invitation to paste, `$(…)` inside double quotes
  executes where it is pasted, and a name spelled `Acme" --aliases "<a registered name>` reads to
  a human as one argument while a shell reads it as three. So the name is checked against an
  ALLOW-list (`_suggestable`), quoted with `shlex.quote` even then, and when it fails the check the
  tool prints the name on its own inert line and tells the steward to type `--name` themselves —
  refusing to suggest rather than suggesting something quoted-but-unreadable.

**`reject` does not write a transition.** It calls `capture.dispositions.reject`, the seam the
drain already owns — a second path from `triage` to `rejected` would be a second set of state
guards to keep in agreement with the first, and this CLI's judgment about an identity is not a
different kind of rejection from a steward's judgment about anything else. Same for `--requeue`,
which is `dispositions.requeue` and nothing more.

**Exit codes.** 0 when the command did what it said; 1 when it refused (a collision, a dirty
clone, a row that is not a situation) or when `--check` found drift; 2 when the TOOL could not
run (no repo, no database). A refusal is not a crash and drift is not a bug — but both are
non-zero, because both mean the thing the operator asked for did not happen.

**Order of operations in `approve`, which is a correctness property and not a style choice:** the
queue row is only requeued AFTER the push lands. A requeue that ran first would hand the librarian
a capture whose entity is not yet on the remote it fetches from, and the capture would park a
second time — the full park-approve-refile circle failing for a reason that has nothing to do with
the circle.
"""
import argparse
import json
import os
import shlex
import sys

from stigmergy.capture import dispositions, schema
from stigmergy.capture.cli import _clean, format_age
from stigmergy.capture.errors import CaptureError
from stigmergy.entities import birth, clone, generator, situations
from stigmergy.entities import mint as mint_lib
from stigmergy.entities.errors import EntityError
from stigmergy.index import store
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian.errors import LibrarianError

_DUMP = {"ensure_ascii": False, "indent": 2}

EXIT_REFUSED = 1
EXIT_CANNOT_RUN = 2
EXIT_INTERRUPTED = 130


def _repo(args) -> str:
    """The steward's clone. Same env var and same default as the librarian's `--repo`
    (`librarian.config`), because it is the same checkout — an operator who has already told one
    tool where the knowledge repo is should not have to tell the other."""
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


# The name is offered inside a shell command, so it is checked against an ALLOW-list rather than
# scrubbed of known-bad characters: a deny-list of shell metacharacters is a list somebody has to
# keep complete forever, against a shell that gains meaning for new punctuation, and the value
# being filtered arrives from captured material. Letters and digits are `str.isalnum()`, so accents
# and non-Latin scripts pass; the punctuation a real entity name needs is enumerated. `'` is
# deliberately NOT here: `shlex.quote` handles it correctly but renders it `'L'"'"'Oreal'`, and a
# command a steward cannot read is a command they retype wrong.
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
    # A row parked BEFORE `schema.SITUATION_KEY` existed records no subject at all, and saying so
    # is the honest rendering — `parked as an unsupported type ("")` reads as a bug in the tool
    # rather than as an old row, and sends a steward looking for a value nothing ever wrote.
    # `open_question` is what those rows do carry, so it stands in.
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
    # Every value below was written from material this system did not author. `_clean` is
    # `stigmergy-queue show`'s own seam, imported rather than re-derived (module docstring).
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
    _print_next_commands(row["id"], situation, row.get("subjects") or [subject])
    return 0


def _suggestable(name: str) -> bool:
    """Whether `name` may be pasted into a printed command at all.

    Not "whether it can be quoted" — `shlex.quote` can quote anything. The question is whether a
    human reading the suggested line sees the same arguments the shell will. `Acme" --aliases "<a
    registered name>` quotes safely and still reads to a person as one argument while parsing as
    three, which combines with the alias-collision chain the birth gate exists to close.

    **No WORD may start with `-`**, not merely the name. `Acme" --aliases "<a registered name>`
    survives the source-side filter in `librarian.report` as `Acme --aliases <name>`, which
    `shlex.quote` renders as one correctly quoted argument — safe to RUN, and still a line whose
    reader has to notice the quoting to know that. Refusing it costs nothing real (no entity is
    called `-anything`) and removes the last shape that reads as more arguments than it is.

    **`schema.UNNAMED_ENTITY_PLACEHOLDER` is refused by VALUE, not by shape.**
    `gates._unresolved_name` and `processing._triage` both fall back to this exact word —
    "nothing was named at all" — when a park carries no real name, and it is syntactically an
    ordinary name (letters and a space): every OTHER check here would happily pass it, and
    `_print_next_commands` would suggest `stigmergy-entities approve ... --name "something unnamed"`
    as a ready-to-run command. Run, it mints a garbage entity that then resolves for every future
    capture mentioning it — exactly the failure governed birth exists to prevent, self-inflicted by
    the tool meant to guard against it. The park itself is correct and stands; only the willingness
    to hand THIS one value back as a fillable suggestion is withheld.
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
    """The exact next command(s) — but only ever built from values that are safe to print as one.

    The convention `stigmergy-queue`'s `RECLAIM_NOW` set, under the same obligation: a message
    containing a command is a promise, so the flags printed here are the flags `birth.prepare`
    accepts, including the derived `--id` it would otherwise refuse.

    A promise has a second half, though, which is that running the printed line does what the line
    says and nothing else. When a name cannot carry that promise the tool says so and stops
    filling it in: the name is printed on its own line, where it is inert text rather than shell
    input, and the command becomes a template with `--name` left for the steward. That is strictly
    more useful than a correctly-quoted line nobody can verify by reading — and it is the branch
    any row whose name never passed `librarian.report`'s own identity filter lands in.

    **`subjects` is a LIST** — one entry for the ordinary single-name case, several for a
    meeting park (`entities.situations.subjects_of`). Every name gets its OWN command block,
    printed and checked against `_suggestable` INDEPENDENTLY: a steward approving one name must
    not be blocked because a sibling name also happens to fail `_suggestable`, and vice versa.
    Approving one is `stigmergy-entities approve {submission_id} ...` for EACH name, one call per
    name; only the last one should pass `--requeue`, since `approve` requeues the whole submission
    (there is one row, not one per name) — every command block below says so.
    """
    unresolved = situation == schema.SITUATION_UNRESOLVED_ENTITY
    types = f"--type <{'|'.join(birth.ENTITY_TYPES)}>"
    tail = "--aliases \"...\" [--role \"...\"] [--requeue]"
    subjects = list(subjects) or [""]
    multi = unresolved and len(subjects) > 1

    if not unresolved:
        # `unsupported-type`: the subject is a TYPE, not a name, so there is no name to fill in and
        # nothing untrusted reaches the line at all.
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
    """A thin adapter over the shared `entities.mint.mint` (ADR 030 D4): resolves the STEWARD's own
    identity from this clone's git config (`clone.preflight`, which is also where "your clone has
    no git identity configured" is refused) and hands it in as `author`, along with the parsed
    `args`. Every discipline — drift refusal, resolve-before-mint, the template render, the
    secrets scan, the one commit, the bounded rebase-and-retry — lives in `mint()` itself, shared
    with a server-driven mint (`entities.remote.mint_via_clone`) so the two doors can never
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
    requeued = None
    if args.requeue:
        # AFTER the push, never before (see the module docstring). Through the drain's own seam, so
        # the state guard, the trace event and the `attempts` invariant are the same ones
        # `stigmergy-queue requeue` gets.
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
    result = dispositions.reject(conn, args.id, actor=args.by or _steward(args),
                                 reason=args.reason)
    if args.json:
        print(json.dumps(result, **_DUMP))
        return 0
    print(f"rejected #{result['id']} — reason recorded in the submitter's report; no entity was "
          f"created and nothing was committed")
    return 0


def _steward(args) -> str:
    """Who is answering for this, defaulting to the clone's own git identity.

    Defaulted rather than required, and only here: this tool's whole premise is that the steward's
    git identity is the signature, so asking them to retype it as `--by` would invite a second,
    different answer to a question the clone has already answered. Still overridable — attribution,
    not authorization.
    """
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
    # Written locally and NOT committed, and the line says so. `approve`/`create` are the one
    # governed push path in this subsystem (dirty check, divergence check, steward identity, one
    # commit); a `regenerate` that pushed on its own would be a second writer to `main` with
    # different safety properties. Drift also means the pages and the registry already disagree,
    # which is a thing a human should look at before publishing the resolution.
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
        # `dest="entity_id"`, NOT the default `id` — `approve` already has a POSITIONAL `id` (the
        # queue row), and argparse resolves both to the same attribute, so `--id globex-corp`
        # silently overwrites the submission id and the queue lookup goes looking for a row
        # numbered "globex-corp". Two flags with one meaning is a naming collision; two flags with
        # one DEST is a data collision, and it fails at the far end of the command with a database
        # error about a value the operator never typed there.
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
        # One sentence, no traceback — including for the git faults `librarian.gitcmd` raises,
        # whose stderr it has already scrubbed and truncated. A raw traceback where a person
        # expected a sentence is this project's most-repeated defect.
        print(f"stigmergy-entities: {ex}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        return _interrupted(args.command)
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
