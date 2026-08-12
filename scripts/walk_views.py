"""Manual walk of the view flow, driven end to end against the real local stack.

Not a test: a narrated walk the way an operator meets it — drop a meeting transcript, watch the
librarian file it and regenerate the touched entity's view in the same run (the post-filing
trigger); read the committed view back from git; then run `stigmergy-views regenerate --entity <id>`
by hand and watch the honest no-op. Real Postgres, real git, real gates, the offline double.

It proves the MECHANISM only — skeleton, synthesis, commit, trigger, no-op. Whether a live `ask`
prefers the view is the operator's judgment against a real corpus and stays a hand step.

Run: `.venv/bin/python scripts/walk_views.py` from the repo root, after `make db-up`.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

from stigmergy.capture import schema  # noqa: E402
from stigmergy.kernel import registry as registry_module  # noqa: E402
from stigmergy.librarian import gitcmd, worker  # noqa: E402
from stigmergy.views import regenerate  # noqa: E402
from tests import testdb  # noqa: E402
from tests.librarian import support  # noqa: E402

os.environ.setdefault("CLEAN_LLM", "fake")

STEP = 0


def step(title):
    global STEP
    STEP += 1
    print(f"\n{'=' * 78}\nSTEP {STEP} — {title}\n{'=' * 78}")


def show(label, value):
    print(f"  {label}: {value}")


TRANSCRIPT = """[00:00] Dana: Let's lock the Q3 pricing floor for Acme Corp's renewal.
[00:04] Alice: Agreed — ship it as a decision from this call.
"""


def main() -> int:
    conn = testdb.connect_or_skip("walk_views")
    schema.ensure_capture_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs WHERE job LIKE 'views%'")

    with tempfile.TemporaryDirectory() as tmp:
        env = support.build_repo(os.path.join(tmp, "git"))
        settings = support.build_settings(env, worktree_root=os.path.join(tmp, "worktrees"))
        deps = support.build_deps(env, settings)

        step("drop a meeting transcript naming Acme Corp (stigmergy-meeting drop, simulated)")
        ack = support.submit_meeting(conn, deps, TRANSCRIPT, title="Q3 Pricing Sync",
                                     meeting_date="2026-07-29", attendees="Dana, Alice")
        show("queued", f"#{ack['id']} (kind={schema.MEETING!r})")

        step("the librarian's meeting flow files it, AND regenerates the touched view "
            "in the same run (the post-filing trigger)")
        item, result = worker.process_next(conn, deps)
        show("status", result.status)
        assert result.status == schema.FILED, f"expected filed, got {result.status}"
        print(result.report["summary"])

        step("job_runs carries a row for the view trigger")
        with conn.cursor() as cur:
            cur.execute("SELECT job, status, stats FROM job_runs WHERE job LIKE 'views%' "
                       "ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        assert row is not None, "no views job_runs row — trigger 1 did not fire"
        show("job_runs row", row)

        step("the committed view, read back from the bare remote (env.repo's own working "
            "checkout never moved — the worker committed from its OWN ephemeral worktree, "
            "pushed straight to the remote, same as every fast-lane/meeting commit)")
        registry = registry_module.load_registry(settings.registry_path)
        entity_id = next(iter(registry.entities))          # the one seeded entity, "acme-corp"
        relpath = regenerate.view_relpath(entity_id)
        page = gitcmd.run("show", f"{settings.branch}:{relpath}", cwd=env.bare).stdout
        assert page, f"{relpath} was never committed to {env.bare}"
        print(page)

        step("trigger 2 — the operator CLI (stigmergy-views regenerate --entity), by hand, "
            "from a steward's clone freshly synced with what the worker just pushed: an honest "
            "no-op, since the worker already regenerated it")
        gitcmd.run("fetch", "origin", settings.branch, cwd=env.repo)
        gitcmd.run("reset", "--hard", f"origin/{settings.branch}", cwd=env.repo)
        outcome = asyncio.run(regenerate.regenerate_entity(
            env.repo, entity_id, registry=registry, branch=settings.branch))
        show("action", outcome.action)
        assert outcome.action == "unchanged", f"expected unchanged, got {outcome.action}"

        print(f"\n{'=' * 78}\nView walk complete — {STEP} step(s). Mechanism proven offline; the "
             f"live 'ask what do we know about {entity_id}?' judgment against the real "
             f"corpus stays a hand step (see this script's module "
             f"docstring).\n{'=' * 78}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
