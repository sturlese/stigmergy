"""Apply ONE approved proposal: clone the knowledge repo with the librarian App's credential,
perform exactly the approved ops, prove the result through the eight gates, commit and push.

A throwaway clone per approval, never a standing checkout — the same posture (and the same
`TemporaryDirectory` guarantee) `entities.remote` takes for a server-driven mint, and for the same
reason: this runs inside whichever process a steward pressed Approve in, and that process must not
hold a working tree for a rare operation. The credential rides the SHARED
`githubapp.authenticated_clone_url`, so a repair and a mint cannot come to disagree about when one
is needed.

**Three independent things have to agree before anything is pushed**, and each was chosen because
the other two cannot see what it sees:

  1. The kind's own validator re-runs against THIS clone — `edits.apply_declared` for the additive
     kinds, `entity_body.apply_declared` for a body draft. The propose-time validation ran against
     a checkout that may be hours old; a page deleted since then must refuse here.
  2. `run_gates(ALL_GATES)` judges the resulting DIFF, exactly as it judges the librarian's own.
     What the ops are supposed to produce is an additive edit, or — for `entity-body`, and only on
     the ONE page this apply names in `body_rewrite_allowed` — a replaced body below that page's
     own H1 with its frontmatter otherwise intact. `gate_body_rewrite` is what proves they did
     rather than what promises they would, and the permission is told to it here, per path.
  3. The cross-check: the diff's paths must equal the proposal's own stored `target_paths` and
     every entry must be a MODIFICATION. The gates would pass a diff touching some other page
     quite happily — it is additive and well-formed — so this is the only thing that can say the
     diff is not the one this row describes. State its reach exactly: an `ops` blob INCONSISTENT
     with `target_paths` cannot reach `main`; content is not compared, and a tamper that edited
     both columns consistently before this row was read is out of scope. Write access to the table
     is the prerequisite for either, so this is a consistency check between two stored facts and
     never a defense against a database an attacker already writes to.

Then `gitcmd.commit(gated_entries=...)` closes the last window: the diff the gates approved is the
diff that lands, bytes included.

Every sentence raised from here is publishable — a steward reads it through the review lane — so
none names this host's clone. Repo-relative paths and gate codes are allowed and are the whole
actionable content of a veto.
"""
import logging
import os
import tempfile
from dataclasses import dataclass, field

from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import config as librarian_config
from stigmergy.librarian import edits, gates, gitcmd, githubapp
from stigmergy.librarian.errors import LibrarianError
from stigmergy.repair import deletion, entity_alias, entity_body, schema, store
from stigmergy.repair.errors import ProposalStateError, RepairError

log = logging.getLogger(__name__)

# One network leg. Generous enough for a cold clone, short enough that a stalled remote cannot pin
# an HTTP worker indefinitely — `entities.remote.MINT_GIT_TIMEOUT_S`'s figure, for the identical
# situation.
REPAIR_GIT_TIMEOUT_S = 60

# The other subprocess budget: the eight gates shell out to the contract linter and to gitleaks,
# and both run HERE on the thread a steward's Approve arrived on. Longer than the git one because
# it is not a network leg — it is a whole-repo lint and a whole-scratch-directory scan — and short
# enough that neither can pin an HTTP worker until the process is restarted.
REPAIR_SUBPROCESS_TIMEOUT_S = 120

# The same env var `librarian.config.Settings.gitleaks_bin` reads. Spelled here rather than
# imported for the reason `entities.mint` spells it: this package must not construct a worker's
# `Settings` to ask one question. The duplication is declared, not discovered.
GITLEAKS_BIN_ENV = "STIGMERGY_GITLEAKS_BIN"

# What a caller is told about a git or configuration fault, and ALL they are told. The detail is
# MOVED, not lost — logged at ERROR with the traceback, where an operator can read it and a
# steward cannot: git names the throwaway clone's absolute path and `gates.ensure_scanner`
# interpolates an operator-supplied one.
APPLY_FAULT_MESSAGE = (
    "the repair could not be applied — the server hit a git or configuration fault partway "
    "through, not a problem with the proposal you approved. Nothing was pushed. The details are "
    "in the server log; ask whoever runs this deployment to look, then approve again")

CLONE_FAILED_MESSAGE = (
    "the knowledge repo could not be cloned to apply this repair — the server could not reach it "
    "or is not credentialed for it. Nothing was pushed. The details are in the server log; ask "
    "whoever runs this deployment to look")


def apply_via_clone(repo_url: str, branch: str, credential, *, proposal: dict, approved_by: str,
                    on_output=None) -> dict:
    """Clone, apply, gate, cross-check, commit, push. Returns `{"commit": sha, "paths": [...]}`.

    `credential` is the env-shaped mapping `librarian.githubapp` reads — required ONLY when
    `repo_url` is `https://`. A local path or `git://` URL authenticates nothing, so `credential`
    may be `None`: the honest statement of when a credential is needed, and what lets this be
    proven against a real bare remote with no key and no network.

    Raises `RepairError` for everything: a refusal a steward can act on names the gate and its
    codes, a fault names neither and is in the log.
    """
    ops = list(proposal.get("ops") or ())
    if not ops:
        raise ProposalStateError("this proposal carries no ops, so there is nothing to apply")
    # Asked BEFORE the clone: an approval with nobody's name on it cannot produce a commit, so
    # cloning first would spend a network leg to arrive at the same refusal.
    approver = _trailer_actor(approved_by)

    try:
        clone_url = githubapp.authenticated_clone_url(repo_url, credential)
    except LibrarianError as ex:
        # The three credential states are re-worded rather than echoed: `githubapp` names the App
        # private-key FILE PATH in one of them, and this sentence goes to a steward over MCP.
        log.error("repair apply: no usable credential for the knowledge repo", exc_info=True)
        raise RepairError(CLONE_FAILED_MESSAGE) from ex
    author = githubapp.identity(credential or {})
    with tempfile.TemporaryDirectory(prefix="stigmergy-repair-apply-") as tmp:
        clone = os.path.join(tmp, "repo")
        try:
            gitcmd.run("clone", "--quiet", "--branch", branch, clone_url, clone,
                       timeout=REPAIR_GIT_TIMEOUT_S)
        except LibrarianError as ex:
            # `_scrub` keeps the CREDENTIAL out of `str(ex)`, but scrubbed is not publishable:
            # what remains is git's stderr naming this host's temp directory.
            log.error("repair apply: could not clone the knowledge repo", exc_info=True)
            raise RepairError(CLONE_FAILED_MESSAGE) from ex
        try:
            # Configured on the clone itself, not only handed to `commit`: `gitcmd.push`'s
            # rebase-and-retry runs `git rebase`, and a fresh temp clone cannot assume a global
            # `~/.gitconfig` supplies a committer.
            gitcmd.run("config", "user.name", author[0], cwd=clone)
            gitcmd.run("config", "user.email", author[1], cwd=clone)
            return _apply_in_clone(clone, branch, credential, proposal=proposal, ops=ops,
                                   author=author, approver=approver, on_output=on_output)
        except RepairError:
            raise
        except LibrarianError as ex:
            # The seam this module promises for everything after the clone: `gates.ensure_scanner`
            # raises when gitleaks is absent, `gitcmd` raises on a push that will not land. Caught
            # as the BASE class — a seam that holds only for the faults already observed breaks on
            # the next one.
            log.error("repair apply: a librarian fault after the clone", exc_info=True)
            raise RepairError(APPLY_FAULT_MESSAGE) from ex


def _apply_in_clone(clone: str, branch: str, credential, *, proposal: dict, ops: list,
                    author: tuple[str, str], approver: str, on_output) -> dict:
    """Everything between a fresh clone and a pushed sha. Split out so the `LibrarianError` seam
    above wraps the WHOLE of it and this function can read as the sequence it is."""
    kind = str(proposal.get("kind") or schema.KIND_EDITS)
    edited, findings = _perform(clone, kind, ops)
    if findings:
        # The edit validator's codes, not its sentences: its messages are written for a worker's
        # log and name the worktree. The codes (`missing-target`, `dead-link`, …) are stable
        # vocabulary and say what a steward would need to know.
        raise RepairError(
            f"this repair no longer applies to the knowledge repo as it stands "
            f"({', '.join(sorted({f.code for f in findings}))}) — the pages moved under the "
            f"proposal. Nothing was pushed; propose again against the current corpus")
    if not edited:
        # Every op was already there. Honest, and NOT an error the steward caused — but there is
        # nothing to commit, and an empty commit would claim a repair that did not happen.
        raise ProposalStateError(
            "this repair changes nothing: the pages already say exactly what it proposes. Nothing "
            "was pushed — the corpus already carries the answer the finding asked for")

    entries = gitcmd.diff_entries(clone)
    _cross_check(entries, proposal, kind=kind, ops=ops)

    gitleaks_bin = os.environ.get(GITLEAKS_BIN_ENV) or librarian_config.Settings.gitleaks_bin
    gates.ensure_scanner(gitleaks_bin)
    told = _lane_and_permission(kind, ops)
    linter_path = os.path.join(clone, *librarian_config.LINTER_RELPATH.split("/"))
    ctx = gates.GateContext(
        worktree=clone, entries=entries, added=gitcmd.added_lines(clone),
        # No material and no outcome: nothing was captured and no agent wrote here — the ops came
        # off a row a human approved. Every gate that reads either is scoped to CREATED pages, and
        # this diff has none.
        material="", outcome=None,
        registry=registry_module.load_registry(
            os.path.join(clone, *librarian_config.REGISTRY_RELPATH.split("/"))),
        # The clone IS the base commit, so its own checked-out linter is the one that governs —
        # no materialization from git needed, unlike the worker's detached worktrees.
        linter_path=linter_path,
        gitleaks_bin=gitleaks_bin,
        # TOLD, not inferred by the gates: this apply runs inside an HTTP request, unlike the
        # worker's. A `LibrarianConfigError` out of an elapsed budget meets the `except
        # LibrarianError` seam above and the row lands `failed` with a sentence, not a hung thread.
        subprocess_timeout_s=REPAIR_SUBPROCESS_TIMEOUT_S,
        # Every caller-scoped fact the gates are TOLD is granted here and nowhere else in this
        # loop: which zones THIS apply owns, which single page's prose its approval covers, which
        # paths it may remove, which non-page file it derived from the pages in this same commit,
        # and — for every page whose bytes were computed ahead of time — exactly what they are.
        # (`provenance_pages` is also granted by the librarian's own source-attachment flow, which
        # is why this sentence names no count.) Every one is
        # derived from the ops that were just performed, never from `target_paths` — the two are
        # cross-checked against each other a few lines above, so deriving a permission from the
        # same fact the cross-check judges would make one stored column able to widen the other.
        write_prefixes=told.lane, body_rewrite_allowed=told.body_rewrite_allowed,
        deletions_allowed=told.deletions_allowed, expected_bytes=told.expected_bytes,
        provenance_pages=told.provenance_pages, derived_files=told.derived_files)
    veto = gates.vetoes(gates.run_gates(ctx))
    if veto:
        raise RepairError(
            f"the gates refused this repair, so nothing was committed or pushed: "
            f"{'; '.join(f'{f.gate}/{f.code}' for f in veto)}. The proposal has been recorded as "
            f"failed and the corpus is unchanged")
    if kind == schema.KIND_DELETE:
        _refuse_surviving_dead_links(clone, linter_path, ops)

    sha = gitcmd.commit(clone, message=commit_message(proposal, approved_by=approver),
                        author_name=author[0], author_email=author[1],
                        # The diff the gates approved is the diff that lands, bytes included.
                        gated_entries=entries)
    if on_output:
        on_output(f"committed {sha[:12]} in a throwaway clone; pushing to {branch}")

    remote_url, config_env = "", {}
    if credential and githubapp.configured(credential):
        slug = githubapp.repo_slug(clone)
        remote_url = githubapp.push_url(slug)
        # Minted as late as possible and handed over in the ENVIRONMENT, never in argv.
        config_env = githubapp.push_config(githubapp.installation_token(credential), slug)
    # THE sha the push produced: a rebase-and-retry REWRITES the commit, and the pre-push sha
    # would name an object no reachable history holds. There is no lease to re-assert here —
    # nothing else is racing for this proposal, because the row moved out of `pending` before the
    # clone was made.
    #
    # The NON-ADDITIVE kinds never rebase. Their apply is a proof against a base — recompute,
    # byte-compare, then perform — and a rebase replays the approved diff onto a tip the gates
    # never judged: a delete can leave a dead link a fresh plan would have scrubbed, a merge can
    # leave a page anchored to the retired identity forever. A lost race there fails CLEAN (the
    # row lands `failed`, nothing is pushed) and the next propose recomputes from state — the
    # same shape the view sweep takes at a mid-batch rebase, for the same reason. Since the view
    # sweep pushes up to its ceiling every interval, losing this race is realistic, and a failed
    # apply a steward re-approves is the correct side to fail on: a wrong write is not
    # recoverable.
    landed = gitcmd.push(clone, branch=branch, remote_url=remote_url, config_env=config_env,
                         author_name=author[0], author_email=author[1],
                         timeout_s=REPAIR_GIT_TIMEOUT_S,
                         rebase=kind not in (schema.KIND_DELETE, schema.KIND_ENTITY_ALIAS))
    return {"commit": landed, "paths": list(edited), **_outcome_detail(kind, ops)}


# What an applied repair records BEYOND the commit and the paths, per kind. `paths` alone cannot
# tell a steward reading the governance ledger months later whether an approval removed one page or
# eleven — and for a deletion that is the whole of what was approved.
LEDGER_RESULT_KEYS = ("commit", "paths", "deleted", "scrubbed_pages", "absorbed", "survivor",
                      "reanchored_pages")


def _outcome_detail(kind: str, ops: list) -> dict:
    """The kind-specific half of an apply's result. Empty for the kinds whose `paths` says
    everything: a ledger row carrying always-empty columns teaches nobody anything."""
    if kind == schema.KIND_DELETE:
        return {"deleted": deletion.deleted_paths(ops),
                "scrubbed_pages": len(deletion.scrubbed_paths(ops))}
    if kind == schema.KIND_ENTITY_ALIAS:
        # WHICH identity absorbed which is the whole of what was approved, and `paths` cannot say
        # it — the two entity pages are two entries in one sorted list.
        return {"survivor": entity_alias.survivor_path(ops),
                "absorbed": entity_alias.absorbed_path(ops),
                "reanchored_pages": len(entity_alias.reanchored_paths(ops))}
    return {}


def _refuse_surviving_dead_links(clone: str, linter_path: str, ops: list) -> None:
    """The knowledge repo's OWN linter, over the whole clone, asked one question: does anything
    still link to a page this sweep removed?

    `gate_contract` filters the linter's findings to the pages a diff TOUCHED, which is right for
    every other kind and blind for this one — a deletion's blast radius is the whole graph, and a
    page the sweep never planned is exactly where a missed reference would sit. So the delete road
    pays for a second scan of the same clone and asks the unfiltered report itself.

    It is scoped to the deleted stems rather than vetoing on ANY error, deliberately: a corpus that
    already carries an unrelated contract error is not this steward's problem, and refusing their
    deletion for it would be a gate bouncing somebody's real work for something they cannot fix
    from here. What this catches is the one thing the sweep could get wrong that nothing else sees:
    its reference scanner is hand-mirrored from that linter and could drift from it.
    """
    stems = {deletion.page_stem(path) for path in deletion.deleted_paths(ops)}
    report = gates.lint_report(clone, linter_path, timeout_s=REPAIR_SUBPROCESS_TIMEOUT_S)
    surviving = set()
    for finding in report.get("findings", []):
        if finding.get("check") != gates.DEAD_LINKS_CHECK or finding.get("severity") != "error":
            continue
        # `gates.dead_link_target` is the ONE reader of that message's shape, so the raw report row
        # is wrapped in the `Finding` it expects rather than re-parsed with a second regex here.
        target = gates.dead_link_target(
            gates.Finding("contract", gates.DEAD_LINKS_CHECK, str(finding.get("message", ""))))
        if target and deletion.link_stem(target) in stems:
            surviving.add((str(finding.get("file", "")), target))
    surviving = sorted(surviving)
    if surviving:
        named = ", ".join(f"{path} still links [[{target}]]" for path, target in surviving)
        raise RepairError(
            f"this deletion would leave the corpus with a dead link, so nothing was committed or "
            f"pushed: {named}. The sweep did not plan a rewrite of that page — propose again, and "
            f"if it happens twice the reference is spelled in a shape the sweep does not read")


def _perform(clone: str, kind: str, ops: list) -> tuple[list[str], list]:
    """The ops, performed — ONE branch per proposal kind, and the only one in this module.

    Each kind has its own validator and its own writer, and both run against THIS clone: the
    propose-time run judged a checkout that may be hours old. An unknown kind falls through to the
    edits road deliberately, where `edits.validate` refuses it by name (`unknown-kind`) rather
    than being silently performed by whichever branch happened to be last — a row whose `kind` the
    code does not know is a row this version must not act on.
    """
    if kind == schema.KIND_ENTITY_BODY:
        return entity_body.apply_declared(clone, ops)
    if kind == schema.KIND_DELETE:
        return deletion.apply_declared(clone, ops)
    if kind == schema.KIND_ENTITY_ALIAS:
        return entity_alias.apply_declared(clone, ops)
    return edits.apply_declared(clone, schema.declared_edits(ops), new_pages=())


@dataclass(frozen=True)
class _ToldFacts:
    """The caller-scoped facts one apply hands the gates. A record rather than a tuple because
    there are four of them now and three are permissions — a positional mix-up between two
    frozensets is a permission granted for the wrong thing, and it would type-check."""

    lane: tuple
    body_rewrite_allowed: frozenset = frozenset()
    deletions_allowed: frozenset = frozenset()
    expected_bytes: dict = field(default_factory=dict)
    provenance_pages: frozenset = frozenset()
    derived_files: frozenset = frozenset()


def _lane_and_permission(kind: str, ops: list) -> _ToldFacts:
    """The caller-scoped facts for this apply — TOLD to the gates, and never inferred by them.

    For the additive kinds every one is the default: the librarian's whole write lane, no page
    whose prose may be replaced, no page that may be removed, no page whose bytes were computed
    ahead of time. That is the property the other two kinds are measured against.

    `entity-body` narrows the lane to the entity zone (this apply has no business anywhere else)
    and names the ONE page the ops declared — never the whole zone, because a permission wide
    enough for a second page is a permission for a page nobody approved.

    `delete` names its deletions and hands over the exact bytes it planned for every page it
    rewrites. Its lane is DERIVED from the plan rather than fixed, because a sweep legitimately
    spans several zones; what confines it is `deletion.validate`, and `lane_for` explains the
    division of labour. It is also the first flow here that MODIFIES a machine-zone page, so it
    names the provenance pages among them — the same fact the librarian's source-attachment flow
    declares, for the same reason: those stamps are the librarian's own and a scrub only removes.

    `entity-alias` derives its lane the same way and hands over the same computed bytes, and it is
    the only kind that names a DERIVED FILE: `ops/entity-registry.json` is not a page, so
    `gate_zone` refuses it by default and rightly. It names no deletion and no body rewrite — a
    merge removes nothing and replaces no prose — so byte-equality is the whole of its proof.

    It also names NO `provenance_pages`, and that absence is a decision rather than an omission.
    `delete` declares them because its scrub only REMOVES a dead link from a machine-written page;
    a merge REWRITES a value, and a caller with no claim to rewrite `content_hash:`/`tier:` should
    not be handed the permission to. What follows is that the machine zones are excluded from the
    re-anchor population upstream (`entity_alias.anchored_paths`) rather than waved through here —
    a `views/` page is derived and the view sweep converges it, so nothing needs the permission.
    """
    if kind == schema.KIND_ENTITY_BODY:
        return _ToldFacts(
            lane=(entity_body.ENTITY_ZONE_PREFIX,),
            body_rewrite_allowed=frozenset(str(o.get("path", "")) for o in ops
                                           if str(o.get("path", ""))))
    if kind == schema.KIND_DELETE:
        return _ToldFacts(lane=deletion.lane_for(ops),
                          deletions_allowed=frozenset(deletion.deleted_paths(ops)),
                          expected_bytes=deletion.expected_bytes(ops),
                          provenance_pages=deletion.provenance_scrubs(ops))
    if kind == schema.KIND_ENTITY_ALIAS:
        return _ToldFacts(lane=entity_alias.lane_for(ops),
                          expected_bytes=entity_alias.expected_bytes(ops),
                          derived_files=entity_alias.derived_files(ops))
    return _ToldFacts(lane=gates.ALLOWED_WRITE_PREFIXES)


def _cross_check(entries, proposal: dict, *, kind: str, ops: list) -> None:
    """The diff must be EXACTLY what the row says, and nothing else.

    Two stored facts have to agree: `ops` produced the diff, `target_paths` says which pages it was
    allowed to touch. So an `ops` blob that DISAGREES with `target_paths` cannot reach `main` —
    that, and no more. Content is not compared, and nothing re-reads the row between the
    pending→approved transition and here, so a tamper that edited both columns consistently before
    this read is out of scope: write access to `repair_proposals` is the prerequisite either way,
    and this is a consistency check, not a defense against it.

    The SHAPE of the diff is checked per kind, because `target_paths` cannot express it. For the
    two editing kinds every entry must be `M`: they edit pages that already exist, so an addition
    or a deletion is not a repair that got out of hand, it is a diff nothing here can have
    produced. For `delete` the removals must be exactly the pages the plan named and the
    modifications exactly the pages it planned to scrub — a sweep that quietly stopped deleting, or
    that deleted a page it had only planned to rewrite, satisfies the path comparison perfectly.
    """
    touched = {e.path for e in entries}
    approved = {str(p) for p in (proposal.get("target_paths") or ())}
    if touched != approved:
        unexpected = sorted(touched - approved)
        missing = sorted(approved - touched)
        raise RepairError(
            "the change this proposal produced is not the change that was approved, so nothing "
            "was committed or pushed. "
            + (f"pages it touched that the approval did not name: {', '.join(unexpected)}. "
               if unexpected else "")
            + (f"pages the approval named that it did not touch: {', '.join(missing)}. "
               if missing else "")
            + "Propose again and approve the new proposal")
    if kind == schema.KIND_DELETE:
        _cross_check_delete(entries, ops)
        return
    if kind == schema.KIND_ENTITY_ALIAS:
        _cross_check_entity_alias(entries, ops)
    off_shape = sorted(f"{e.path} ({e.status})" for e in entries if e.status != "M")
    if off_shape:
        raise RepairError(
            f"this repair did something other than edit existing pages, so nothing was committed "
            f"or pushed: {', '.join(off_shape)}. Every op in this vocabulary is an addition to a "
            f"page that already exists")


def _cross_check_delete(entries, ops: list) -> None:
    """The sweep's own shape: removals == the plan's deletions, modifications == its scrubs, and no
    third status anywhere."""
    removed = sorted({e.path for e in entries if e.status == "D"})
    modified = sorted({e.path for e in entries if e.status == "M"})
    off_shape = sorted(f"{e.path} ({e.status})" for e in entries if e.status not in ("D", "M"))
    if off_shape:
        raise RepairError(
            f"this deletion did something other than remove and rewrite existing pages, so nothing "
            f"was committed or pushed: {', '.join(off_shape)}")
    if removed != deletion.deleted_paths(ops) or modified != deletion.scrubbed_paths(ops):
        raise RepairError(
            f"the pages this deletion actually removed are not the pages it was approved to "
            f"remove, so nothing was committed or pushed. approved to delete: "
            f"{', '.join(deletion.deleted_paths(ops)) or 'nothing'}; actually deleted: "
            f"{', '.join(removed) or 'nothing'}. Propose again and approve the new proposal")


def _cross_check_entity_alias(entries, ops: list) -> None:
    """A merge's own shape: the ABSORBED page is in the diff and it is a modification.

    The path comparison above cannot say this and it is the half that matters. A merge whose ops
    were edited to leave the absorbed page alone — re-anchoring its pages onto the survivor while
    the absorbed identity stays live and unmarked — names the same `target_paths` set only if the
    absorbed page is named in it, so the one thing to assert is that the diff actually carries it.
    A deletion's cross-check makes the identical argument about a sweep that quietly stopped
    deleting.
    """
    absorbed = entity_alias.absorbed_path(ops)
    modified = {e.path for e in entries if e.status == "M"}
    if absorbed and absorbed not in modified:
        raise RepairError(
            f"this merge did not mark {absorbed} as absorbed, so nothing was committed or pushed: "
            f"an identity is retired by being superseded, and a merge that re-anchors its pages "
            f"without doing so leaves the corpus with two live identities and no record of the "
            f"decision. Propose again and approve the new proposal")


def commit_message(proposal: dict, *, approved_by: str) -> str:
    """The commit an approved repair lands as. `Approved-by:` is half of how `git log` answers who
    authorized a change to the corpus, so the actor is collapsed to one line: the console passes a
    free-text one by design, and a newline in it would inject arbitrary commit-message lines — a
    second, forged trailer among them."""
    ops = proposal.get("ops") or ()
    findings = ", ".join(str(i) for i in (proposal.get("finding_ids") or ())) or "none recorded"
    return (f"{_commit_subject(proposal, ops)}\n\n{(proposal.get('rationale') or '').strip()}\n\n"
            f"Proposal #{proposal.get('id')}; findings {findings}.\n"
            f"Approved-by: {_trailer_actor(approved_by)}")


def _commit_subject(proposal: dict, ops) -> str:
    """One line of `git log` saying what an approval did. A deletion says so in the verb: the whole
    point of reading `git log` after one is finding out which pages left, and "3 edit(s) on
    wiki/notes/…" would name a page that no longer exists."""
    kind = str(proposal.get("kind") or schema.KIND_EDITS)
    if kind == schema.KIND_DELETE:
        removed = deletion.deleted_paths(ops)
        stems = [deletion.page_stem(path) for path in removed]
        first = stems[0] if stems else "the knowledge repo"
        return (f"chore(repair): delete {len(removed)} page(s) — {first}"
                + ("…" if len(stems) > 1 else ""))
    if kind == schema.KIND_ENTITY_ALIAS:
        # Both names, in the verb: reading `git log` after a merge is how somebody finds out which
        # identity absorbed which, and "4 edit(s) on wiki/entities/…" would name one of the two and
        # say nothing about the decision.
        survivor = entity_alias.page_stem(entity_alias.survivor_path(ops)) or "an entity"
        absorbed = entity_alias.page_stem(entity_alias.absorbed_path(ops)) or "another"
        return f"chore(repair): merge {absorbed} into {survivor}"
    targets = [str(p) for p in (proposal.get("target_paths") or ())]
    return (f"chore(repair): {kind} — {len(ops)} edit(s) on "
            f"{targets[0] if targets else 'the knowledge repo'}")


def _trailer_actor(approved_by: str) -> str:
    collapsed = " ".join(str(approved_by or "").split())
    if not collapsed:
        raise RepairError(
            "applying a repair needs a non-empty approver — the `Approved-by:` trailer is half of "
            "how `git log` answers who authorized a change to the corpus")
    return collapsed


def apply_approved(conn, repo_url: str, branch: str, credential, *, proposal: dict,
                   approved_by: str, on_output=None) -> dict:
    """`apply_via_clone` plus the two status writes that must never be forgotten around it.

    THE door every surface applies through, so "a failed apply is recorded as failed" is a
    property of the code rather than of each caller remembering. The approved status is NOT
    restored on failure: `error` says what went wrong, the row stays visible as `failed`, and a
    steward may propose again — a silent revert to pending would hide that a gate refused.

    **Both arms exist because either one alone strands the row.** A row left in `approved` after a
    fault is unreachable from every direction at once: a steward cannot decide it (it is not
    pending), the proposer will not re-derive it (its key is remembered), and nothing reports it.
    The residual — this process dying between `mark_decided` and the bookkeeping below — is what
    `docs/reference/operator-runbook.md` carries a guarded UPDATE for.
    """
    try:
        result = apply_via_clone(repo_url, branch, credential, proposal=proposal,
                                 approved_by=approved_by, on_output=on_output)
    except RepairError as ex:
        # `str(ex)` is safe to persist and to publish: every sentence raised above is written for
        # a steward and names no path of this host's (module docstring).
        store.mark_failed(conn, proposal["id"], str(ex))
        raise
    except Exception as ex:
        # The CLASS NAME only, and that is the difference from the arm above: this arm catches
        # everything nobody wrote a steward-facing sentence for, and an arbitrary exception's
        # message names paths, DSNs and row content. `error` is a steward-facing column. The
        # exception itself is re-raised untouched — each door maps it, and the console records the
        # original class in `admin_actions`.
        log.error("repair apply: an unanticipated fault; the proposal is recorded as failed",
                  exc_info=True)
        store.mark_failed(conn, proposal["id"], ex.__class__.__name__)
        raise
    store.mark_applied(conn, proposal["id"], result["commit"])
    return result
