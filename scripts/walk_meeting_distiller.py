"""Manual walk of the meeting distiller, driven end to end against the real local stack.

Not a test: a narrated walk the way an operator meets it — drop a transcript, watch the meeting
flow claim it, read the filed page-set report, then drop a transcript naming two entities the
registry has never heard of and watch the librarian PROPOSE them and file the set anyway. Real
Postgres, real git, real gates, the offline double for the agent.

Nothing parks and nobody is asked a question. An unknown name becomes an entity page with
`approved_by` EMPTY — that empty string IS the proposal mark — plus a `proposed` registry entry,
written into the SAME commit as the meeting's pages, and the capture ends `filed`. A steward
confirms, merges or declines the identity afterwards; the last step prints exactly what they would
see, read out of a clone of the remote the walk just pushed to.

Run: `.venv/bin/python scripts/walk_meeting_distiller.py` from the repo root, after `make db-up`.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from stigmergy.capture import schema  # noqa: E402
from stigmergy.entities import cli as entities_cli  # noqa: E402
from stigmergy.entities import generator  # noqa: E402
from stigmergy.librarian import gitcmd, worker  # noqa: E402
from tests import testdb  # noqa: E402
from tests.librarian import support  # noqa: E402

STEP = 0


def step(title):
    global STEP
    STEP += 1
    print(f"\n{'=' * 78}\nSTEP {STEP} — {title}\n{'=' * 78}")


def show(label, value):
    print(f"  {label}: {value}")


TRANSCRIPT = """[00:00] Dana: Let's lock the Q3 pricing floor for Acme Corp's renewal.
[00:04] Alice: Agreed — and the new renewal-terms clause should apply to every client, not
just Acme.
[00:09] Priya: Good. Ship both as decisions from this call.
"""


# The offline double has no NLP: it reads only the explicit `DOUBLE:` directive, never the prose.
# A real agent reaches the same proposal from the transcript alone — the fixture registry knows
# only Acme Corp, and each decision this call took is about one of the two names below.
UNREGISTERED_TRANSCRIPT = """DOUBLE:meeting-propose=Nebula Systems,Project Kestrel
[00:00] Dana: We reviewed the Nebula Systems opportunity and the new internal codename,
Project Kestrel, needs its own tracking going forward.
"""


def main() -> int:
    conn = testdb.connect_or_skip("walk_meeting_distiller")
    schema.ensure_capture_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_queue")

    with tempfile.TemporaryDirectory() as tmp:
        env = support.build_repo(os.path.join(tmp, "git"))
        settings = support.build_settings(env, worktree_root=os.path.join(tmp, "worktrees"))
        deps = support.build_deps(env, settings)

        step("drop a transcript naming two decisions (brain_submit kind=meeting, simulated)")
        ack = support.submit_meeting(conn, deps, TRANSCRIPT, title="Q3 Pricing Sync",
                                     meeting_date="2026-07-29", attendees="Dana, Alice")
        show("queued", f"#{ack['id']} (kind={schema.MEETING!r})")

        step("the librarian's meeting flow claims it")
        item, result = worker.process_next(conn, deps)
        show("status", result.status)
        print(result.report["summary"])
        assert result.status == schema.FILED, f"expected filed, got {result.status}"

        step("the filed page set, read back from git")
        sha = result.result_ref.rsplit("@", 1)[1]
        for path in support.changed_paths(env.repo, sha):
            print(f"  {path}")

        step("drop a second transcript naming two entities the registry does not know")
        support.submit_meeting(conn, deps, UNREGISTERED_TRANSCRIPT, title="Pipeline Review",
                               meeting_date="2026-07-30")
        item2, result2 = worker.process_next(conn, deps)
        show("status", result2.status)
        print(result2.report["summary"])
        assert result2.status == schema.FILED, f"expected filed, got {result2.status}"

        step("ONE commit: the meeting's pages AND the identities it proposed to carry them")
        sha2 = result2.result_ref.rsplit("@", 1)[1]
        for path in support.changed_paths(env.repo, sha2):
            print(f"  {path}")
        registry = json.loads(support.read_filed_page(env.repo, sha2,
                                                      generator.REGISTRY_RELPATH))
        print(f"\n  {generator.REGISTRY_RELPATH}, regenerated in that same commit:")
        for entity_id, entry in sorted(registry["entities"].items()):
            mark = "PROPOSED — waiting on a steward" if entry["proposed"] else "registered"
            print(f"    {entity_id:<18} {entry['name']:<18} {entry['type']:<13} {mark}")

        step("what a steward has to decide, out of a clone of the remote it was pushed to")
        steward = os.path.join(tmp, "steward")
        gitcmd.run("clone", "--quiet", env.bare, steward)
        pending = entities_cli.pending_in(steward)
        for proposal in pending["entities"]:
            page = os.path.join(steward, *proposal["page"].split("/"))
            with open(page, encoding="utf-8") as f:
                mark = next(line for line in f
                            if line.startswith(f"{generator.APPROVED_BY_KEY}:"))
            print(f"  identity  {proposal['id']:<18} {proposal['name']:<18} {proposal['page']}")
            print(f"            {mark.strip()}   <- empty: nobody has confirmed this name yet")
        for spelling in pending["aliases"]:
            print(f"  spelling  {spelling['alias']!r} for {spelling['entity_name']}")
        print("\n  stigmergy-entities approve <id> | merge <id> --into <id> | decline <id>")
        assert [p["id"] for p in pending["entities"]] == ["nebula-systems", "project-kestrel"], (
            f"expected both identities proposed, got {pending['entities']}")

        print(f"\n{'=' * 78}\nMeeting-distiller walk complete — {STEP} step(s): both sets filed, "
              f"{len(pending['entities'])} identities proposed, nothing parked.\n{'=' * 78}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
