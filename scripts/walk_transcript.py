"""Manual walk of a TRANSCRIPT through the one pipe, end to end against the real local stack.

Not a test: a narrated walk the way an operator meets it — drop a transcript, watch the librarian
claim it like any other capture, read the filed report, then drop a transcript naming two entities
the registry has never heard of and watch the librarian INTRODUCE them and file anyway. Real
Postgres, real git, real gates, the offline double for the agent.

**A transcript takes the same road as a note.** `kind="meeting"` chooses the brief, the 1 MB cap
and the `sources/meetings/` folder its material is archived to — never a different flow. What is
distilled out of it is ordinary `wiki/` pages, each citing that archive.

Nothing parks and nobody is asked a question, before or after. An unknown name becomes an entity
page born CONFIRMED by the person whose capture introduced it, written into the SAME commit as the
pages, and the capture ends `filed`. The last step reads the identities and their approver out of a
clone of the remote the walk just pushed to.

Run: `.venv/bin/python scripts/walk_transcript.py` from the repo root, after `make db-up`.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from stigmergy.capture import schema  # noqa: E402
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
UNREGISTERED_TRANSCRIPT = """DOUBLE:propose=Nebula Systems|organization,Project Kestrel|project
[00:00] Dana: We reviewed the Nebula Systems opportunity and the new internal codename,
Project Kestrel, needs its own tracking going forward.
"""


def main() -> int:
    conn = testdb.connect_or_skip("walk_transcript")
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

        step("the librarian claims it — the same flow every capture takes")
        item, result = worker.process_next(conn, deps)
        show("status", result.status)
        print(result.report["summary"])
        assert result.status == schema.FILED, f"expected filed, got {result.status}"

        step("what it filed, read back from git — the archive and the pages that cite it")
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

        step("ONE commit: the pages AND the identities introduced to carry them")
        sha2 = result2.result_ref.rsplit("@", 1)[1]
        for path in support.changed_paths(env.repo, sha2):
            print(f"  {path}")
        registry = json.loads(support.read_filed_page(env.repo, sha2,
                                                      generator.REGISTRY_RELPATH))
        print(f"\n  {generator.REGISTRY_RELPATH}, regenerated in that same commit:")
        for entity_id, entry in sorted(registry["entities"].items()):
            approver = entry["approved_by"] or "(before approvals were recorded)"
            print(f"    {entity_id:<18} {entry['name']:<18} {entry['type']:<13} "
                  f"introduced by {approver}")

        step("the identities, read out of a clone of the remote it was pushed to")
        clone = os.path.join(tmp, "clone")
        gitcmd.run("clone", "--quiet", env.bare, clone)
        born = [e for e in generator.read_entity_pages(clone)
                if e.canonical_id in ("nebula-systems", "project-kestrel")]
        for entity in born:
            print(f"  identity  {entity.canonical_id:<18} {entity.name:<18} {entity.relpath}")
            print(f"            approved_by: {entity.approved_by!r}   "
                  f"<- the capture is the approval; nobody was asked")
        assert sorted(e.canonical_id for e in born) == ["nebula-systems", "project-kestrel"], (
            f"expected both identities born, got {[e.canonical_id for e in born]}")
        assert all(e.approved_by for e in born), "an identity was born confirmed by nobody"

        print(f"\n{'=' * 78}\nTranscript walk complete — {STEP} step(s): both captures filed "
              f"through the one pipe, {len(born)} identities introduced, nothing waiting on "
              f"anybody.\n{'=' * 78}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
