"""The mint orchestration: from a validated proposal to one pushed commit.

Extracted from `entities.cli`'s own `_mint` (ADR 030 D4) so the CLI and a server-driven mint
(`entities.remote.mint_via_clone`) share EXACTLY the same discipline instead of two doors into one
registry slowly drifting apart: resolve-before-mint against the registry the commit will PUBLISH,
drift refusal, the template render, `generator.regenerate`, the gitleaks scan over exactly the
files the commit carries, ONE commit, bounded rebase-and-retry against a concurrent push, never a
force-push.

**`author` is supplied by the caller, never derived here from `repo`'s git config.** The CLI's
thin adapter (`entities.cli._mint`) still calls `clone.preflight`, which is where "your clone has
no git identity configured" is refused — that refusal is meaningful only for a STEWARD's own
checkout. A server-driven mint runs in a throwaway clone that carries no steward at all; it
resolves the librarian App's identity itself (`librarian.githubapp.identity`) and hands it here
directly. This function still runs the branch/clean/in-sync checks itself
(`clone.ensure_on_branch`/`ensure_clean`/`ensure_in_sync` — `preflight`'s own disciplines, minus
the identity read), so a caller that skipped them gets no less protection than one that ran
`preflight` first; the CLI running both costs three cheap git reads, never git writes.

**`trailer`** lands in the commit MESSAGE, appended as its own paragraph, when non-empty — the
`Approved-by: <resolved identity>` line ADR 030 D1 requires of a server-driven (App-authored)
mint. Empty by default, which is what keeps a steward's own `stigmergy-entities approve`/`create`
commit byte-identical to what it always was.
"""
import os

from stigmergy.entities import birth, clone, generator
from stigmergy.entities.errors import CollisionError, EntityError
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian import gates

# Repo-relative, slash-separated — the same spelling discipline `librarian.config`'s RELPATHs use.
TEMPLATE_RELPATH = "ops/templates/entity.md"

# The env var `librarian.config.Settings.gitleaks_bin`'s own binary reads for the same binary.
# Spelled twice because `stigmergy.entities` must not import a worker's `Settings` object to ask one
# question (`entities/index.md`'s own documented edge); the duplication is declared here rather
# than left to be discovered.
GITLEAKS_BIN_ENV = "STIGMERGY_GITLEAKS_BIN"


def mint(repo: str, *, entity_id: str, name: str, entity_type: str, aliases=(), role: str = "",
         branch: str, today: str, author: tuple[str, str], submission_id: int | None = None,
         trailer: str = "", on_output=None) -> dict:
    """Everything between "this identity is allowed to exist" and "it is on the remote".

    Shared by every mint path — `stigmergy-entities approve`, `stigmergy-entities create` and a
    server-driven mint through `entities.remote.mint_via_clone` (ADR 030) — in full: they differ
    only in where `repo`/`author` come from and whether a queue row (`submission_id`) prompted it,
    and letting one of them skip a check, or acquire a different one, is how two doors into one
    registry come to enforce two different contracts.

    **The gate is asked about the registry this commit will PUBLISH, not the one on disk.** Those
    are the same object only when the repo has no drift, and the whole reason `--check` exists is
    that it sometimes does. Hand `birth.prepare` the `committed_registry` — the FILE — while
    `regenerate` derives the published registry from the PAGES, and an unregistered `Acme Corp.md`
    sitting in the clone is invisible to the collision gate and present in the commit:
    `approve --name Acme` passes both checks and publishes two entries whose matcher keys collapse
    onto one. Hence the two steps below, in this order: refuse on drift at all, then check against
    the derived view.
    """
    action = "approve" if submission_id else "create"
    clone.ensure_on_branch(repo, branch, action=action)
    clone.ensure_clean(repo, action=action)
    clone.ensure_in_sync(repo, branch, action=action)
    _refuse_drift(repo, action=action)

    entities = generator.read_entity_pages(repo)
    proposal = birth.prepare(
        canonical_id=entity_id, name=name, entity_type=entity_type,
        aliases=aliases, role=role or "",
        registry=generator.registry_of(entities),
        existing_pages=[e.relpath for e in entities])

    template_path = os.path.join(repo, *TEMPLATE_RELPATH.split("/"))
    if not os.path.exists(template_path):
        raise EntityError(
            f"{TEMPLATE_RELPATH} is missing from {repo} — a new entity page is that template with "
            f"its identity fields filled in, and this command does not carry its own copy (the "
            f"template is the knowledge repo's own source of truth for the page's shape)")
    with open(template_path, encoding="utf-8") as f:
        page = birth.render_page(f.read(), proposal, today=today)

    registry_path = generator.registry_path(repo)
    snapshot = generator.snapshot(repo)
    page_path = clone.write_page(repo, proposal.relpath, page)
    try:
        generator.regenerate(repo)
        _refuse_secrets(repo, [proposal.relpath, generator.REGISTRY_RELPATH], action=action)
    except Exception:
        # Roll back to EXACTLY what was on disk, by bytes we captured ourselves — never with `git
        # checkout` or `git clean`. This is (for the CLI path) a human's clone, and a rollback that
        # runs a destructive git command is one bad predicate away from discarding work this tool
        # never saw; a server-driven mint's throwaway clone gets no less care, on the same code.
        _restore(page_path, registry_path, snapshot)
        raise

    landed = clone.commit_and_push(
        repo, branch=branch,
        message=birth.commit_message(proposal, submission_id=submission_id, trailer=trailer),
        author=author,
        # The retry's regeneration AND the retry's gate: after a rebase the tree underneath this
        # commit has moved, so the derived file is re-derived and amended in — and the identity is
        # re-asked of what actually landed, because the tree moving is the tree the gate's answer
        # was about. Returns whether the file changed, so a race that touched nothing relevant does
        # not rewrite the commit for nothing.
        regenerate=lambda: _recheck_and_regenerate(repo, proposal, branch=branch),
        on_retry=on_output)
    return {"entity_id": proposal.canonical_id, "name": proposal.name,
            "entity_type": proposal.entity_type, "aliases": list(proposal.aliases),
            "page": proposal.relpath, "registry": generator.REGISTRY_RELPATH,
            "commit": landed, "steward": f"{author[0]} <{author[1]}>", "branch": branch}


def _refuse_drift(repo: str, *, action: str) -> None:
    """Refuse to mint into a clone whose registry and pages already disagree.

    The alternative is not "proceed harmlessly": `regenerate` would silently resolve somebody
    else's drift and publish the resolution inside a commit whose message says it created ONE
    entity. That is `ensure_clean`'s argument one layer up — anything already lying around lands in
    this commit, signed by this steward (or, for a server-driven mint, by the App) — applied to the
    derived file instead of the working tree. It is also what makes the collision gate below
    trustworthy: `--check` passing is the statement that the pages and the file describe the same
    registry, which is the premise the gate's answer is only meaningful under.
    """
    outcome = generator.check(repo)
    if not outcome.divergences:
        return
    listed = "\n  ".join(d.message.split(" — run ")[0] for d in outcome.divergences)
    raise EntityError(
        f"refusing to {action} — {generator.REGISTRY_RELPATH} and {generator.ENTITIES_RELDIR}/ "
        f"already disagree in this clone, so the collision check would be asked about a registry "
        f"this commit is not going to publish:\n  {listed}\n"
        f"Run `{generator.FIX_COMMAND}`, review what it writes and commit that first (it is a "
        f"change to who resolves to what, which is exactly the decision this tool exists to put in "
        f"front of a human), then re-run this command")


def _recheck_and_regenerate(repo: str, proposal: birth.Proposal, *, branch: str) -> bool:
    """The rebase hook: re-ask the collision gate, then re-derive. Raises to abandon the push.

    Re-deriving the moved DERIVED FILE is only half of what a rebase invalidates. The other half is
    the question that moved with it: two callers minting `Acme` and
    `Zenith Systems (alias: Acme)` in the same minute pass their own gates against a registry
    neither has published yet, auto-merge cleanly (the two entries sort far apart, so git sees no
    conflict at all), and leave one matcher key claimed by two entities, resolved last-wins. The
    gate ran; it simply ran against a registry that no longer exists by the time the commit lands.
    So it runs again here, against what actually landed.

    **The proposal's own entity is excluded from what it is checked against**, which is the whole
    subtlety: by this point our page IS in the tree, so an unfiltered derived registry would report
    every proposal as colliding with itself. `generator.registry_of` exists to build that
    minus-one view through the same indexing code as the full one.

    Order matters: the check runs BEFORE the regeneration, so a refusal leaves the working tree
    exactly as the rebase left it rather than with a rewritten registry nobody is going to commit.
    """
    others = [e for e in generator.read_entity_pages(repo)
              if e.canonical_id != proposal.canonical_id]
    try:
        birth.recheck(proposal, registry=generator.registry_of(others),
                      existing_pages=[e.relpath for e in others])
    except EntityError as ex:
        raise CollisionError(
            f"{ex}\n\nThis collision did not exist when the command started: something else "
            f"pushed to {branch} while this one was committing, and the identity being minted "
            f"resolves to theirs. Nothing was pushed and nothing was force-pushed; the commit is "
            f"in the local clone ({clone.head(repo)[:12]}), where `git -C {repo} log -1` and "
            f"`git -C {repo} log origin/{branch} -1` show both sides of the race") from ex
    return generator.regenerate(repo).changed


def _refuse_secrets(repo: str, relpaths: list[str], *, action: str) -> None:
    """gitleaks over the exact files this commit will carry, before it is made.

    `approve`/`create`/a server-driven mint are the path-scoped write paths to `ops/` on `main`,
    committing `ops/entity-registry.json` and `wiki/entities/`; what they commit includes `--role`/
    `--aliases` (or the MCP/Slack/console equivalents) — free text a human typed with material on
    screen, or an agent-adjacent surface relayed. They commit `--no-verify` (so the knowledge
    repo's own hooks do not run), so this scan is the one that runs instead: git cannot forget, so
    `main` is the place a secret must never reach.

    `gates.scan_worktree_files` rather than a second gitleaks invocation — same binary, same
    `--redact`, same JSON report, same `Finding` whose message already names the rule id.

    Refusing when the scanner is missing (`ensure_scanner`) rather than skipping: a secrets gate
    that silently passes is worse than no gate.
    """
    gitleaks_bin = os.environ.get(GITLEAKS_BIN_ENV) or librarian_config.Settings.gitleaks_bin
    gates.ensure_scanner(gitleaks_bin)
    findings = gates.scan_worktree_files(repo, relpaths, gitleaks_bin=gitleaks_bin)
    if not findings:
        return
    # gitleaks names the file it actually read, which is the copy inside the scratch directory
    # `scan_worktree_files` builds. That path is true and useless: it is gone by the time anyone
    # reads the message, and it points at nothing they can open. The relpaths handed in above are
    # the same files under names they recognise, so the message is said in those.
    listed = "\n  ".join(_relocate(f.message, relpaths) for f in findings)
    raise EntityError(
        f"refusing to {action} — the secret scanner matched something in what this commit would "
        f"carry:\n  {listed}\n"
        f"Nothing was committed and the page was removed. If this is a real credential, rotate it "
        f"and re-run with the value out of the role/aliases fields; if it is a false positive, the "
        f"rule id above is what to allowlist in the knowledge repo's gitleaks configuration")


def _relocate(message: str, relpaths: list[str]) -> str:
    """Rewrite a scanner's absolute scratch path as the repo-relative file its reader knows."""
    for relpath in relpaths:
        marker = message.find(relpath)
        if marker == -1:
            continue
        start = message.rfind(" ", 0, marker) + 1
        return message[:start] + message[marker:]
    return message


def _restore(page_path: str, registry_path: str, snapshot: str | None) -> None:
    clone.discard_untracked(page_path)
    if snapshot is None:
        clone.discard_untracked(registry_path)
    else:
        with open(registry_path, "w", encoding="utf-8") as f:
            f.write(snapshot)
