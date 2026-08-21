"""Manual walk of the view flow, driven end to end against the real local stack.

Not a test: a narrated walk the way an operator meets it — drop a meeting transcript, watch the
librarian file it and regenerate the touched entity's view in the same run (the post-filing
hook); read the committed view back from git; run `stigmergy-views regenerate --entity <id>` by
hand and watch the honest no-op; then let the worker's periodic convergence sweep create a view for
an entity NOTHING has a hook for. Real Postgres, real git, real gates, the offline double.

It proves the MECHANISM only — skeleton, synthesis, commit, hook, no-op, convergence. Whether a
live `ask` prefers the view is the operator's judgment against a real corpus and stays a hand
step.

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

        step("drop a meeting transcript naming Acme Corp (brain_submit kind=meeting, simulated)")
        ack = support.submit_meeting(conn, deps, TRANSCRIPT, title="Q3 Pricing Sync",
                                     meeting_date="2026-07-29", attendees="Dana, Alice")
        show("queued", f"#{ack['id']} (kind={schema.MEETING!r})")

        step("the librarian's meeting flow files it, AND regenerates the touched view "
            "in the same run (the post-filing trigger)")
        item, result = worker.process_next(conn, deps)
        show("status", result.status)
        assert result.status == schema.FILED, f"expected filed, got {result.status}"
        print(result.report["summary"])

        step("job_runs carries a row for the post-filing hook")
        with conn.cursor() as cur:
            cur.execute("SELECT job, status, stats FROM job_runs WHERE job LIKE 'views%' "
                       "ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
        assert row is not None, "no views job_runs row — the post-filing hook did not fire"
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

        step("the operator CLI (stigmergy-views regenerate --entity), by hand, "
            "from a steward's clone freshly synced with what the worker just pushed: an honest "
            "no-op, since the worker already regenerated it")
        gitcmd.run("fetch", "origin", settings.branch, cwd=env.repo)
        gitcmd.run("reset", "--hard", f"origin/{settings.branch}", cwd=env.repo)
        outcome = asyncio.run(regenerate.regenerate_entity(
            env.repo, entity_id, registry=registry, branch=settings.branch))
        show("action", outcome.action)
        assert outcome.action == "unchanged", f"expected unchanged, got {outcome.action}"

        step("a page lands with NO hook of any kind — the shape every door except a meeting has "
            "(an ordinary capture, a Slack or Drive drop, an applied repair, a hand edit). "
            "Nothing regenerates anything, and the entity has no view at all")
        second_id = "globex"
        with open(os.path.join(env.repo, "ops", "entity-registry.json"), "w") as f:
            f.write(f'{{"entities": {{"{entity_id}": {{"name": "Acme Corp", "type": "organization", '
                   f'"aliases": []}}, "{second_id}": {{"name": "Globex", "type": "organization", '
                   f'"aliases": []}}}}}}\n')
        with open(os.path.join(env.repo, "wiki", "entities", "Globex.md"), "w") as f:
            f.write(f'---\ntype: entity\ntitle: "Globex"\nentity: [{second_id}]\n'
                   f'status: developing\ncreated: "2026-07-01"\nupdated: "2026-07-01"\n'
                   f'tags: [entity]\n---\n\n# Globex\n\nAn entity nothing has filed a meeting for.\n')
        support.commit_and_push(env.repo, "feat: an entity page filed by a door with no view hook")
        on_remote = gitcmd.run("ls-tree", "--name-only", f"{settings.branch}:views", cwd=env.bare,
                               check=False).stdout.split()
        show("views/ on the remote", ", ".join(on_remote) or "(the tree does not exist yet)")
        assert f"{second_id}.md" not in on_remote, "nothing should have written globex's view yet"

        step("the worker's periodic convergence sweep — the guarantee. It builds its OWN "
            "ephemeral worktree (an idle pass has no capture's to borrow), asks the corpus which "
            "views diverge, and fixes those. NOTE the population: --stale could never have named "
            "globex, because it has no view to be stale")
        result = worker.run_view_sweep(conn, deps)
        show("stats", result.stats)
        assert result.stats["written"] == 1, f"expected one view written, got {result.stats}"
        page = gitcmd.run("show", f"{settings.branch}:{regenerate.view_relpath(second_id)}",
                          cwd=env.bare).stdout
        assert page, f"the sweep did not commit views/{second_id}.md"
        print(page)

        step("and again, with nothing changed — the cost property: no commit and no model call")
        before = gitcmd.run("rev-parse", settings.branch, cwd=env.bare).stdout.strip()
        again = worker.run_view_sweep(conn, deps)
        show("stats", again.stats)
        assert again.stats["written"] == 0 and again.stats["removed"] == 0
        assert gitcmd.run("rev-parse", settings.branch, cwd=env.bare).stdout.strip() == before, (
            "a converged corpus must cost zero commits")
        show("remote tip", "unmoved")

        print(f"\n{'=' * 78}\nView walk complete — {STEP} step(s). Mechanism proven offline; the "
             f"live 'ask what do we know about {entity_id}?' judgment against the real "
             f"corpus stays a hand step (see this script's module "
             f"docstring).\n{'=' * 78}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
