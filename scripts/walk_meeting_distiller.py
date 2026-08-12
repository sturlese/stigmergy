"""Manual walk of the meeting distiller, driven end to end against the real local stack.

Not a test: a narrated walk the way an operator meets it — drop a transcript, watch the meeting
flow claim it, read the filed page-set report, then drop a transcript naming an unregistered entity
and watch it park. Real Postgres, real git, real gates, the offline double for the agent.

Run: `.venv/bin/python scripts/walk_meeting_distiller.py` from the repo root, after `make db-up`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from stigmergy.capture import schema  # noqa: E402
from stigmergy.librarian import worker  # noqa: E402
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
# A real agent reaches the same park from the transcript alone.
UNRESOLVED_TRANSCRIPT = """DOUBLE:meeting-triage=Nebula Systems,Project Kestrel
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

        step("drop a transcript naming two decisions (stigmergy-meeting drop, simulated)")
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

        step("drop a second transcript naming an unregistered entity")
        support.submit_meeting(conn, deps, UNRESOLVED_TRANSCRIPT, title="Pipeline Review",
                               meeting_date="2026-07-30")
        item2, result2 = worker.process_next(conn, deps)
        show("status", result2.status)
        print(result2.report["summary"])
        assert result2.status in (schema.NEEDS_INPUT, schema.TRIAGE), (
            f"expected needs_input/triage, got {result2.status}")

        print(f"\n{'=' * 78}\nMeeting-distiller walk complete — {STEP} step(s), both outcomes reached.\n"
              f"{'=' * 78}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
