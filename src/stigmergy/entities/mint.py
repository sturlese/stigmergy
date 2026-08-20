"""The mint orchestration: from a validated proposal to one pushed commit — the ONE function
`entities.cli._mint` and `entities.remote.mint_via_clone` both call, so the two doors into one
registry cannot drift apart. The discipline, in order: drift refusal; resolve-before-mint against
the registry the commit will PUBLISH; the template render; `generator.regenerate`; the gitleaks
scan over exactly the files the commit carries; ONE commit; bounded rebase-and-retry; never a
force-push.

`author` is supplied by the caller, never derived here from git config: a server-driven mint runs
in a throwaway clone with no steward and resolves the App's identity itself. `mint()` still runs
the branch/clean/in-sync checks, so a caller that skipped `preflight` gets no less protection.
`trailer` (`Approved-by: ...` for an App-authored mint) is appended to the commit message when
non-empty; the empty default keeps a steward's own commit byte-identical.
"""
import os

from stigmergy.entities import birth, clone, generator
from stigmergy.entities.errors import CollisionRaceError, EntityError, TemplateMissingError
from stigmergy.kernel.fsutil import write_text_atomic
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian import gates

# Repo-relative, slash-separated; the generator owns the knowledge repo's identity layout.
TEMPLATE_RELPATH = generator.TEMPLATE_RELPATH

# The same env var `librarian.config.Settings.gitleaks_bin` reads. Spelled twice because
# `stigmergy.entities` must not import a worker's `Settings` to ask one question; the duplication
# is declared here rather than left to be discovered.
GITLEAKS_BIN_ENV = "STIGMERGY_GITLEAKS_BIN"


def mint(repo: str, *, entity_id: str, name: str, entity_type: str, aliases=(), role: str = "",
         branch: str, today: str, author: tuple[str, str], submission_id: int | None = None,
         trailer: str = "", on_output=None) -> dict:
    """Everything between "this identity is allowed to exist" and "it is on the remote".

    Shared in full by every mint path — the paths differ only in where `repo`/`author` come from
    and whether a queue row prompted it; letting one skip a check, or acquire a different one, is
    how two doors into one registry come to enforce two contracts. The collision gate is asked
    about the registry this commit will PUBLISH (derived from the pages), never the file on disk:
    with drift, an unregistered `Acme Corp.md` in the clone is invisible to the file and present
    in the commit. Hence the order below — refuse on drift at all, then gate against the derived
    view.
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
        # The TYPE, not the text, is what `entities.remote` reads to re-word this for a steward
        # holding no clone; the sentence below stays the operator's, naming which checkout is
        # missing the template (`entities/errors.py`'s module docstring).
        raise TemplateMissingError(
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
    except BaseException:
        # Roll back by bytes we captured ourselves — never `git checkout` or `git clean`: the CLI
        # path is a human's clone, and a destructive git rollback is one bad predicate away from
        # discarding work this tool never saw.
        #
        # `BaseException`, not `Exception`: this window is the registry regeneration and the
        # gitleaks scan — the slowest thing this command does, so the likeliest moment for a
        # steward's Ctrl-C — and a `KeyboardInterrupt` that skipped the rollback left an untracked
        # page and a rewritten registry behind, which the NEXT `create`/`approve` then refused on
        # as a dirty tree. The re-raise is what keeps `KeyboardInterrupt`/`SystemExit` propagating
        # after the clone has been put back; nothing here swallows one.
        _restore(page_path, registry_path, snapshot)
        raise

    landed = clone.commit_and_push(
        repo, branch=branch,
        message=birth.commit_message(proposal, submission_id=submission_id, trailer=trailer),
        author=author,
        # The retry's regeneration AND the retry's gate: after a rebase the derived file is
        # re-derived and the identity re-asked of what actually landed. Returns whether the file
        # changed, so an irrelevant race does not rewrite the commit for nothing.
        regenerate=lambda: _recheck_and_regenerate(repo, proposal, branch=branch),
        on_retry=on_output)
    return {"entity_id": proposal.canonical_id, "name": proposal.name,
            "entity_type": proposal.entity_type, "aliases": list(proposal.aliases),
            "page": proposal.relpath, "registry": generator.REGISTRY_RELPATH,
            "commit": landed, "steward": f"{author[0]} <{author[1]}>", "branch": branch}


def _refuse_drift(repo: str, *, action: str) -> None:
    """Refuse to mint into a clone whose registry and pages already disagree.

    `regenerate` would otherwise silently resolve somebody else's drift and publish the resolution
    inside a commit whose message says it created ONE entity — `ensure_clean`'s argument, applied
    to the derived file. Drift also invalidates the collision gate's premise: the pages and the
    file describing the same registry.
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

    A rebase invalidates the gate's answer, not only the derived file: two callers minting `Acme`
    and `Zenith Systems (alias: Acme)` in the same minute pass their own gates, auto-merge cleanly
    (the entries sort far apart) and leave one matcher key claimed by two entities, last-wins. So
    the gate runs again against what actually landed — with the proposal's OWN entity excluded
    (its page is in the tree by now; unfiltered, every proposal collides with itself). The check
    runs BEFORE the regeneration, so a refusal leaves the tree exactly as the rebase left it.
    """
    others = [e for e in generator.read_entity_pages(repo)
              if e.canonical_id != proposal.canonical_id]
    try:
        birth.recheck(proposal, registry=generator.registry_of(others),
                      existing_pages=[e.relpath for e in others])
    except EntityError as ex:
        # `CollisionRaceError`, not the plain `CollisionError` the gate itself raises: the two say
        # different things and only this one names the clone. `entities.remote` maps THIS type and
        # lets the gate's own verdict through, so a steward who simply proposed an identity that
        # already exists keeps being told to point the capture at it.
        raise CollisionRaceError(
            f"{ex}\n\nThis collision did not exist when the command started: something else "
            f"pushed to {branch} while this one was committing, and the identity being minted "
            f"resolves to theirs. Nothing was pushed and nothing was force-pushed; the commit is "
            f"in the local clone ({clone.head(repo)[:12]}), where `git -C {repo} log -1` and "
            f"`git -C {repo} log origin/{branch} -1` show both sides of the race") from ex
    return generator.regenerate(repo).changed


def _refuse_secrets(repo: str, relpaths: list[str], *, action: str) -> None:
    """gitleaks over the exact files this commit will carry, before it is made.

    What a mint commits includes `--role`/`--aliases` — free text typed with untrusted material on
    screen — and the commit is `--no-verify`, so this scan is the one that runs: git cannot
    forget, and `main` is the place a secret must never reach. `gates.scan_worktree_files`, never
    a second gitleaks invocation. A missing scanner REFUSES (`ensure_scanner`) rather than skips —
    a secrets gate that silently passes is worse than no gate.
    """
    gitleaks_bin = os.environ.get(GITLEAKS_BIN_ENV) or librarian_config.Settings.gitleaks_bin
    gates.ensure_scanner(gitleaks_bin)
    findings = gates.scan_worktree_files(repo, relpaths, gitleaks_bin=gitleaks_bin)
    if not findings:
        return
    # gitleaks names the scratch copy it actually read — true and useless, gone before anyone
    # reads the message. Rewritten to the repo-relative names the reader knows.
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
        # Atomic like the write it un-does: a truncating rewrite interrupted mid-rollback would
        # leave a half-written registry in the steward's clone. A crash between the tmp write and
        # the rename leaves `<registry>.tmp` untracked, and `clone.ensure_clean` then refuses the
        # NEXT approve by name (`--porcelain` counts untracked files) — deliberately fail-closed;
        # the operator deletes the leftover.
        write_text_atomic(registry_path, snapshot)
