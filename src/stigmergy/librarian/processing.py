"""Processing ONE capture: the filing path, from queue row to commit — or to a refusal.

The order is cheapest-first and refuse-early, because every step below the agent costs an agent
run:

    material -> retry collapse -> already-filed -> secrets/PII over the MATERIAL
             -> worktree -> agent -> apply the agent's DECLARED edits -> stamp server fields
             -> gates -> [one corrective retry] -> commit -> push -> filed

**Why code applies the edits.** The agent writes only new files; the reciprocal `related:` links and
overlap/contradiction callouts it wants on pages that already exist are named in its outcome and
performed here, between the agent and the gates (see `edits.py` for the two live runs that
settled it). They are not exempt from anything: they land in the same
diff, the gates run over the whole of it, and `gate_body_rewrite` judges code's work exactly as it
judged the agent's.

**Why the secrets scan runs over the material and not only over the diff.** A capture containing
a secret bounces WHOLE — whether or not the agent copied it onto the page — so scanning the
material is the required behaviour, not an optimization. It also happens to save
an agent run, which is why it sits above the worktree. The diff is scanned again afterwards, by
the gate, because that is the surface that would actually be committed.

**The one corrective retry**: the gates hand their findings back and the agent gets exactly one
more attempt. Not two — an agent that cannot satisfy deterministic
gates in two tries is not going to on the third, and the budget is per item.

**And not one, when no veto names a repair the agent can perform.** A pass spent on a finding the
agent cannot act on — a page it cannot write, a scanner that could not run — is not a chance, it is
a certainty of the same refusal one agent run later, and for `zone/body-rewrite` it hands back an
instruction to repair work code did (`gates.unrepairable`). The item then refuses after ONE pass
and the report says so.

**And the outcome's own SHAPE reaches that retry by the same road.** It did not: `parse_outcome`
raised `AgentError` for every shape problem, which finished the item, so the agent was never told
what was wrong and did the same thing on both passes — on the librarian's first real walk, over a
`summary` four characters too long. `errors.OutcomeShapeError` now carries findings and
`_run_in_worktree` treats them exactly as it treats a gate veto. What stayed an exception is what
telling the agent cannot fix: no outcome file at all, an unreadable one, one over the byte ceiling,
invalid JSON, nesting past the depth ceiling.

**The human loop, routed by CODE from the agent's declared outcome.** The agent declares
`triage: {kind: "unresolved-entity", name: ...}` when it cannot place a capture, and code decides
where that lands: on a capture that still has its one question, `_ask_or_park` asks the SUBMITTER
(`needs_input`, with a code-built question naming the registry's actual contents); on one whose
question is spent — because it was already asked, whatever happened next — it parks with the
STEWARD (`triage`) and never asks twice.
The budget is a database column (`asked_at`), so it survives a requeue and a lease redelivery, which
is precisely where a counter held in this process would not. A submitter's answer comes back in on
the next ordinary delivery and reaches the agent as fenced, labelled DATA (`_one_pass`).

**Terminal states split by cause, not by gate.** A secrets or PII match is the submitter's to act
on (`rejected`, naming the rule and the line). A zone veto on ordinary material is a system fault
(`failed`)
— the benign-twin rule says it must never fire on ordinary content, so if it does the librarian
malfunctioned, and telling the submitter to "fix and resubmit" would send them looping against a
bug. The same veto WITH a traceable steering attempt in the material is content-actionable
(`rejected`), because then something in the capture really did cause it. And two vetoes are
neither, because their honest destination is the steward's queue: an anchoring veto that is the
only thing still refusing is a park (`_unanchorable`), and so is a page minted in a folder the
fast lane may not create (`_uncreatable_type`). Both are destinations the cooperative agent
reaches by parking the capture itself.
"""
import asyncio
import dataclasses
import datetime
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

from stigmergy.capture import queue, schema
from stigmergy.capture.errors import CaptureError

# `MAX_BODY_LINES`/`SPLIT_CHUNK_LINES` are IMPORTED, not re-declared: the two NUMBERS the contract
# linter and this flow's own splitter must agree on come from one place, so they cannot drift the
# way independent literals that happen to match already have once.
from stigmergy.kernel import converters
from stigmergy.kernel.normalize import slugify
from stigmergy.kernel.page import MAX_BODY_LINES, SPLIT_CHUNK_LINES
from stigmergy.librarian import acl_rules, base_inputs, config, dedup, edits, gates, gitcmd, report
from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import page as page_policy
from stigmergy.librarian.errors import (
    AgentError,
    GitError,
    LeaseLostError,
    LibrarianError,
    OutcomeShapeError,
    StaleBaseError,
    WorktreeError,
)
from stigmergy.views import regenerate as views_regenerate

log = logging.getLogger(__name__)

# The first pass plus exactly one corrective retry. Defined in `config` because the visibility
# timeout is computed from it — two numbers that must agree do not live in two modules — and
# re-exported here under the name every call site already uses.
MAX_AGENT_ATTEMPTS = config.MAX_AGENT_ATTEMPTS


@dataclass
class Result:
    """What `process_item` decided. `error` is the queue column's one human sentence; `report` is
    the structured fact set. They agree by construction — both come from `report.py`.

    `diagnostics_path` is deliberately NOT part of `report`: it names a local file for an operator
    and has no business crossing to a submitter through `brain_submissions`.
    """
    status: str
    result_ref: str = ""
    report: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    diagnostics_path: str = ""
    # The agent's structured account, to be stored on `capture_queue.outcome` so a
    # re-file after a park can reuse it instead of re-reading the material. Set only on a PARK and
    # only when there is something worth keeping (`_with_park_outcome`); `None` everywhere else,
    # which `queue.finish` reads as "this caller has no outcome and must not blank the column".
    outcome: dict | None = None

    @property
    def error(self) -> str:
        """The `capture_queue.error` column means one thing: why the row is where it is.
        A filed row has no problem to report, so it stays empty and the report carries the news."""
        return "" if self.status == schema.FILED else self.report.get("summary", "")


@dataclass
class AgentPasses:
    """How many agent passes this item has STARTED — mutable, and shared with the caller on purpose.

    The count is known only inside the retry loop; the failure report is composed a layer up, in
    `worker.process_next`, from an exception. So the loop records the pass it is on here, and
    `process_item` stamps it onto any `LibrarianError` on its way out (`at_agent_attempt`). Without
    it the report named the queue delivery and nothing else, and "queue delivery 1" while the agent
    had had two tries is precisely the ambiguity `report.failed_system` was fixed to remove.
    """
    count: int = 0


@dataclass
class Deps:
    """Everything `process_item` needs that it does not own. Injected rather than constructed so
    a test can drive the whole path with a memory evidence store, a double agent and a scratch
    repo — no monkeypatching, matching the repo's settings/seam discipline."""
    settings: object
    evidence: object
    agent: object
    registry: object
    acl_config: object = None
    repo: str = ""
    today: str = ""             # injectable clock: `as_of` must be reproducible in a test

    def as_of(self) -> str:
        return self.today or datetime.date.today().isoformat()


def _material(deps: Deps, item: dict) -> str:
    """The archived material for this submission, from the evidence plane.

    Read from the blob rather than from `payload->>'text'` on purpose: the blob is what retention
    does NOT purge, so a filed page and the submitter's "resubmit the material that supports it"
    refer to the same bytes for as long as the page exists.
    """
    for key in item.get("blob_refs") or []:
        return deps.evidence.get(key).decode("utf-8", errors="replace")
    return (item.get("payload") or {}).get("text", "")


def _injection_categories(outcome) -> list[str]:
    """The categories the agent reported, filtered to the fixed set. Anything the agent invented
    is dropped rather than echoed — the report may name a category and nothing else."""
    found = []
    for finding in getattr(outcome, "findings", ()) or ():
        category = str((finding or {}).get("category", ""))
        if category in gates.INJECTION_CATEGORIES and category not in found:
            found.append(category)
    return found


def _stamp(ctx: gates.GateContext, deps: Deps, item: dict, *, cite_stem: str = "") -> dict:
    """Rewrite every NEW page's server-owned frontmatter, and return the values written.

    **The source attachment (`cite_stem`)**: pages in `ctx.provenance_pages` are skipped —
    `_stamp_attached_sources` already stamped them under the provenance group, and this group is
    labelled "(fast-lane pages)" for the same reason `stamp_source_fields`'s docstring gives in
    the other direction. Every page this loop DOES stamp additionally gains the `sources:`
    citation of `[[<cite_stem>]]` (`page.add_source_citation` — code guarantees the synthesis
    cites its verbatim source), a per-page declaration in `ctx.page_declared` (the outcome's
    own type and anchoring — required because populating `page_declared` for the source pages
    switches `gate_anchoring` to per-page mode, and the synthesis must keep being asked the
    anchoring question there), and a per-page stamped record in `ctx.stamped_by_path` including
    the merged `sources:` list, so `gate_frontmatter`'s output-equality check covers the citation
    like every stamped field. With `cite_stem` empty — every ordinary capture — none of that
    runs.

    Runs before the gates, so what the contract linter and the field checks see is the FINAL page
    — the one that would be committed — not the agent's draft. The returned dict is what
    `gate_frontmatter` re-reads the page against: the check and the write share one source of
    truth, so they cannot disagree about what the server said.

    `entity` is computed by `gates.resolve_entity_ids` from the anchoring outcome `gate_anchoring`
    is about to verify against the SAME call. **This is exactly two call sites
    (`resolve_entity_ids` here, and `report.filed`'s own, independent `_anchor_phrase`
    resolution) staying in sync by hand, NOT a single-source guarantee** — see
    `resolve_entity_ids`'s own docstring for what IS shared (the veto and the stamp) and what is
    not (the report's rendering).

    **Refuses to stamp a partial or empty-looking resolution for an `entity`-kind outcome.**
    `unresolved` non-empty, or `kind == "entity"` with nothing resolved at all, means
    `gate_anchoring` is about to veto this pass on the SAME call's own finding — this page will
    never reach a commit. Stamping `[]` here regardless (rather than the partially-resolved
    `ids`, which could be a plausible-looking non-empty list) is defence in depth: no reachable
    path lets that value survive to a committed page (first pass, corrective retry, final pass,
    `needs_input` reply and the cross-checks were all traced), but that invariant
    used to be held ENTIRELY by `gate_anchoring` running after this function and vetoing correctly
    — a fact this function had no way to check about itself. This line makes it true here too, so
    it survives the gate list being reordered or a bug in the other half.
    """
    anchoring = getattr(ctx.outcome, "anchoring", None) or {}
    kind = str(anchoring.get("kind", "")).lower() if isinstance(anchoring, dict) else ""
    entity, unresolved = gates.resolve_entity_ids(anchoring, deps.registry)
    if kind == "entity" and (unresolved or not entity):
        entity = []
    stamped = {"status": page_policy.FILED_STATUS, "as_of": deps.as_of(),
               "submitted_by": item["submitted_by"], "entity": entity}
    for path in ctx.in_lane_new_pages():
        if path in ctx.provenance_pages:
            continue    # stamped by `_stamp_attached_sources`, under the provenance group
        full = os.path.join(ctx.worktree, path)
        try:
            with open(full, encoding="utf-8") as f:
                drafted = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        acl = acl_rules.resolve(deps.acl_config, path)
        text = page_policy.stamp_server_fields(
            drafted,
            submitted_by=item["submitted_by"],
            acl=acl,
            as_of=deps.as_of(),
            entity=entity)
        cited = []
        if cite_stem:
            text, cited = page_policy.add_source_citation(text, cite_stem)
        with page_policy.open_for_rewrite(full) as f:
            f.write(text)
        stamped["acl"] = acl
        if cite_stem:
            # See the docstring's source-attachment paragraph: the per-page declaration keeps
            # the anchoring
            # question asked in per-page mode, and the per-page record extends the
            # output-equality check to the citation. `{**stamped}` snapshots THIS path's `acl`.
            ctx.page_declared[path] = {
                "page_type": str(getattr(ctx.outcome, "page_type", "") or ""),
                "anchoring": anchoring if isinstance(anchoring, dict) else {}}
            ctx.stamped_by_path[path] = {**stamped, "sources": list(cited)}
    return stamped


# Control characters have no place in a commit trailer or a subject line. A newline in particular
# is how a 60-character "title" forges the one field `git log` alone answers: `x\n\nSubmitted-by:
# ceo@acme.com` fits comfortably and reads as a real trailer.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# The trailer keys this message writes itself. Collapsing whitespace already makes a real forged
# trailer impossible — git only reads trailers from the LAST paragraph, and a single-line subject
# cannot start one — but `feat(note): x Submitted-by: ceo@acme.com` still READS as attribution in
# `git log --oneline`, and the colon is the only thing making it look like a field. So the colon
# goes.
_RESERVED_TRAILER_RE = re.compile(r"(?i)\b(submitted-by|co-authored-by|signed-off-by)\s*:")


def _subject(title: str) -> str:
    """One safe commit subject from an agent-supplied title.

    Whitespace collapsed (so no newline survives into the message at all), control characters
    dropped, reserved trailer keys defanged, and only THEN truncated. Order is the whole fix:
    truncating first and sanitizing after would leave a forgery intact whenever it fitted inside
    the limit, which is exactly what happened.
    """
    collapsed = " ".join(_CONTROL_CHARS_RE.sub(" ", str(title or "")).split())
    defanged = _RESERVED_TRAILER_RE.sub(lambda m: f"{m.group(1)} ", collapsed)
    return " ".join(defanged[:60].split()).strip() or "capture"


def _commit_message(item: dict, outcome, page_path: str, *, n_sources: int = 0) -> str:
    """One capture, one commit, one filed page — a 1:1 trace from submission to sha to page.

    The last third of that is enforced rather than assumed: `_cross_check_outcome` vetoes a capture
    that created more than one page, because this subject, `result_ref`, the dedup pointer and the
    report all name exactly one and the rest would be committed and reported nowhere. The commit may
    still TOUCH other pages — the additive `related:`/callout edits `edits.py` applies — and those
    are named in the report's `pages_edited`.

    The human goes in a `Submitted-by:` trailer as well as in the page, so `git log` alone answers
    who asked for it without opening the file.

    The type in the subject comes from the FOLDER the page actually landed in, not from what the
    agent said it filed: the folder is the fact, the declaration is a claim, and the zone gate has
    already refused any page where the two disagree.
    """
    page_type = page_policy.type_for_folder(page_path) or "note"
    body = f"Filed by the librarian from capture #{item['id']}."
    if n_sources:
        # The attached source part(s) ride in this same commit; the subject and `result_ref`
        # keep naming the ONE synthesis page (the human's door into the set, `_file_meeting`'s
        # own precedent), so the body is where the commit stops under-reporting what it carries.
        body += f" {n_sources} source page(s) — the captured thread, verbatim — ride in it too."
    return (f"feat({page_type}): {_subject(getattr(outcome, 'title', ''))}\n\n"
            f"{body}\n\n"
            f"Submitted-by: {item['submitted_by']}\n")


def _pre_agent(conn, item: dict, deps: Deps, *, material: "str | None" = None) -> tuple:
    """Everything BEFORE any flow's agent runs: dedup (levels 1-2) and the material-level
    secrets/PII scan. Shared by `process_item`, `process_meeting_item` and — through
    `process_item` — the drive flow: reused rather than duplicated, since a transcript, an ordinary
    capture and an extracted document collapse/dedup/scan on exactly the same terms; nothing about
    these four checks is per-flow.

    `material`: the drive flow CONVERTS before anything else and hands the extracted text
    in — dedup still keys on the payload's own sha256 (the manifest, content identity), and the
    scan runs over exactly the text that could reach a page. Every other caller passes nothing
    and reads the archived material as always.

    Returns `(material, None)` to continue, or `(material, Result)` when one of the checks already
    reached a terminal state.
    """
    settings = deps.settings
    material = _material(deps, item) if material is None else material

    # ── level 1: retry collapse (before the agent; no semantics at all) ──────────────────
    retry = dedup.find_retry(conn, item, window_s=settings.dedup_window_s)
    if retry:
        return material, Result(schema.FILED, retry.result_ref,
                                report.filed_retry(original_id=retry.submission_id,
                                                   page_path=retry.page_path,
                                                   commit=retry.commit))

    # ── level 2: already in the graph ───────────────────────────────────────────────────
    already = dedup.find_already_filed(conn, item)
    if already:
        return material, Result(schema.REJECTED, "",
                                report.rejected_duplicate(page_path=already.page_path,
                                                          as_of=already.as_of))

    # ── secrets and PII over the MATERIAL: bounce the whole capture ─────────────────────
    secret_hits = gates.scan_secrets(material, gitleaks_bin=settings.gitleaks_bin,
                                     label="your material")
    if secret_hits:
        # Read from `values`, never re-parsed out of `message`/`locator` — those are for humans,
        # and a hit that was only visible after adjacent lines were rejoined has no line number
        # in the submitter's own file to report.
        line, rule = secret_hits[0].values
        return material, Result(schema.REJECTED, "",
                                report.rejected_secret(line=line, rule_id=rule))

    pii_hits = gates.scan_pii([("your material", n, text)
                               for n, text in enumerate(material.splitlines(), start=1)])
    if pii_hits:
        hit = pii_hits[0]
        label = hit.message.split("what looks like ", 1)[-1].split(" near line")[0]
        return material, Result(schema.REJECTED, "",
                                report.rejected_pii(line=hit.locator.rsplit(":", 1)[-1],
                                                    pattern_label=label))
    return material, None


def process_item(conn, item: dict, deps: Deps, *, material: "str | None" = None) -> Result:
    """Take one claimed queue row all the way to a terminal state. Never raises for an ordinary
    refusal — every outcome is a `Result`. Only a genuinely unexpected error propagates, and the
    worker turns that into `failed` with the attempt count.

    `material`: see `_pre_agent` — the drive flow's pre-converted text; every other caller
    omits it."""
    material, early = _pre_agent(conn, item, deps, material=material)
    if early is not None:
        return early
    settings = deps.settings

    # ── the agent, in a throwaway worktree ──────────────────────────────────────────────
    # The ref is resolved and NAMED rather than left implicit. A service filing from the canonical
    # remote is correct; one silently diverging from the operator's local branch is not, and that
    # divergence cost a walk — see `gitcmd.BaseRef`.
    base = gitcmd.base_ref(deps.repo, settings.branch)
    # Deployed only: a base that did not come from the remote is a FAULT here, not a fallback.
    # `base_ref` answers a failed fetch with a `log.warning` and the local branch, which is right for
    # a laptop (a guard must not refuse the machine it was written for) and wrong
    # for a container: `bootstrap.verify_checkout_at_base` refuses exactly this state before the
    # first claim, and without this line a credential that expires an hour later silently walks the
    # deployed worker back into it for the rest of its life — judging captures against the ACL
    # config, registry and linter of a commit the remote moved past, while the governance flow
    # (`approve` -> push -> requeue) depends on that fetch working.
    #
    # Raising rather than returning a `Result` is the point: nothing about this capture caused it, so
    # the item is released rather than filed or refused (`StaleBaseError` carries the whole argument
    # for why it is the one config fault that stops the loop instead of failing the row).
    if settings.require_remote_base and not base.remote:
        raise StaleBaseError(
            f"the base resolved to the local {base.describe()} instead of origin/{settings.branch} "
            f"— the fetch failed, so this deployed worker would judge capture #{item['id']} against "
            f"the ACL config, entity registry and contract linter of a commit the remote may have "
            f"moved past hours ago. The likely cause is the GitHub App installation (revoked, or a "
            f"token that has expired since this container started) or the network. The capture is "
            f"left in the queue")
    log.info("filing submission %s against %s", item["id"], base.describe())

    # ── the registry AND the ACL config, read at THIS item's base commit ─────────────────────
    # `base_ref` fetched a moment ago, so `base` is the remote's tip: this is the read that makes
    # fetch-before-claim mean something. `worker.startup_checks` resolves both ONCE and that was
    # correct while nothing could rewrite either mid-run — but a steward's `stigmergy-entities approve`
    # now pushes a new entity between two polls, and a capture requeued by that same command would
    # otherwise be judged against the registry this worker read at startup, park again, and prove the
    # full circle broken for a reason that has nothing to do with the circle.
    #
    # **The ACL config is read here for the same reason and one worse consequence.** Reading it
    # once at startup looks safe — nothing in the PLATFORM rewrites it mid-run — but a steward
    # pushing a tightened `acl.json` to `main` does, and a long-running worker would then keep
    # stamping pages with the audience labels of the commit it booted from, for its whole lifetime.
    # That fails in the silently-OPEN direction: a rule made NARROWER on the remote is ignored, and
    # the page lands in a commit whose `acl.json` disagrees with the labels stamped on it. Both
    # inputs are pure functions of a commit, read at the commit being filed against, exactly like
    # the linter (`base_inputs`).
    #
    # `replace` rather than mutating `deps`: the per-item values are scoped to this item, and the
    # startup ones stay exactly what they were — which is what keeps the startup check an early
    # fail-closed refusal rather than a cache this line silently invalidates.
    deps = dataclasses.replace(deps,
                               registry=base_inputs.load_registry(deps.repo, base),
                               acl_config=base_inputs.load_acl(deps.repo, base))
    passes = AgentPasses()
    # The contract linter comes out of THIS item's base commit, not off the operator's disk
    # (`base_inputs`). Materialized here rather than once per run because `base` is resolved here:
    # the script that judges the diff is always the one in the commit the diff was built from, and
    # a linter fix pushed between two polls takes effect without a restart.
    with base_inputs.linter_at(deps.repo, base) as linter_path, \
            gitcmd.ephemeral_worktree(deps.repo, base.sha, settings.worktree_root) as worktree:
        try:
            return _run_in_worktree(conn, item, deps, material, worktree, passes,
                                    linter_path=linter_path)
        except LibrarianError as ex:
            # Annotate, then re-raise unchanged: the worker owns the decision to turn this into a
            # `failed` Result, and it needs to know how many agent passes were spent to say so
            # honestly. A bare `raise` keeps the original traceback.
            ex.at_agent_attempt(passes.count)
            raise


def _run_in_worktree(conn, item: dict, deps: Deps, material: str, worktree: str,
                     passes: "AgentPasses | None" = None, *, linter_path: str = "") -> Result:
    """The retry POLICY: one pass, one corrective pass, then refuse.

    The pass itself is `_one_pass`. Keeping the two apart is what lets an outcome whose SHAPE the
    boundary refused reach the corrective retry by the same road a gate veto takes — it used to
    escape the loop as an exception, so the agent was never told and both attempts were spent
    identically (`errors.OutcomeShapeError` carries the account).
    """
    settings = deps.settings
    corrective, findings, outcome, diagnostics = "", [], None, ""
    passes = passes if passes is not None else AgentPasses()

    for attempt in range(1, MAX_AGENT_ATTEMPTS + 1):
        passes.count = attempt
        try:
            result, findings, outcome = _one_pass(conn, item, deps, material, worktree, corrective,
                                                  linter_path=linter_path)
        except OutcomeShapeError as ex:
            # Unlike every other `AgentError`, SAYING SO can fix this one. The file itself is
            # already gone (`read_outcome` deletes before it parses), but a backend that parses its
            # own dict — the offline double does — may have left one behind.
            result, findings, outcome = None, list(ex.findings), None
            agent_module.discard_outcome_file(worktree)
        if result is not None:
            return result

        # A veto naming no repair the agent can perform does not spend the retry (`gates.
        # unrepairable`). The second pass could not clear it, so it would
        # reach this same terminal state one agent run later — and for the body-rewrite pair it
        # would hand the agent an instruction to repair work it did not do.
        blocked = gates.unrepairable(findings)
        if attempt < MAX_AGENT_ATTEMPTS:
            if not blocked:
                corrective = gates.corrective_brief(findings)
                _reset_for_retry(worktree)
                continue
            # Logged where the retry is SKIPPED, not wherever a blocking veto is seen: on the last
            # pass there is no retry to withhold, and saying "no corrective retry" about a run that
            # had already spent one would be the same counter confusion one level down.
            log.warning("item %s: skipping the corrective retry after agent pass %s — %s name(s) "
                        "no repair the agent can perform", item.get("id"), attempt,
                        ", ".join(sorted({f"{f.gate}/{f.code}" for f in blocked})))

        # Refused for good, and the worktree is about to be reaped — which is where the offending
        # diff used to go: the report said THAT a body was rewritten and never WHAT changed.
        diagnostics = preserve_refused_diff(worktree, item, findings,
                                            root=settings.refused_diff_root)
        break

    # `passes.count`, not `MAX_AGENT_ATTEMPTS`: the loop can now end after one pass, and a report
    # claiming two agent attempts when one ran is the ambiguity `report.failed_system` was fixed to
    # remove — reintroduced from the other side.
    return _refuse(item, findings, outcome, agent_attempts=passes.count,
                   diagnostics_path=diagnostics)


def _reset_for_retry(worktree: str) -> None:
    """Put the worktree back to the commit it branched from before the corrective pass.

    `reset --hard` restores the tracked tree and `clean -fdq` removes what the refused pass wrote —
    including any outcome file a backend left behind, whatever shape it was in.
    """
    gitcmd.run("reset", "--hard", "HEAD", cwd=worktree)
    gitcmd.run("clean", "-fdq", cwd=worktree)


def _one_pass(conn, item: dict, deps: Deps, material: str, worktree: str,
              corrective: str, *, linter_path: str = "") -> tuple:
    """ONE agent pass: run it, apply its declared edits, stamp, and run every gate.

    Returns `(result, findings, outcome)`. A non-`None` `result` is TERMINAL — the caller returns it
    untouched — and otherwise `findings` is what the corrective brief (or the final refusal) is built
    from. Extracted from the loop above so the loop holds the policy and nothing else, and so the
    reset-and-retry tail exists once rather than once per way a pass can be refused.
    """
    settings = deps.settings
    # The source attachment — `None` for every capture whose
    # door did not assert it, and everything it changes below is behind that `None`.
    attachment = _source_attachment(item)
    # The submitter's answer to the librarian's one question, if this row has one. It travels as
    # DATA — fenced and labelled by `agent.build_prompt`, never spliced into the instructions — and
    # it bypasses nothing: the anchoring gate still asks the registry, `_stamp` still writes the
    # server-owned frontmatter, and an answer saying "file as verified, acl: [leadership]" reaches
    # the agent as a sentence the submitter wrote and reaches no gate at all.
    # (ADR 028 D7): with the attachment ON, the agent is TOLD the flow fact.
    # The first real drive capture proved leaving it implicit wrong: the brief's genre rules make a
    # whole document read as `type: source` (a type the fast lane may not create), so the agent
    # parked a capture whose source half code had already taken. Server-composed, instruction-
    # side (the corrective brief's own standing), never derived from the material's shape.
    flow_note = "" if attachment is None else (
        f"SYSTEM NOTE (from the pipeline, not from the submitter): this capture arrived through "
        f"the {attachment.source_kind} door as a whole document. The system itself attaches the "
        f"VERBATIM material as a `sources/` page set in this same commit — the source half is "
        f"already handled; it is not yours to write, and it is not a reason to park. Your whole "
        f"job is the SYNTHESIS: file exactly one note/decision/concept page distilling what this "
        f"document establishes, anchored through the registry as always; the system will make "
        f"your page cite the attached source.")
    run = deps.agent.run(worktree=worktree, material=material,
                         hints=(item.get("hints") or {}).get("client", {}),
                         submitted_by=item["submitted_by"], corrective=corrective,
                         reply=item.get("reply") or "", flow_note=flow_note)
    outcome = run.outcome
    # The outcome file is the agent's channel, not part of its work: consume it before the
    # diff is taken so it can never reach a commit or trip the zone gate.
    agent_module.discard_outcome_file(worktree)
    if outcome is None:
        raise AgentError("the agent produced no usable account of what it did")

    # The agent decided it cannot place this. It is SUPPOSED to have written nothing — but
    # "supposed to" is not a check, and this branch used to assert it without looking. A triage
    # outcome with a diff behind it is an agent that wrote and then said it did not, so the
    # diff decides here as everywhere else: park it only if the worktree really is clean.
    # The attachment's source pages are not written until AFTER this check, on purpose: a parked
    # capture files nothing, so nothing may be in the worktree when the agent parks.
    if outcome.decision == "triage":
        stray = gitcmd.diff_entries(worktree)
        if stray:
            raise AgentError(
                f"the agent parked the capture but left {len(stray)} change(s) in the "
                f"worktree; nothing was filed and nothing was committed")
        return _triage(item, deps, outcome), [], outcome

    # With the attachment ON, the lane widens by exactly the attachment's own folder — the
    # same per-flow, on-the-context widening the meeting flow does (`GateContext.write_prefixes`'s
    # own comment), so every ordinary capture keeps the unwidened defaults.
    lane_kwargs = {}
    if attachment is not None:
        lane_kwargs = dict(
            write_prefixes=gates.ALLOWED_WRITE_PREFIXES + (attachment.prefix,),
            creatable_types=frozenset(page_policy.FAST_LANE_TYPES) | {"source"},
            extra_folder_types={attachment.prefix.rstrip("/"): "source"})
    ctx = gates.GateContext(
        worktree=worktree,
        entries=gitcmd.diff_entries(worktree),
        added=gitcmd.added_lines(worktree),
        material=material, outcome=outcome, registry=deps.registry,
        # The linter materialized from this item's base commit, handed down from `process_item` —
        # NOT `settings.linter_path`, which is where the script sits in somebody's working tree.
        linter_path=linter_path, gitleaks_bin=settings.gitleaks_bin, **lane_kwargs)

    if not ctx.entries:
        raise AgentError("the agent wrote nothing and did not park the capture")

    # ── code's own additive edits, from the agent's DECLARATION ──────────────────────
    # The agent wrote only new files; the reciprocal links and callouts it asked for on
    # existing pages are validated against the real graph and applied here, before the diff
    # the gates judge is taken. Nothing is exempted by this: the edits land in the same diff
    # and `gate_body_rewrite` reads them like anything else.
    edited, edit_findings = edits.apply_declared(
        worktree, outcome.edits, new_pages=ctx.in_lane_new_pages())

    # ── the attached source page(s), written by CODE after the agent's own work ─────────
    # After `apply_declared` so the agent's declared edits were validated against ITS pages
    # only, and before `_stamp` so the whole set is stamped and judged as one diff. A collision
    # takes the same road every finding takes; `_reset_for_retry` wipes these pages with the
    # rest of the pass, so the corrective pass re-writes them against its own outcome.
    written_sources = None
    if attachment is not None:
        written_sources = _write_attached_sources(worktree, attachment, outcome, material)
        if isinstance(written_sources, list):
            return None, written_sources, outcome
        ctx.provenance_pages = frozenset(written_sources["paths"])
        # With per-page declarations in play (`ctx.page_declared`, populated by the two stamp
        # calls below), `gate_anchoring` switches to per-page mode — so the synthesis page(s)
        # must carry the outcome's own declaration there or the anchoring question would
        # silently stop being asked of them. `_stamp` writes it (see its `cite_stem` half).
    ctx.entries = gitcmd.diff_entries(worktree)
    ctx.added = gitcmd.added_lines(worktree)

    if attachment is not None:
        _stamp_attached_sources(ctx, deps, item, written_sources["ids_by_path"])
        ctx.stamped = _stamp(ctx, deps, item, cite_stem=written_sources["stems"][0])
    else:
        ctx.stamped = _stamp(ctx, deps, item)

    # Re-read the diff: stamping changed the pages the gates are about to judge.
    ctx.entries = gitcmd.diff_entries(worktree)
    ctx.added = gitcmd.added_lines(worktree)
    findings = gates.run_gates(ctx) + edit_findings + _cross_check_outcome(ctx)
    if not gates.vetoes(findings):
        return (_file(conn, item, deps, ctx, outcome, findings, worktree, edited=edited,
                      source_pages=tuple(written_sources["paths"]) if written_sources else ()),
                [], outcome)
    return None, findings, outcome


# ── the refused diff, preserved ───────────────────────────────────────────────────────────────
# A veto reaps the worktree, so until now the offending diff was gone the moment it was refused:
# the report said THAT the agent rewrote a body and never WHAT it changed, and for a defect that
# will recur that is a debugging dead end.
#
# What is preserved is deliberately asymmetric, and the asymmetry is the safety property:
#
#  * REMOVED lines are kept verbatim. They are content already committed in this repo — the thing
#    that was about to be destroyed, and the only thing that answers "what did it change".
#  * ADDED lines are withheld entirely. They are the librarian's draft of untrusted captured
#    material, so writing them to a file beside the queue would be the same mistake as putting a
#    secret in a log. Their COUNT is kept, which is what a reader actually needs from them.
#
# Bounded twice (lines and bytes) because a diff is attacker-influenced in size.
REFUSED_DIFF_MAX_LINES = 200
REFUSED_DIFF_MAX_BYTES = 32 * 1024


def refused_diff_digest(worktree: str, item: dict, findings) -> str:
    """A bounded, redacted rendering of the diff that was refused."""
    entries = {entry.path: entry for entry in gitcmd.diff_entries(worktree)}
    added: dict[str, int] = {}
    removed: dict[str, list[str]] = {}
    budget, path = REFUSED_DIFF_MAX_LINES, ""
    for line in gitcmd.diff_text(worktree).splitlines():
        if line.startswith("+++ b/"):
            path = gitcmd.header_path(line, "+++ b/")
        elif line.startswith("--- a/"):
            path = gitcmd.header_path(line, "--- a/")
        elif line.startswith("+") and not line.startswith("+++"):
            added[path] = added.get(path, 0) + 1
        elif line.startswith("-") and not line.startswith("---"):
            kept = removed.setdefault(path, [])
            if budget > 0:
                kept.append(line)
                budget -= 1

    refused_by = ", ".join(sorted({f"{f.gate}/{f.code}" for f in gates.vetoes(findings)}))
    out = [
        "# stigmergy-librarian: a refused diff, preserved for diagnosis",
        f"# submission: {item.get('id')}   delivery: {item.get('attempts')}",
        f"# refused by: {refused_by}",
        "#",
        "# ADDED lines are withheld on purpose: they are this capture's material, and captured",
        "# material never goes into a log or a report (the same discipline as a secret). Removed",
        "# lines are content already committed in this repo, so they are shown verbatim — they are",
        "# what was about to be destroyed, which is the question this file exists to answer.",
        "",
    ]
    for entry_path, entry in sorted(entries.items()):
        gone = removed.get(entry_path, [])
        out.append(f"{entry.status} {entry.old_mode}->{entry.new_mode} {entry_path}  "
                   f"+{added.get(entry_path, 0)} (withheld) -{len(gone)}")
        out.extend(f"    {line}" for line in gone)
    return "\n".join([*out, ""])[:REFUSED_DIFF_MAX_BYTES]


def preserve_refused_diff(worktree: str, item: dict, findings, *, root: str) -> str:
    """Write the digest beside the queue and return its path (`""` when it could not be written).

    Never raises: this is diagnostics, and losing them must not change an item's outcome.
    """
    try:
        directory = config.refused_diff_dir(root)
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(directory, f"{item.get('id')}-{stamp}.diff")
        with open(path, "w", encoding="utf-8") as f:
            f.write(refused_diff_digest(worktree, item, findings))
    except (OSError, GitError) as ex:
        log.warning("could not preserve the refused diff for item %s (%s)",
                    item.get("id"), ex.__class__.__name__)
        return ""
    # The PATH only. Everything inside it that could carry captured material was already withheld,
    # and this line names a file rather than quoting one.
    log.error("item %s was refused; the refused diff is preserved at %s", item.get("id"), path)
    return path


def _cross_check_outcome(ctx: gates.GateContext) -> list:
    """The agent's account must AGREE with the diff — the diff decides.

    `page_path` used to be taken from the outcome with the diff as a mere fallback, and that
    string becomes `result_ref`, the submitter's report, the audit row and the pointer a future
    retry is collapsed onto. So an agent could report a page it never created — a whitespace-only
    edit to an existing page passes the body-rewrite gate, and the anchoring gate returns nothing
    when no page was created — and the row reached `filed` naming a path that does not exist.

    Three checks, all cheap: the outcome's `page_path` must be empty or a page the diff really
    created, any non-`triage` outcome must have created at least one page in the lane (filing is
    what `filed` means; an edit to somebody else's page is not a filing), and it must have created
    EXACTLY one.

    **Why exactly one.** `_file` takes `in_lane_new_pages()[0]` — the alphabetically first entry of
    `git diff --raw` — for `page_path`, `result_ref`, the commit subject, the dedup pointer and the
    whole report. A second page created in the same run would be committed, stamped and pushed while
    appearing on no surface a human reads, and `gate_anchoring` unions wikilinks across ALL new
    pages and requires only that one resolve, so a page with no anchor of its own would ride in on
    the first page's coat-tails past the anchoring check. "One capture, one commit" was true of
    commits and 1:N in pages. If multi-page filing is ever wanted, `report.filed` has to carry the
    list and `result_ref` has to name the set — a contract change, not a code change, and until it
    is made the veto is the honest position.

    **"Exactly one" means one AGENT page.** `ctx.provenance_pages` — the source attachment's
    code-written part(s), empty for every ordinary capture — is excluded from the count: those
    pages are named by the report's own `source_pages` list and cited from the synthesis, so the
    "committed and reported nowhere" argument does not apply to them, and counting them would
    veto every attached capture by construction.
    """
    new_pages = [p for p in ctx.in_lane_new_pages() if p not in ctx.provenance_pages]
    out = []
    if not new_pages:
        out.append(gates.Finding(
            "outcome", "no-page-created",
            "reported filing a capture but created no page in the fast lane's folders: an "
            "additive edit to an existing page is not a filing"))
    if len(new_pages) > 1:
        out.append(gates.Finding(
            "outcome", "multiple-pages",
            f"created {len(new_pages)} pages in one capture; a capture files exactly one page, and "
            f"every surface that reports it (result_ref, the commit subject, the dedup pointer) "
            f"names one — the others would be committed and reported nowhere",
            locator=", ".join(sorted(new_pages))))
    claimed = getattr(ctx.outcome, "page_path", "")
    if claimed and claimed not in new_pages:
        out.append(gates.Finding(
            "outcome", "page-path-mismatch",
            f"reported {claimed} as the filed page, but the diff created no such page",
            locator=claimed))
    return out


def _triage(item: dict, deps: Deps, outcome) -> Result:
    """Which parked report the agent's declaration earns — and WHICH HUMAN it is parked on.

    `agent.parse_outcome` refuses a parked outcome whose `kind` is neither documented one, and
    one missing the field its report has to name — so a `triage` reaching here carries both. The
    fallbacks stay as defence in depth (an `Outcome` can be constructed directly), the same posture
    `report._as_list` takes behind the same boundary; what they no longer do is quietly become the
    report an agent's silence used to earn: "unresolved-entity", about
    `schema.UNNAMED_ENTITY_PLACEHOLDER`.

    **The routing is CODE's, from the agent's DECLARED outcome.** The agent's job is to say "I
    cannot place this, and here is the name". What that costs the submitter is this function's
    decision, and it is a testable contract rather than a judgment: an `unresolved-entity` outcome
    on a capture that still has its question asks it; every other park, and the same one on a
    capture whose question is already spent, goes to the steward.

    **The injection findings travel on this road too.** They used to be composed only where a
    capture was FILED or REFUSED, so a capture whose material tried to steer the librarian AND
    which the agent then parked recorded the attempt nowhere: `triage` is a terminal state like any
    other, and two roads reach the same parked sentence (`_unanchorable` is the other) — a finding
    recorded on one and not the other is how two paths to one destination come to disagree about
    what happened. They go into the REPORT, which is the surface `queue.finish` persists and
    `brain_submissions` returns, as well as onto the `Result`.
    """
    parked = outcome.triage or {}
    rationale = getattr(outcome, "summary", "")
    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]
    if str(parked.get("kind", "")) == agent_module.TRIAGE_UNSUPPORTED_TYPE:
        return Result(schema.TRIAGE, "",
                      report.triage_type(judged_type=parked.get("judged_type") or "unknown",
                                         agent_rationale=rationale, findings=notes),
                      findings=notes)
    return _ask_or_park(item, deps, name=parked.get("name") or schema.UNNAMED_ENTITY_PLACEHOLDER,
                        agent_rationale=rationale, notes=notes)


def _ask_or_park(item: dict, deps: Deps, *, name: str, agent_rationale: str,
                 notes: list) -> Result:
    """The one-ask budget, spent or not — the whole routing rule, in one place.

    **`asked_at` is the budget and the database holds it.** It is stamped by `queue.finish` on the
    FIRST transition into `needs_input` and never cleared: not by the reply that returns the row to
    `queued`, not by a steward's requeue, not by a lease redelivery. So a capture that has been
    asked cannot be asked again by any road — including the two where a second question would be
    most tempting and most wrong: the submitter answered and the answer named something the registry
    still does not know, and a worker died mid-item and the row came back for another delivery.
    Holding the budget in code (a flag on the run, a count of passes) would survive neither.

    A capture whose question is spent parks in `triage` as an ENTITY situation — the steward's own
    queue, where an unregistered name belongs once the submitter has had their say. It is not a
    failure and the report does not read like one.
    """
    if item.get("asked_at"):
        return Result(schema.TRIAGE, "",
                      report.triage_entity(name=name, agent_rationale=agent_rationale,
                                           findings=notes, asked=True),
                      findings=notes)
    candidates = gates.registry_candidates(deps.registry)
    shown = candidates if len(candidates) <= report.MAX_QUESTION_CANDIDATES else []
    return Result(schema.NEEDS_INPUT, "",
                  report.needs_input(submission_id=item["id"], name=name, candidates=shown,
                                     total_candidates=len(candidates),
                                     agent_rationale=agent_rationale, findings=notes),
                  findings=notes)


def _file(conn, item, deps, ctx, outcome, findings, worktree, *, edited=(),
          source_pages=()) -> Result:
    """The gates passed: commit, push, and say what happened.

    `page_path` comes from the DIFF, never from the outcome — `_cross_check_outcome` has already
    refused any disagreement, so by here they are the same page, and taking the one the diff
    proves keeps it that way if that check is ever loosened.

    **`source_pages`**: the attachment's code-written part(s), in part order, from
    `_write_attached_sources`' own plan — never re-derived from the diff, where `sorted()` puts
    `-p2` before the bare stem (`_file_meeting`'s exact precedent). They are excluded when
    PICKING `page_path`: `sources/` sorts before `wiki/`, so the plain `[0]` would name the
    thread copy instead of the synthesis on every attached capture.

    `edited` is what `edits.apply` actually changed, and it goes into the report as `pages_edited`.
    It used to be bound in `_run_in_worktree` and dropped on the floor, which left the submitter's
    report unable to answer "what did this capture change besides its own page" — the commit touched
    a colleague's page and no surface a human reads said so.

    `outcome.summary` goes in as `agent_rationale` for the neighbouring reason: it is the agent's own
    account of WHY this page went where it went, and every other field in the report is code's
    observation of WHAT happened. It is the agent's claim, which is why it travels under a name that
    says so — the gates have already refused any disagreement between the claim and the diff.
    """
    from stigmergy.librarian import githubapp

    page_path = [p for p in ctx.in_lane_new_pages() if p not in ctx.provenance_pages][0]
    message = _commit_message(item, outcome, page_path, n_sources=len(source_pages))
    author_name, author_email = githubapp.identity()
    # The LOCAL sha. Deliberately not used for `result_ref`: `push` may rebase, which rewrites the
    # commit, and the sha that matters to a submitter is the one that landed on the branch.
    #
    # `gated_entries`: the diff the gates approved is the diff that lands, BYTES included.
    # `ctx` holds the entries `run_gates` was actually handed — each carrying the content
    # hash read before any gate subprocess ran — so this passes THOSE rather than re-deriving
    # them; a second derivation would be taken after the window it exists to check. See
    # `gitcmd.commit`.
    gitcmd.commit(worktree, message=message, author_name=author_name, author_email=author_email,
                  gated_entries=ctx.entries)

    remote_url, config_env = "", {}
    if githubapp.configured():
        slug = _repo_slug(deps.repo)
        remote_url = githubapp.push_url(slug)
        # Minted as late as possible and handed over in the ENVIRONMENT, never in argv.
        config_env = githubapp.push_config(githubapp.installation_token(), slug)

    # The last thing before the only irreversible step. A lease lost while we worked means the row
    # was redelivered and somebody else owns this capture; pushing anyway is how one capture gets
    # filed twice, with the second page referenced by no queue row. `finish` would refuse the row
    # afterwards — correctly, and far too late, because the commit is already on `main`.
    if not queue.holds_lease(conn, item["id"], expected_attempts=item["attempts"]):
        raise LeaseLostError(
            f"the lease on submission {item['id']} (delivery {item['attempts']}) was lost while "
            f"this item was being processed; nothing was pushed")
    # THE sha, from the push: `page_path@sha` has to name a commit a human can `git show`, and
    # after a rebase-and-retry the pre-push sha names nothing reachable. Found by the docker e2e
    # with two workers racing — see `gitcmd.push`.
    sha = gitcmd.push(worktree, branch=deps.settings.branch, remote_url=remote_url,
                      config_env=config_env, author_name=author_name, author_email=author_email)

    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]
    notes += [f.message for f in findings if f.severity == gates.SEVERITY_NOTE]
    return Result(
        schema.FILED, f"{page_path}@{sha}",
        report.filed(page_path=page_path, commit=sha,
                     anchoring=outcome.anchoring or {}, registry=deps.registry,
                     links=list(outcome.links_created),
                     overlaps=[dict(o) for o in outcome.overlaps],
                     pages_edited=list(edited),
                     agent_rationale=getattr(outcome, "summary", ""),
                     findings=notes,
                     source_pages=list(source_pages)),
        findings=notes)


def _repo_slug(repo: str) -> str:
    """`owner/name` from the checkout's `origin`, for the App's push URL."""
    url = gitcmd.origin_url(repo)
    slug = url.rsplit(":", 1)[-1] if url.startswith("git@") else url.split("github.com/")[-1]
    return slug.removesuffix(".git")


def _refuse(item, findings, outcome, *, agent_attempts: int = 0,
            diagnostics_path: str = "") -> Result:
    """Both attempts vetoed. Which terminal state depends on WHY, not on which gate."""
    veto = gates.vetoes(findings)
    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]

    # Selected by CODE, not by gate. The secrets and PII gates also emit `unscanned-diff` — "the
    # diff produced no readable added lines, so I refused rather than pass unscanned" — which is a
    # SYSTEM fault, not a finding about the submitter's material. Routing it through
    # `rejected_secret` would tell a person gitleaks matched a secret in their capture when it
    # matched nothing at all, and send them hunting for a credential that is not there.
    secret = next((f for f in veto if f.code == "secret"), None)
    if secret:
        return Result(schema.REJECTED, "",
                      report.rejected_secret(line=secret.locator.rsplit(":", 1)[-1],
                                             rule_id=secret.message.rsplit("rule: ", 1)[-1]
                                             .rstrip(")"),
                                             where="the drafted page"),
                      diagnostics_path=diagnostics_path)
    pii = next((f for f in veto if f.code == "pii"), None)
    if pii:
        label = pii.message.split("what looks like ", 1)[-1].split(" near line")[0]
        return Result(schema.REJECTED, "",
                      report.rejected_pii(line=pii.locator.rsplit(":", 1)[-1],
                                          pattern_label=label, where="the drafted page"),
                      diagnostics_path=diagnostics_path)

    zone = next((f for f in veto if f.gate == "zone"), None)
    categories = _injection_categories(outcome)
    if zone and categories:
        # The veto fired AND the material carries a traceable steering attempt: content-
        # actionable, so the submitter is told what in their capture caused it.
        return Result(schema.REJECTED, "",
                      report.rejected_steering(path=zone.locator, category=categories[0],
                                               findings=notes),
                      diagnostics_path=diagnostics_path)

    if _frontmatter_only(veto):
        # Content-caused, not the librarian failing at its job — see `_frontmatter_only`'s own
        # docstring. This decides only the DESTINATION; the refusal itself already failed CLOSED
        # (nothing ambiguous was committed). `unparseable` alone keeps its own sentence (the
        # shape-repair instruction);
        # `forged-field`/`forbidden-field` (with or without `unparseable` alongside) get the
        # "you declared a field the server owns" sentence instead — same reason code, because a
        # read path may only branch on that, not on prose.
        codes = {f.code for f in veto}
        builder = (report.rejected_malformed_frontmatter if codes == {"unparseable"}
                  else report.rejected_forged_field)
        return Result(schema.REJECTED, "", builder(findings=notes),
                      diagnostics_path=diagnostics_path)

    # ── the two parks: a refusal whose honest destination is the steward's queue ────────
    # Each requires its veto to be the WHOLE story, so they are mutually exclusive by construction
    # and their order here decides nothing.
    uncreatable = _uncreatable_type(veto)
    if uncreatable:
        return Result(schema.TRIAGE, "",
                      report.triage_type(judged_type=uncreatable,
                                         agent_rationale=getattr(outcome, "summary", ""),
                                         findings=notes),
                      findings=notes, diagnostics_path=diagnostics_path)

    unanchorable = _unanchorable(veto)
    if unanchorable:
        # **A veto that survived the last pass goes to the STEWARD, not to the submitter.** The
        # question is routed from the agent's DECLARED outcome (`_triage`) and from nothing else:
        # an anchor the agent attempted and could not land is a gate's verdict about a page, and
        # turning a gate's verdict into a question to a non-technical person would be
        # `anchoring_brief`'s audience confusion in the other direction. The two roads still
        # converge on one destination and one sentence — `triage`, `report.triage_entity`; only
        # WHICH human is waited on differs, and only for the road the agent named.
        return Result(schema.TRIAGE, "",
                      report.triage_entity(name=unanchorable.locator,
                                           agent_rationale=getattr(outcome, "summary", ""),
                                           findings=notes, asked=bool(item.get("asked_at"))),
                      findings=notes, diagnostics_path=diagnostics_path)

    # Anything else — a zone veto on ordinary material, a binary page, a linter error, an
    # anchoring outcome the agent never declared, an outcome that disagrees with the diff — is the
    # librarian failing at its job, not the submitter's problem.
    worst = veto[0] if veto else None
    return Result(schema.FAILED, "",
                  report.failed_system(attempts=item.get("attempts", 1),
                                       agent_attempts=agent_attempts,
                                       stage=worst.gate if worst else "gates",
                                       reason=worst.message if worst else "unknown"),
                  findings=notes, diagnostics_path=diagnostics_path)


def _uncreatable_type(veto) -> str:
    """Is "the fast lane cannot create that type" the WHOLE story — and which type is it?

    Then the item is parked, not failed. Criterion 4 already says where a governed type belongs:
    *"a capture the librarian judges to be an `entity`, `meeting`, `metric`, `dataset`, `person`,
    `team`, `product`, `customer`, `policy` or `source` lands in `triage` with the reason — never
    silently downgraded to `note` and never filed"*. The agent that RECOGNISES the type parks the
    capture and lands exactly there (`_triage`, `unsupported-type`); the one that writes the page
    anyway was told the librarian broke. Same capture, same news, two destinations, with the worse
    one reached by the agent trying harder — the identical asymmetry `_unanchorable` closed for
    anchoring, and it needed no measurement to settle because **the destination is mechanically
    derivable**: the folder the page landed in supplies `judged_type`, so nothing here is judged.

    Returns the TYPE rather than the finding, because that is the whole of what the routing needs
    and it comes from the path — `page.type_for_folder` is the same inverse lookup `gate_zone` used
    to decide the veto in the first place. `""` when the veto is not the whole story or the folder
    names nothing, which is `failed`: a park about a type nobody can name tells a steward less than
    an honest system fault does.

    **Only when it is the whole story**, for the reason `_unanchorable` gives at greater length: a
    park says "this material is fine, it just belongs elsewhere", and it must not bury a second,
    real fault. Nothing co-occurs with this veto by construction — `_check_created_type` returns at
    the first refusal, so it is the only finding that page produces — so unlike the anchoring park
    there is no companion veto to admit, and any second veto forces `failed`.

    Today this cannot fire: `gate_zone._check_created_type` documents why (both derived views of
    `page.PAGE_TYPES` currently agree, so `ensure_creatable` cannot raise for a type
    `type_for_folder` returned). It is the routing that belongs beside the guard, written while the
    argument is in front of us rather than the day the table grows a governed foldered type.
    """
    uncreatable = ("zone", gates.TYPE_NOT_CREATABLE)
    kinds = {(f.gate, f.code) for f in veto}
    if kinds != {uncreatable}:
        return ""
    finding = next(f for f in veto if (f.gate, f.code) == uncreatable)
    judged = page_policy.type_for_folder(finding.locator)
    # And the derived type must still be one the fast lane may not create. The gate's contract
    # already says so, and asking the shared table again is what keeps this routing from inventing
    # a park the moment the two disagree: "this reads like a note page, and note needs a steward's
    # review" is the sentence a silent disagreement would produce.
    return "" if page_policy.classify_page_type(judged).creatable else judged


def _unanchorable(veto) -> "gates.Finding | None":
    """Is "nothing on this page anchors" the WHOLE story of this refusal, and does it name what?

    Then the item is parked, not failed. After two passes that is the honest outcome — the
    submission goes to `triage` with the unresolved name as its open question — and it is the
    destination the cooperative half already reaches — an agent that SEES resolution will fail
    parks itself and lands in `triage`, while one
    that attempts an anchor and cannot land it used to be told the librarian could not finish. That
    was untrue, it was the worse of two available destinations, and it was reached by the agent
    trying HARDER. The two paths now converge.

    **Only when that is the WHOLE story**, which is why this is a set membership test and not a
    search for one finding. Parking says "a steward registers an entity and this material is fine";
    if the librarian also rewrote somebody's body, wrote a page git calls binary or produced an
    outcome that disagrees with its own diff, that sentence would bury a real fault under a routine
    one. `failed` stays correct there — and either way nothing is committed and the refused-diff
    digest names every gate that fired.

    **A `dead_links` finding does not automatically ride along, and the rule for when it may is
    narrow.** This used to admit the linter's `dead_links` finding alongside the anchoring veto
    unconditionally, justified by an implication that held only under an older mechanism:
    `gate_anchoring` used to refuse after finding that NO wikilink on the page resolves, so every
    link on it named something unregistered, and a dead one named something with no page either —
    the two findings were the same fact in two vocabularies, for a page this capture created.

    That mechanism is gone. `gate_anchoring` checks the DECLARED `anchoring.entities` list against
    the registry and never reads the page's links at all — so an unresolved declared id says
    nothing about whether the page's own wikilinks are healthy, IN GENERAL. But the librarian
    skill still instructs the agent to carry a wikilink to the entity it declares, so the ORDINARY
    case is still an agent that writes
    `[[Acme Ventures Inc]]` and declares `"entities": ["Acme Ventures Inc"]` in the same breath: one
    unresolved id, one dead link, the SAME name, on a page created for the sole purpose of being
    about that entity. Routing that combination to `failed` — "the librarian broke" — for the
    exact case a park exists for would be wrong. But an unrelated dead link elsewhere on the page
    (a stray link in prose about something else entirely) is a SEPARATE content defect that has
    nothing to do with the missing registry entry, and admitting THAT would tell a steward "a
    steward registers an entity and this material is fine" about a page that also carries a real,
    unrelated fault.

    So the rule is narrower than "any dead_links finding is fine" and narrower than "no dead_links
    finding is ever fine": every OTHER veto finding alongside the anchoring one must be a
    `("contract", "dead_links")` finding whose target (`gates.dead_link_target`) names ONE OF the
    values this anchoring veto could not resolve (`Finding.values`, matched via
    `gates.normalize_identifier` — the agent that writes the wikilink and the outcome from the
    same judgment need not spell it identically down to case, accent composition or incidental
    spacing). Anything else — an unrelated dead link, a binary-page veto, more than one kind of
    companion finding — means this anchoring veto is not provably the whole story, and the refusal
    falls through to `failed` (the librarian's job, surfaced rather than glossed over), exactly as
    it did before this fix for every combination that fails this test.

    **A SET test over `Finding.values` (verbatim), not a single-string equality over
    `Finding.locator` (a DISPLAY string).** `anchor.locator` comes from
    `gates._unresolved_name`, built for a human/prompt to read: it returns only the FIRST
    unresolved value, sanitized, whitespace-collapsed and clamped to `MAX_BRIEF_NAME_LEN` (80)
    characters with an ellipsis. Comparing THAT against a dead-link target broke three ways, all
    of them routing a legitimate park to `failed`:
    - **Plural anchors** — `entity:` is plural (one to three is the expected shape).
      Two unregistered entities produce one anchoring veto and TWO `dead_links`
      vetoes; the second always failed the single-locator equality. `values` carries every
      unresolved id, not just the one `_unresolved_name` happened to pick for display.
    - **NFC vs NFD** — the old comparison casefolded without normalizing. `[[Nestlé]]` in NFD
      against a declared NFC `Nestlé` differ byte-for-byte despite being the same name — exactly
      the accent-composition question `page.path_key` already exists to answer for a path, and
      `gates.normalize_identifier` now answers the same way for an identifier.
    - **The 80-char clamp** — any declared name longer than `MAX_BRIEF_NAME_LEN` (`agent`'s own
      `MAX_IDENTIFIER_LEN` allows up to 400) never matched its own un-clamped dead-link target.

    **And only when there is something to match.** `values` is empty exactly when NOTHING was
    declared at all (`kind: "entity"` with an empty `entities` list) — `gates._unresolved_name`
    still gives that case a non-empty display `locator` (`"something unnamed"`, for the steward
    park), but there is no real value for a companion dead link to name. A LONE anchoring veto
    (no companion findings at all) still parks regardless of `values` — that guard only matters
    once there is something else in `veto` this function must decide whether to admit, and an
    empty `values` there means nothing here matches, the conservative direction (falls through to
    `failed`) rather than the old bug's opposite mistake of a literal `[[something unnamed]]`
    wikilink coincidentally matching the placeholder string.

    The two other anchoring codes are deliberately NOT routed here. `undeclared` and `no-reason`
    are malformed outcomes — the agent declared no anchoring judgment at all — so there is no
    unresolved name and nothing for a steward to resolve; they remain a system fault.
    """
    unresolved = ("anchoring", gates.ANCHORING_UNRESOLVED)
    dead_link = ("contract", gates.DEAD_LINKS_CHECK)
    anchor = next((f for f in veto if (f.gate, f.code) == unresolved), None)
    if anchor is None or not anchor.locator:
        return None
    others = [f for f in veto if (f.gate, f.code) != unresolved]
    if not others:
        return anchor
    if not anchor.values:
        return None
    wanted = {gates.normalize_identifier(v) for v in anchor.values}
    for finding in others:
        if (finding.gate, finding.code) != dead_link:
            return None
        target = gates.dead_link_target(finding)
        if not target or gates.normalize_identifier(target) not in wanted:
            return None
    return anchor


def _frontmatter_only(veto) -> bool:
    """Is "the frontmatter gate refused this page" the WHOLE story of this refusal?

    Findings cycle 1, 4.7 established the reasoning for `("frontmatter", "unparseable")` alone:
    `page._strip_keys` drops a dropped key's TOP-LEVEL line correctly but, for a value spanning
    multiple lines whose continuation is not indented under its key (`entity: [` / `"acme"` / `]`,
    each on its own unindented line — legal-looking but not the shape this repo's line-based
    dialect expects), the continuation lines are not recognized as part of what is being dropped
    and survive as a stray fragment. `stamp_server_fields` then appends its own well-formed lines
    around that fragment, and the result is frontmatter a real YAML parser cannot read at all.
    Every swallow attempt there already failed CLOSED (nothing ambiguous is ever committed); what
    was wrong was the DESTINATION — routing to `failed` ("the librarian broke") for content the
    capture itself supplied in a shape this parser was never going to represent.

    **Findings cycle 2, B5: that reasoning is not specific to `unparseable` — it is a property of
    the GATE.** `gate_frontmatter` also produces `forged-field` (a server-owned field declared with
    the wrong value, declared twice, or via a construct this dialect refuses outright — a BOM, YAML
    explicit-key syntax) and `forbidden-field` (`owner`/`id`/`content_hash`, never legitimate on a
    fast-lane page). Both are exactly as content-caused as `unparseable`: the material tried to
    assert something the server computes itself, and the librarian did its job correctly by
    refusing it. Routing THOSE to `failed` — while `unparseable` from the very same gate routes to
    `rejected` — was the asymmetry 4.7 closed for one code and left standing for its two siblings,
    and it gets more likely to matter now that B1 moved some of that catch INTO this gate. This is
    still a set-membership test, same posture as `_uncreatable_type`/`_unanchorable`: only when
    every finding in the veto comes from the `frontmatter` gate (whatever the mix of its three
    codes) is it provably content-caused rather than a symptom riding beside a real system fault —
    a `binary-page` veto (or any other gate) alongside it still falls through to `failed`.
    """
    return bool(veto) and all(f.gate == "frontmatter" for f in veto)


def failure_result(item: dict, stage: str, reason: str, *, agent_attempts: int = 0) -> Result:
    """The worker's own wrapper for an unexpected error — a dead worktree, a git failure, an
    agent that never produced an outcome. Always a system fault, never the submitter's.

    `agent_attempts` defaults to 0 because these faults are raised from anywhere in the path,
    including before the agent ever ran; the report then omits the agent counter rather than
    guessing at it, and still names the queue delivery.
    """
    return Result(schema.FAILED, "",
                  report.failed_system(attempts=item.get("attempts", 1), stage=stage,
                                       reason=reason, agent_attempts=agent_attempts))


# The failures that are KNOWN ways processing can fail, so the report can name the stage
# instead of shrugging "unexpected". `CaptureError` is in here for `EvidenceError` above all:
# a row whose evidence blob is gone (retention purged it, or the row was written against a
# different evidence store) is an ordinary, diagnosable fault — the material cannot be read, so
# nothing can be verified against it — and calling that "unexpected" would send an operator
# looking for a bug that is not there.
PROCESSING_ERRORS = (AgentError, GitError, WorktreeError, LeaseLostError, CaptureError)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The fast lane's source ATTACHMENT: a parameter, never a third flow.
#
# The door rule: material with independent documentary existence files a `sources/` page beside
# the synthesis; a conversational capture leaves none. The Slack door was the first on the
# "documentary" side of that line, and the shape it fixed is reused by every door since: the
# source page is a fast-lane PARAMETER built from the meeting flow's own pieces —
# `_build_source_parts` writes the verbatim part(s), `page.stamp_source_fields` stamps the
# provenance group, `GateContext.provenance_pages` tells the gates which pages carry it — and the
# synthesis cites the source in `sources:` (`page.add_source_citation`, applied by `_stamp`).
# The drive flow reuses the same writer.
#
# With the parameter OFF (`_source_attachment` returns None — every MCP capture, and every door
# until it opts in), the fast lane builds exactly the ctx it would without any of this.
# ═══════════════════════════════════════════════════════════════════════════════════════════════

SLACK_SOURCE_PREFIX = "sources/slack/"
DRIVE_SOURCE_PREFIX = "sources/drive/"


@dataclass(frozen=True)
class SourceAttachment:
    """Which `sources/` page set one fast-lane capture attaches, and the provenance it carries.

    Built ONLY from facts a DOOR asserted server-side: `source_client`/`source_permalink` are
    refused at the client seam for every door but Slack's own
    (`capture.schema.reject_source_provenance_hints`, `BrainService.door`), which is what makes
    keying a FLOW decision on a hint sound — the hint stopped being client-writable the moment it
    became load-bearing."""
    prefix: str          # the zone folder, with its trailing slash ("sources/slack/")
    source_kind: str     # the contract's `source_kind:` enum value ("slack")
    tags: tuple          # the source page's frontmatter tags
    url: str             # `url:` on every part — the Slack permalink; "" when the door sent none
    suffix: str          # "thread": titles read "<title> — thread", stems "<slug>-thread"


def _source_attachment(item: dict) -> "SourceAttachment | None":
    """The parameter's ON/OFF switch, decided per item from facts a DOOR asserted server-side.
    `None` — the OFF position — for every ordinary capture (an MCP snippet files a synthesis and
    nothing else). Two ON positions today: the Slack door (keyed on the `source_client` hint,
    refused at the client seam for every other door) and the drive flow (ADR 028), keyed on the
    ROW'S OWN
    `kind`: `"drive"` is only ever written by the `stigmergy-drive` operator CLI
    (`schema.MCP_SUBMIT_KINDS` keeps it unreachable through `brain_submit`), which makes the
    kind itself the strongest server-asserted fact available — no hint consulted to decide."""
    client = (item.get("hints") or {}).get("client") or {}
    if item.get("kind") == schema.DRIVE:
        return SourceAttachment(prefix=DRIVE_SOURCE_PREFIX, source_kind="google-drive",
                                tags=("source", "drive-document"),
                                url=str(client.get("drive_url") or ""), suffix="document")
    if client.get("source_client") != schema.SLACK_DOOR:
        return None
    return SourceAttachment(prefix=SLACK_SOURCE_PREFIX, source_kind="slack",
                            tags=("source", "slack-thread"),
                            url=str(client.get("source_permalink") or ""), suffix="thread")


def _write_attached_sources(worktree: str, attachment: SourceAttachment, outcome,
                            material: str) -> "dict | list":
    """CODE writes the attached source page(s), verbatim from the archived material — the fast
    lane's half of what `_write_meeting_pages` does for the meeting set, through the same writer
    (`_build_source_parts`) and the same collision discipline: paths are checked against the
    repo's existing pages first, and a collision returns one veto finding (the corrective-retry
    road) with nothing written.

    The stem comes from the agent's own `outcome.title` — the one judgment call in here, and it is
    the SAME judgment the agent already makes for the synthesis page's filename; slugified, so it
    carries the same trust. A recaptured thread whose title slugifies to an existing source stem
    is refused rather than suffixed: the brief tells the agent the path exists, and a different
    title is its own repair.

    Returns `{"stems": [...], "paths": [...]}` in part order (1, 2, ...), the order
    `_file`'s report and the `sources:` citation both rely on.
    """
    title = str(getattr(outcome, "title", "") or "").strip() or "Capture"
    stem = f"{slugify(title) or 'capture'}-{attachment.suffix}"
    parts = _build_source_parts(stem, f"{title} — {attachment.suffix}", material,
                                source_kind=attachment.source_kind, tags=attachment.tags,
                                url=attachment.url)
    paths = [f"{attachment.prefix}{part_stem}.md" for part_stem, _pid, _text in parts]
    existing = page_policy.path_keys(gitcmd.tracked_paths(worktree))
    collisions = sorted({p for p in paths if page_policy.path_key(p) in existing})
    if collisions:
        return [gates.Finding(
            "outcome", "existing-page-collision",
            f"the source page(s) this capture would attach already exist in the repo: "
            f"{', '.join(collisions)} — most likely this thread was captured before; a different "
            f"page title yields a different source stem",
            locator=", ".join(collisions))]
    for path, (_stem, _pid, text) in zip(paths, parts, strict=True):
        _write_new(worktree, path, text)
    return {"stems": [part_stem for part_stem, _pid, _text in parts], "paths": paths,
            "ids_by_path": {path: pid for path, (_stem, pid, _text)
                            in zip(paths, parts, strict=True)}}


def _stamp_attached_sources(ctx: gates.GateContext, deps: Deps, item: dict,
                            ids_by_path: dict) -> None:
    """`_stamp_meeting`'s source-page loop, for the fast lane's attachment: per-page `source`
    declarations, `page.stamp_source_fields` over each part (the provenance group, never the
    fast-lane group `_stamp` writes), and the per-page stamped record `gate_frontmatter` checks
    output-equality against. `tier` stays the default `"1"`: a captured thread is a direct
    recording of the conversation itself — primary, exactly like the meeting flow's transcript.

    Iterates `ctx.provenance_pages`, which the caller populated from what
    `_write_attached_sources` just wrote — the same told-not-inferred posture as
    `_stamp_meeting`. `ids_by_path` is the producer's own explicit
    chain identity per part, stamped as `id:` and recorded so the gate's output-equality check
    covers it like every other stamped field."""
    digest = hashlib.sha256((ctx.material or "").encode("utf-8")).hexdigest()
    extracted_at = datetime.datetime.now(datetime.UTC).isoformat()
    for path in sorted(ctx.provenance_pages):
        page_id = str(ids_by_path.get(path) or "")
        ctx.page_declared[path] = {"page_type": "source"}
        _rewrite(ctx.worktree, path, lambda text, d=digest, e=extracted_at, pid=page_id:
                 page_policy.stamp_source_fields(text, submitted_by=item["submitted_by"],
                                                 as_of=deps.as_of(), content_hash=d,
                                                 extracted_at=e, page_id=pid))
        ctx.stamped_by_path[path] = {
            "status": page_policy.FILED_STATUS, "as_of": deps.as_of(),
            "submitted_by": item["submitted_by"],
            "content_hash": f"sha256:{digest}", "extracted_at": extracted_at, "tier": "1",
            **({"id": page_id} if page_id else {})}


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The drive flow (ADR 028): conversion at the worker, then the fast lane with the source
# attachment ON. NOT a third flow: everything from `_pre_agent` onward is `process_item` itself,
# byte for byte — the drive-specific code is exactly the bytes→text step below and the
# `_source_attachment` drive branch above. Kernel hands do the extraction (deterministic,
# text-layer first); `vision_extract` is the code-decided, once-per-document fallback for
# scanned PDFs; a conversion fault is a NAMED stage (`conversion`), never an exception loop and
# never a submitter-blaming report.
# ═══════════════════════════════════════════════════════════════════════════════════════════════

# A text-layer PDF yields well over this per page; a scanned one yields almost nothing. The
# form-feed count is pdftotext's own page marker, so the heuristic needs no second parse.
DRIVE_VISION_MIN_CHARS_PER_PAGE = 200
# Below this many characters TOTAL the extraction is unusable outright — with no vision
# capability the honest answer is a refusal, not an agent pass over empty text.
DRIVE_MIN_TEXT_CHARS = 50


class _ConversionRefused(Exception):
    """A drive conversion that cannot proceed. `str(self)` is the WIRE sentence (reaches the
    submitter through `Result.report` — no paths, no `str(exception)`, the R5 rule);
    `log_detail` is the operator's, logged where it is raised or caught."""

    def __init__(self, wire: str, log_detail: str = ""):
        super().__init__(wire)
        self.log_detail = log_detail or wire


def process_drive_item(conn, item: dict, deps: Deps) -> Result:
    """`process_item`'s sibling for `kind == "drive"` rows (ADR 028 D4): convert FIRST —
    the original bytes from the evidence plane through the kernel hands — then delegate to the
    SAME fast-lane path over the extracted text. Never raises for an ordinary refusal."""
    try:
        material = _drive_material(deps, item)
    except _ConversionRefused as ex:
        log.error("item %s: drive conversion refused — %s", item.get("id"), ex.log_detail)
        return failure_result(item, "conversion", str(ex))
    return process_item(conn, item, deps, material=material)


def _drive_material(deps: Deps, item: dict) -> str:
    """The extracted text of a drive capture's original bytes (`blob_refs[1]` — the manifest is
    `blob_refs[0]`, ADR 028 D3's stated-by-design layout; the first multi-blob capture in this
    codebase). Deterministic hands first, vision as the bounded fallback, three honest refusals:
    no bytes blob, an extraction the hands cannot produce, an extraction over the material cap.
    """
    refs = item.get("blob_refs") or []
    if len(refs) < 2:
        raise _ConversionRefused(
            "this drive capture carries no original-bytes blob — it was not enqueued by "
            "stigmergy-drive; re-drop the file with the CLI")
    client = (item.get("hints") or {}).get("client") or {}
    name = str(client.get("drive_name") or "document")
    ext = os.path.splitext(name)[1].lower()
    method = converters.method_for_ext(ext)
    data = deps.evidence.get(refs[1])

    with tempfile.TemporaryDirectory(prefix="stigmergy-drive-conv-") as tmp:
        path = os.path.join(tmp, "doc" + ext)
        with open(path, "wb") as f:
            f.write(data)
        try:
            text = converters.extract(path, method)["text"]
        except Exception as ex:  # noqa: BLE001 — every converter failure becomes one named stage
            raise _ConversionRefused(
                f"the {method} converter could not extract text from {name!r} — the file may be "
                f"corrupt or not what its extension claims; the operator's log has the detail",
                log_detail=f"{ex.__class__.__name__}: {ex}") from ex
        text = _with_vision_fallback(path, method, text, name)

    if not text.strip():
        raise _ConversionRefused(
            f"no text could be extracted from {name!r} — the document appears to carry none")
    n_bytes = len(text.encode("utf-8"))
    if n_bytes > schema.MAX_MATERIAL_BYTES:
        raise _ConversionRefused(
            f"the extracted text of {name!r} is {n_bytes:,} bytes, over the material cap of "
            f"{schema.MAX_MATERIAL_BYTES:,} — the brain files documents, not databases; split "
            f"the document and re-drop the part worth keeping")
    return text


def _with_vision_fallback(path: str, method: str, text: str, name: str) -> str:
    """ONE bounded OCR pass for a PDF whose text layer came back thin (a scanned deck), decided
    by CODE — ADR 028 D4 rejected agent-orchestrated extraction. The env read mirrors
    `converters.vision_extract`'s own call-time read of the same variable: this function only
    asks "is the capability configured at all" to choose between falling back and refusing
    honestly. Keeps whichever extraction is LONGER — vision output degrading below the text
    layer must never lose real text."""
    if method != "pdf":
        return text
    stripped = text.strip()
    pages = text.count("\f") + 1
    if len(stripped) >= DRIVE_VISION_MIN_CHARS_PER_PAGE * pages:
        return text
    if not os.environ.get("GEMINI_API_KEY"):
        if len(stripped) < DRIVE_MIN_TEXT_CHARS:
            raise _ConversionRefused(
                f"{name!r} looks like a scanned PDF (no usable text layer) and this worker has "
                f"no vision OCR configured — the operator can set GEMINI_API_KEY and requeue, "
                f"or drop a text-layer export instead")
        log.warning("drive conversion: %r yields %d chars over %d page(s) — thin, and no "
                    "GEMINI_API_KEY to OCR with; proceeding with the text layer", name,
                    len(stripped), pages)
        return text
    try:
        ocr = converters.vision_extract(path)
    except Exception as ex:  # noqa: BLE001 — vision failing must degrade, not crash the item
        log.warning("drive conversion: vision fallback for %r failed (%s: %s)", name,
                    ex.__class__.__name__, ex)
        if len(stripped) < DRIVE_MIN_TEXT_CHARS:
            raise _ConversionRefused(
                f"{name!r} looks like a scanned PDF and the vision OCR fallback failed — the "
                f"operator's log has the detail; requeue to retry") from ex
        return text
    ocr_text = (ocr.get("text") or "").strip()
    if len(ocr_text) > len(stripped):
        log.info("drive conversion: %r OCR'd by %s (%d chars over the text layer's %d)", name,
                 ocr.get("model", "vision"), len(ocr_text), len(stripped))
        return ocr_text
    return text


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# The meeting flow: a page SET (source + meeting + N decisions), atomically, or nothing.
#
# A SEPARATE entry point (`process_meeting_item`) rather than a branch inside `process_item`,
# because the two flows disagree about the one invariant `process_item`'s own machinery is built
# on: exactly one new page per capture. `_pre_agent` (dedup, the material scan) is the one piece
# genuinely shared — reused, not duplicated. Everything from the agent call onward is code that
# knows it is filing a SET: the agent invocation
# (`deps.agent.run_meeting`, the meeting brief instead of the librarian skill), the gate context
# (a widened, flow-scoped lane — `gates.GateContext.write_prefixes`/`creatable_types`, never the
# global `page.FOLDER_BY_TYPE`, so an ordinary capture claiming `type: meeting` still parks), the
# cross-check (`_cross_check_meeting_outcome`, the
# SET's own atomicity contract — `_cross_check_outcome`'s "exactly one page" rule is UNCHANGED and
# still governs every ordinary capture), and the commit/report (`_file_meeting`,
# `report.filed_meeting`).
# ═══════════════════════════════════════════════════════════════════════════════════════════════

# The meeting flow's write prefixes and creatable types — computed once, module level, because
# they are a property of the FLOW, not of any one item.
#
# **These are THREE folders, not the ordinary fast-lane set plus two.** Writing this as
# `gates.ALLOWED_WRITE_PREFIXES + (the two meeting folders)` widened the gate-side lane to include
# `wiki/notes/` and `wiki/concepts/`, which no meeting contract mentions: the knowledge-repo brief
# (`MEETING_SYSTEM_PROMPT_HEADER`/`SKILL.md`: "writes are confined to NEW .md pages under
# `sources/meetings/`, `wiki/meetings/` and `wiki/decisions/`") and
# `agent.MEETING_ALLOWED_WRITE_RE` both say THREE. The widening made the injected prompt's claim
# FALSE: a steered meeting agent writing `wiki/notes/evil.md` was denied at tool time (the
# agent-side hook was correctly narrow) but would have been ADMITTED by this context had it
# reached the gate any other way, routing a steering attempt through the terminal cross-check as
# `report.failed_system` — a system fault, not `rejected_steering`. Three folders, matching the
# brief exactly, closes both: the hook denies at tool time AND `gate_zone` vetoes with
# `outside-lane` if anything ever reaches it anyway — defence in depth rather than one and only
# defence.
#
# **What these three folders BIND is code, not the agent.** They used to be the AGENT's own lane
# (mirrored by `agent.MEETING_ALLOWED_WRITE_RE`) — what a Write/Edit tool call was permitted to
# touch. The agent now has no page-writing tool at all (its one allowed write is its own outcome
# file — `agent.confine_outcome_write`), and CODE is the sole author of every page in the set
# (`_write_meeting_pages`). So these are the FLOW's own placement contract — where code itself may
# create a page for this capture — and `gate_zone` judges the diff against them as a defence
# against a bug in code's own construction, where it used to be a defence against a steered
# agent.
MEETING_SOURCE_PREFIX = "sources/meetings/"
MEETING_MEETING_PREFIX = "wiki/meetings/"
MEETING_DECISION_PREFIX = "wiki/decisions/"
MEETING_WRITE_PREFIXES = (MEETING_SOURCE_PREFIX, MEETING_MEETING_PREFIX, MEETING_DECISION_PREFIX)
MEETING_CREATABLE_TYPES = frozenset({"source", "meeting", "decision"})
MEETING_EXTRA_FOLDER_TYPES = {"sources/meetings": "source", "wiki/meetings": "meeting"}


def process_meeting_item(conn, item: dict, deps: Deps) -> Result:
    """`process_item`'s sibling for `kind == "meeting"` rows. Never raises for an ordinary
    refusal, same contract as `process_item`."""
    material, early = _pre_agent(conn, item, deps)
    if early is not None:
        return early
    settings = deps.settings

    base = gitcmd.base_ref(deps.repo, settings.branch)
    if settings.require_remote_base and not base.remote:
        raise StaleBaseError(
            f"the base resolved to the local {base.describe()} instead of origin/{settings.branch} "
            f"— the fetch failed, so this deployed worker would judge capture #{item['id']} "
            f"against a commit the remote may have moved past. The capture is left in the queue")
    log.info("filing meeting submission %s against %s", item["id"], base.describe())

    deps = dataclasses.replace(deps,
                               registry=base_inputs.load_registry(deps.repo, base),
                               acl_config=base_inputs.load_acl(deps.repo, base))
    passes = AgentPasses()
    meeting_meta = _meeting_meta(item)
    with base_inputs.linter_at(deps.repo, base) as linter_path, \
            gitcmd.ephemeral_worktree(deps.repo, base.sha, settings.worktree_root) as worktree:
        try:
            return _run_meeting_in_worktree(conn, item, deps, material, meeting_meta, worktree,
                                            passes, linter_path=linter_path)
        except LibrarianError as ex:
            ex.at_agent_attempt(passes.count)
            raise


def _meeting_meta(item: dict) -> dict:
    """The drop CLI's own hints (title, meeting_date, attendees, source_label) — the agent's
    HINTS, never instructions: they never bind a decision's placement or anchor."""
    client = (item.get("hints") or {}).get("client", {})
    return {k: client.get(k, "") for k in ("title", "meeting_date", "attendees", "source_label")}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# A re-file after a park REUSES the prior outcome. **A park must not cost knowledge.**
#
# **The failure this exists for**, measured on a real long transcript:
#
#   1. A pass distilled six decisions and was refused `anchoring/unresolved`. Nothing was wrong
#      with the distillation — one entity simply did not exist in the registry yet.
#   2. A steward minted the entity and requeued.
#   3. The next pass threw that distillation away, re-read the material from scratch, and produced
#      THREE. Two of the lost decisions were ones an attendee confirmed were really taken.
#
# The result was faithful and incomplete: the system discarded a good distillation because of an
# anchoring failure that had nothing to do with its content, on the park->resolve->re-file loop
# built for the normal case. It gets worse with meeting size — the longer the transcript, the more
# a fresh read can drop, and the more likely a park is in the first place (more names, more
# chances one is unregistered).
#
# **The fix, and why it is not merely a cache.** The anchoring resolution is a REGISTRY LOOKUP over
# the prior outcome's entity names, not a judgment that needs the model again — and the outcome is
# already a structured object. So a re-file re-runs the existing pipeline (`_write_meeting_pages`
# + every gate) over the STORED outcome against the FRESH registry: if the steward's mint resolved
# the name, the same decisions file. No new mechanism decides anchoring; the gates do, exactly as
# they always did, over content that no longer changes underneath them.
#
# **The model still runs when it should.** A stored outcome is reused only when the material and
# the submitter's reply are byte-identical to what produced it — a new reply is new information for
# the distillation, and the refused pass above came *after* a `brain_reply`, so that is a real case
# and not a hypothetical. Anything else, and the model re-reads.
#
# **And when a genuine re-distillation does happen, the report DIFFS the two outcomes** — that is
# the only reason the original loss was ever caught, and anyone taking this work must diff rather
# than read the second result, because a fresh distillation looks perfectly plausible on its own.
# ══════════════════════════════════════════════════════════════════════════════════════════════
OUTCOME_REUSE_VERSION = 1


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _outcome_to_raw(outcome) -> dict:
    """The stored shape: plain JSON, built field by field rather than by `dataclasses.asdict`.

    Explicit on purpose. `asdict` would silently carry any field added to `MeetingOutcome` later
    into a column a future pass re-parses, and the round trip has to be something a reader can
    check by eye against `agent.parse_meeting_outcome`'s own field list. Tuples become lists
    because that is what JSON does to them anyway, and what `_list` requires on the way back.
    """
    return {
        "decision": outcome.decision,
        "meeting_title": outcome.meeting_title,
        "attendees": list(outcome.attendees),
        "meeting_notes": outcome.meeting_notes,
        "action_items": [dict(a) for a in outcome.action_items],
        "decisions": [{"title": d.get("title", ""), "body": d.get("body", ""),
                       "anchoring": dict(d.get("anchoring") or {})} for d in outcome.decisions],
        "summary": outcome.summary,
        "findings": [dict(f) for f in outcome.findings],
        "triage": dict(outcome.triage or {}),
    }


def _decision_titles(outcome) -> tuple:
    return tuple(d.get("title", "") for d in (outcome.decisions or ()) if d.get("title"))


def _first_park_titles(item: dict) -> tuple:
    """The decision titles the FIRST park of this row ever carried.

    Read from the stored dict and carried forward untouched on every subsequent park, so the diff
    instrument survives a chain of parks AND a process restart. `_Reuse.prior_titles` is per-run and
    cannot: it is gone the moment the worker moves to the next item.
    """
    stored = item.get("outcome")
    if not isinstance(stored, dict):
        return ()
    first = stored.get("first_park_titles")
    if isinstance(first, list):
        return tuple(t for t in first if t)
    raw = stored.get("raw")
    if isinstance(raw, dict):     # a row written before this key existed
        return tuple(d.get("title", "") for d in (raw.get("decisions") or [])
                     if isinstance(d, dict) and d.get("title"))
    return ()


def _with_park_outcome(result: Result, outcome, *, material: str, item: dict) -> Result:
    """Attach the outcome to a PARKED result so `queue.finish` stores it. The one funnel — every
    meeting result, filed or refused, passes through here, so there is a single answer to "when is
    a distillation kept".

    Kept only when all three hold, and each exclusion is a real case:

    * the row is PARKED (`needs_input`/`triage`) — a terminal row can never re-file, and
      `queue.finish` clears the column on those anyway;
    * the agent decided to FILE — a `triage` outcome carries no distillation to preserve;
    * it carries at least one decision — an empty distillation is not worth a reuse, and reusing
      one would skip the model on a pass that has nothing to lose by running it.

    **A SECOND park must not silently overwrite a richer first one** — the same knowledge loss
    this whole mechanism exists to stop, reproduced one step earlier. `queue.finish`'s `COALESCE`
    REPLACES on a non-None value, so this sequence lost knowledge with nothing reporting it:
    park stores 6 decisions → a steward requeues after minting the wrong name → the reuse is vetoed
    → the fresh model run yields 3 → **the 3 overwrite the 6** → a later requeue reuses those 3 and
    the filing report says *"3 decision(s) preserved"* — true of the last park and false of the
    history, reassuring about exactly the loss it was built to surface.

    Two things close it, and neither is "keep the bigger one": `first_park_titles` carries the
    original set forward so the diff outlives the chain, and a park that drops a decision **says so
    at the pass that caused it** rather than waiting for a filing that may never come. Choosing the
    richer outcome instead would be this function silently overruling the gates about which
    distillation is fileable, which is not its job.
    """
    if result.status not in schema.PARKED_STATUSES or outcome is None:
        return result
    if getattr(outcome, "decision", "") != "file" or not _decision_titles(outcome):
        return result
    titles = _decision_titles(outcome)
    first = _first_park_titles(item) or titles
    stored = {
        "version": OUTCOME_REUSE_VERSION,
        # Both digests are the REUSE PRECONDITION, not provenance decoration: the stored
        # distillation is a function of the material AND the reply it was produced from, so a
        # change to either invalidates it. See `_reusable_outcome`.
        "material_sha256": _sha(material),
        "reply_sha256": _sha(item.get("reply") or ""),
        # Never overwritten once set — the whole point is that it outlives every later park.
        "first_park_titles": list(first),
        "raw": _outcome_to_raw(outcome),
    }
    dropped = [t for t in first if t not in titles]
    if dropped:
        log.warning(
            "meeting item %s: this park's distillation LOST %d decision(s) that the first park "
            "carried (%s) — the stored outcome is being replaced, and the first park's titles are "
            "kept so a later filing can still diff them",
            item.get("id"), len(dropped), ", ".join(dropped))
    else:
        log.info("meeting item %s: keeping the parked distillation (%d decision(s)) for a re-file",
                 item.get("id"), len(titles))
    return dataclasses.replace(result, outcome=stored,
                               report=_with_park_loss(result.report, dropped, titles))


def _with_park_loss(report: dict, dropped: list, kept: tuple) -> dict:
    """The park report's own account of a distillation that shrank, for the human reading it.

    Reported at the pass that CAUSED the loss, not only at a filing that may never happen: a row
    can sit parked indefinitely, and "we lost two decisions three passes ago" is not something to
    learn from a report that only exists if somebody eventually resolves the entity.

    Appended to `summary` rather than added as a sibling key, deliberately: `summary` is the field
    `Result.error` returns and therefore the one sentence that reaches `capture_queue.error`,
    `stigmergy-queue show` and `brain_submissions`. A new key would have been invisible on every
    surface a human actually reads. The structured sibling is there too, for a caller that
    branches rather than reads (`report.base_report`'s own doctrine).
    """
    if not dropped:
        return report
    notice = (
        f"\n\n⚠ This pass re-read the transcript and produced a SMALLER distillation than the "
        f"first park did: {len(dropped)} decision(s) are no longer present "
        f"({', '.join(dropped)}). Nothing is lost from the transcript itself — the evidence is "
        f"archived — but read those before accepting whatever this capture eventually files.")
    return {**report,
            "summary": (report.get("summary", "") + notice),
            "distillation_loss": {"dropped": list(dropped), "kept": list(kept)}}


def _reusable_outcome(item: dict, material: str) -> tuple:
    """`(MeetingOutcome | None, why_not: str)` for the outcome stored on this row, if any.

    **Re-parsed through `agent.parse_meeting_outcome`, never trusted as stored.** The row is a
    mutable surface — an operator can edit it, a migration can touch it, and a future version of
    this code will read rows an older one wrote — so the stored value goes through exactly the
    validator a fresh agent outcome goes through. A shape problem means "no reusable outcome", not
    a refusal: the honest fallback is the model, which is what would have happened anyway.
    """
    stored = item.get("outcome")
    if not isinstance(stored, dict) or not stored:
        return None, ""
    if stored.get("version") != OUTCOME_REUSE_VERSION:
        return None, f"stored under version {stored.get('version')!r}"
    if stored.get("material_sha256") != _sha(material):
        return None, "the archived material is not the one it was distilled from"
    if stored.get("reply_sha256") != _sha(item.get("reply") or ""):
        # The submitter answered (or answered again) since the distillation was made. That answer
        # is INPUT to the distillation — `agent.build_prompt` hands the reply to the model — so
        # reusing an outcome produced without it would silently ignore what the human just said.
        return None, "the submitter's reply changed since it was distilled"
    try:
        outcome = agent_module.parse_meeting_outcome(stored.get("raw"))
    except (OutcomeShapeError, AgentError) as ex:
        return None, f"the stored outcome no longer validates ({ex.__class__.__name__})"
    if outcome.decision != "file" or not _decision_titles(outcome):
        return None, "the stored outcome declares no decisions to file"
    return outcome, ""


@dataclass
class _Reuse:
    """What happened to a stored outcome on this item, for the filing report.

    `prior_titles` is what the parked pass had distilled. `reused` says this pass filed exactly
    that, with no model call. When `prior_titles` is non-empty and `reused` is False, a genuine
    re-distillation happened and the report owes the DIFF — the instrument that caught the loss.
    """
    prior_titles: tuple = ()
    reused: bool = False


def _run_meeting_in_worktree(conn, item, deps, material, meeting_meta, worktree, passes,
                             *, linter_path: str = "") -> Result:
    """`_run_in_worktree`'s meeting sibling: same retry POLICY (one pass, one corrective pass,
    then refuse), a different pass function.

    **One attempt in front of the loop, with no agent in it.** A stored outcome from a
    previous park is re-filed first, through the same `_one_meeting_pass` — same page builders,
    same stamp, same eight gates, same registry read at this item's base commit. If the steward's
    mint resolved the name that parked it, the SAME decisions file and no model runs. If it still
    does not pass, the loop below runs exactly as it always did and the report diffs the outcomes.

    The reuse attempt deliberately does NOT consume `passes.count` (it starts no agent pass, and
    that counter is what the failure report means by "agent attempts") and does not consume the
    corrective-retry budget: it cost no model call, so it may not spend one.
    """
    settings = deps.settings
    corrective, findings, outcome, diagnostics = "", [], None, ""

    prior, why_not = _reusable_outcome(item, material)
    # **The FIRST park's titles, not the stored outcome's.** If an intermediate pass
    # re-distilled and parked again, the stored outcome is already the smaller set — diffing this
    # filing against it would compare a loss to itself and report "nothing dropped". The original
    # is carried forward in `first_park_titles` precisely so the instrument outlives the chain.
    reuse = _Reuse(prior_titles=_first_park_titles(item))
    if prior is None and why_not:
        log.info("meeting item %s: not reusing the stored distillation — %s",
                 item.get("id"), why_not)
    if prior is not None:
        log.info("meeting item %s: re-filing the parked distillation (%d decision(s)) against the "
                 "current registry, with no agent pass", item.get("id"), len(reuse.prior_titles))
        result, findings, outcome = _one_meeting_pass(
            conn, item, deps, material, meeting_meta, worktree, "",
            linter_path=linter_path, reused=prior,
            reuse=dataclasses.replace(reuse, reused=True))
        if result is not None:
            return _with_park_outcome(result, outcome, material=material, item=item)
        # The stored outcome still does not pass the gates. Fall through to a real agent pass —
        # a genuine re-distillation, which the report will diff against `reuse.prior_titles`.
        log.warning("meeting item %s: the parked distillation did not pass the gates (%s); "
                    "re-distilling, and the report will diff what changed", item.get("id"),
                    ", ".join(sorted({f"{f.gate}/{f.code}" for f in gates.vetoes(findings)})))
        _reset_for_retry(worktree)

    for attempt in range(1, MAX_AGENT_ATTEMPTS + 1):
        passes.count = attempt
        try:
            result, findings, outcome = _one_meeting_pass(
                conn, item, deps, material, meeting_meta, worktree, corrective,
                linter_path=linter_path, reuse=reuse)
        except OutcomeShapeError as ex:
            result, findings, outcome = None, list(ex.findings), None
            agent_module.discard_outcome_file(worktree)
        if result is not None:
            return _with_park_outcome(result, outcome, material=material, item=item)

        blocked = gates.unrepairable(findings)
        if attempt < MAX_AGENT_ATTEMPTS:
            if not blocked:
                corrective = gates.corrective_brief(findings)
                _reset_for_retry(worktree)
                continue
            log.warning("meeting item %s: skipping the corrective retry after agent pass %s — "
                       "%s name(s) no repair the agent can perform", item.get("id"), attempt,
                       ", ".join(sorted({f"{f.gate}/{f.code}" for f in blocked})))

        diagnostics = preserve_refused_diff(worktree, item, findings,
                                            root=settings.refused_diff_root)
        break

    return _with_park_outcome(
        _refuse_meeting(item, findings, outcome, agent_attempts=passes.count,
                        diagnostics_path=diagnostics),
        outcome, material=material, item=item)


# ── code writes every page in the set ────────────────────────────────────────────────────────
# The agent's job is to decide the decisions, anchor each, and DRAFT the meeting page's
# notes and each decision page's body — as data. Everything about a PAGE (frontmatter, filename,
# the source page's verbatim body, the meeting page's Attendees/Action Items/Decisions sections) is
# built here, by code, from that data. Nothing here is untrusted-material-shaped except the two
# free-text fields the agent actually drafts (`outcome.meeting_notes`, a decision's own `body`) —
# both still pass through `gates.gate_secrets`/`gate_pii` exactly like an ordinary capture's body
# does, because code writing the CONTAINER does not make the model's own prose trusted.
def _yaml_str(value: str) -> str:
    """One frontmatter scalar, safely quoted (JSON scalars are valid YAML — the same escaper
    `page_policy._yaml_list` already relies on, reused here rather than a second bare
    `f'"{v}"'` that a title containing a `"` would turn into invalid YAML)."""
    return json.dumps(str(value or ""))


def _source_stem(meeting_meta: dict) -> str:
    """The source page's stem, decided by CODE from the operator's own drop-CLI hint — BEFORE the
    agent runs, so the path can be handed to it in the prompt rather than invented by it.
    Content-only, no date prefix (the flow's own convention: only the MEETING page's filename
    carries a date; source and decision stems never do — the gardener's `date-bearing-body-link`
    check flags the convention over the corpus, with no veto)."""
    return f"{slugify(meeting_meta.get('title') or 'meeting')}-transcript"


def _meeting_stem(meeting_date: str, title: str) -> str:
    """The meeting page's own stem — the one filename in this set that DOES carry a date
    (`YYYY-MM-DD-<slug>`), computed after the call from the agent's own `meeting_title` (a hint may
    not be what the material turned out to be about) and the operator's `--date`."""
    base = f"{meeting_date}-{title}" if meeting_date else title
    return slugify(base)


def _decision_stems(titles: list) -> list:
    """One filesystem-safe stem per decision title, collision-safe within this one capture: two
    decisions that slugify to the same stem get `-2`, `-3`, ... suffixes rather than silently
    colliding onto one file (`page_policy.open_for_new`'s `O_EXCL` would otherwise turn a same-slug
    second decision into a crash rather than a routed refusal)."""
    seen: dict = {}
    stems = []
    for title in titles:
        base = slugify(title) or "decision"
        seen[base] = seen.get(base, 0) + 1
        stems.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return stems


# The split-and-cross-link BOUNDARY, up to `_SOURCE_SPLIT_LOOKBACK` lines,
# reimplemented here rather than shared — see the import comment above `MAX_BODY_LINES` for why.
_SOURCE_SPLIT_LOOKBACK = 30


def _chunk_source_body(lines: list, budget: int) -> list:
    """Greedy and budget-bounded, preferring a
    blank-line boundary up to `_SOURCE_SPLIT_LOOKBACK` lines back (never breaking inside a fenced
    code block) so a split does not land mid-paragraph when a nearby blank line is available."""
    chunks, start = [], 0
    while start < len(lines):
        end = min(start + budget, len(lines))
        if end < len(lines):
            for back in range(end, max(start + 1, end - _SOURCE_SPLIT_LOOKBACK), -1):
                candidate = lines[start:back]
                fences = sum(1 for line in candidate if line.lstrip().startswith("```"))
                if not candidate[-1].strip() and fences % 2 == 0:
                    end = back
                    break
        chunks.append(lines[start:end])
        start = end
    return chunks


def _source_part_stem(stem: str, n: int) -> str:
    return stem if n == 1 else f"{stem}-p{n}"


def _build_source_parts(stem: str, title: str, material: str, *, source_kind: str,
                        tags: tuple, url: str = "") -> list:
    """The source page(s), verbatim from the archived material — code writes this,
    never the agent. The transcript already lives in the agent's prompt; having it write the same
    text back out as a page body is pure waste (the largest cost/latency item the first real walk
    found) and a correctness risk besides — a model copying 863 lines can drop, reorder or
    normalise one, and the "ground truth" page would then be a lossy copy of the transcript
    rather than the transcript.

    **THE extracted source-page writer, for every flow that needs one.** `source_kind`/`tags`/`url`
    are parameters — explicit at every call site, with no caller-favouring defaults — so the
    meeting flow, the fast lane's attachment (`_write_attached_sources`) and the Drive door share
    one writer rather than growing a third.

    A body over `MAX_BODY_LINES` is split into cross-linked parts — `Continues in [[...]]` /
    `Continued from [[...]]` — the corpus-wide convention, written for this flow's page shape. The
    FILENAME stem carries the `-p<n>` suffix (a wikilink target must be a filename), and (ADR 028
    D6) every part ALSO carries its explicit chain identity, computed here BY THE PRODUCER and
    stamped by the server: `page_id = <stem>` for part 1, `<stem>#p<n>` after — the `#p`
    sub-identity convention, declared instead of inferred. `index.corpus` prefers the declared
    `id:` over the stem, so the chain collapse keys on a fact; the older `-p<n>` filename
    inference stays only as belt-and-braces for pages filed before this existed.

    **The set's arity is not "exactly one source page"** —
    `_cross_check_meeting_outcome` accepts N >= 1 parts. "Exactly one meeting page" and the
    decision 1:1 link rule are unchanged.

    Returns `[(part_stem, page_id, full_page_text), ...]` — a DRAFT, server-owned fields and
    all: every part still passes through `_stamp_meeting`/`page_policy.stamp_source_fields`
    afterwards exactly as the one-part case always has, so what is written here for
    `content_hash`/`tier`/`status`/`as_of`/`submitted_by` is immediately overwritten and never
    trusted as drafted (a drafted `id` is stripped the same way — `SERVER_OWNED_KEYS` names it).
    A drafted `verification` is STRIPPED rather than overwritten — nothing computes a
    replacement.
    """
    body_lines = (material or "").splitlines()
    chunks = (_chunk_source_body(body_lines, SPLIT_CHUNK_LINES)
             if len(body_lines) > MAX_BODY_LINES else [body_lines])
    total = len(chunks)
    parts = []
    for n, chunk in enumerate(chunks, start=1):
        part_title = title if n == 1 else f"{title} (part {n})"
        body = [f"# {part_title}", ""]
        if n > 1:
            body += [f"Continued from [[{_source_part_stem(stem, n - 1)}]].", ""]
        body += chunk
        if n < total:
            body += ["", f"Continues in [[{_source_part_stem(stem, n + 1)}]]."]
        front = [
            "type: source",
            f"title: {_yaml_str(part_title)}",
            f"source_kind: {source_kind}",
            f"url: {_yaml_str(url)}",
            f"tags: [{', '.join(tags)}]",
            "related: []",
            "sources: []",
        ]
        text = "---\n" + "\n".join(front) + "\n---\n\n" + "\n".join(body).rstrip("\n") + "\n"
        page_id = stem if n == 1 else f"{stem}#p{n}"
        parts.append((_source_part_stem(stem, n), page_id, text))
    return parts


def _build_decision_page(title: str, body: str, source_stem: str, created: str) -> str:
    """A decision page's DRAFT — frontmatter code owns, body the agent drafted (Context/Options/
    Decision/Why/Consequences, per `ops/templates/decision.md`). `sources:` names the transcript
    page directly, so the meeting's own evidence is one hop away without a body-prose wikilink to
    a page whose stem might carry the meeting's date — a convention
    `gardener.checks.check_date_bearing_body_links` reports on, and no longer a veto here.

    `created`/`updated` (the contract linter's `REQUIRED_FIELDS`, knowledge repo's own
    `stigmergy_lint.py`) are NOT server-owned (`page_policy.SERVER_OWNED_KEYS` does not name them,
    unlike `status`/`as_of`) — they are ordinary drafted fields, so code drafts them itself here,
    from the same date `_stamp_meeting` stamps as `as_of` (the meeting's own date, or today's if
    the operator's `--date` hint is somehow absent). `sources/meetings/` pages are exempt from
    this requirement (`MACHINE_REQUIRED_FIELDS`), which is why `_build_source_parts` does not draft
    either field at all.
    """
    front = [
        "type: decision",
        f"title: {_yaml_str(title)}",
        'owner: ""',
        f"created: {created}",
        f"updated: {created}",
        "tags: [decision]",
        "related: []",
        f'sources: ["[[{source_stem}]]"]',
    ]
    body_text = (body or "").strip() or "## Context\n\n(not provided)"
    text = "---\n" + "\n".join(front) + "\n---\n\n" + f"# {title}\n\n{body_text}\n"
    return text


def _build_meeting_page(outcome, title: str, source_stem: str, decision_stems: list,
                        created: str) -> str:
    """The meeting (provenance) page, built entirely by CODE from the agent's structured account —
    Attendees, Action Items and the "## Decisions" section are STRUCTURE code owns; only "## Notes"
    is the agent's own drafting. This is what makes a links/decisions mismatch structurally
    impossible rather than merely checked: code cannot declare
    a decision it did not also link, because the link list IS the decision list it just wrote.

    `created`/`updated`: see `_build_decision_page`'s own comment — the same contract requirement,
    the same source date.
    """
    front = [
        "type: meeting",
        f"title: {_yaml_str(title)}",
        f"created: {created}",
        f"updated: {created}",
        "tags: [meeting]",
        "related: []",
        f'sources: ["[[{source_stem}]]"]',
    ]
    lines = [f"# {title}", "", "## Attendees", ""]
    lines += [f"- {a}" for a in outcome.attendees] or ["- (none recorded)"]
    lines += ["", "## Action Items", ""]
    items = list(outcome.action_items)
    if items:
        for entry in items:
            box = "[x]" if entry.get("done") else "[ ]"
            owner, action = entry.get("owner") or "", entry.get("action") or ""
            row = " — ".join(part for part in (owner, action) if part)
            lines.append(f"- {box} {row}".rstrip())
    else:
        lines.append("- (none)")
    lines += ["", "## Decisions", ""]
    lines += [f"- [[{stem}]]" for stem in decision_stems] or ["- (none from this meeting)"]
    lines += ["", "## Notes", "", (outcome.meeting_notes or "").strip() or "(no additional notes)"]
    return "---\n" + "\n".join(front) + "\n---\n\n" + "\n".join(lines) + "\n"


def _write_new(worktree: str, rel_path: str, text: str) -> None:
    full = os.path.join(worktree, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with page_policy.open_for_new(full) as f:
        f.write(text)


def _write_meeting_pages(worktree: str, outcome, meeting_meta: dict, material: str, *,
                         source_stem: str, created: str):
    """CODE writes every page in the set. All-or-nothing: paths are computed
    first, checked against the repo's existing pages, and only written once none collide — the
    same atomicity the set has always had, now enforced before the first byte is written rather
    than discovered mid-write.

    Returns a plan dict — `{"source_stems", "meeting_stem", "decision_stems",
    "decisions_by_path"}` — on success, or a `list[gates.Finding]` (one veto) when a computed path
    already exists, so the caller can hand it back to the SAME corrective-retry road every other
    finding takes.
    """
    meeting_date = meeting_meta.get("meeting_date") or ""
    meeting_title = outcome.meeting_title or meeting_meta.get("title") or "Meeting"
    meeting_stem = _meeting_stem(meeting_date, meeting_title)
    meeting_path = f"{MEETING_MEETING_PREFIX}{meeting_stem}.md"

    decision_titles = [d.get("title", "") for d in outcome.decisions]
    decision_stems = _decision_stems(decision_titles)
    decision_paths = [f"{MEETING_DECISION_PREFIX}{stem}.md" for stem in decision_stems]

    source_parts = _build_source_parts(source_stem, f"{meeting_title} — transcript", material,
                                       source_kind="meeting",
                                       tags=("source", "meeting-transcript"), url="")
    source_paths = [f"{MEETING_SOURCE_PREFIX}{stem}.md" for stem, _pid, _text in source_parts]

    existing = page_policy.path_keys(gitcmd.tracked_paths(worktree))
    all_paths = [*source_paths, meeting_path, *decision_paths]
    collisions = sorted({p for p in all_paths if page_policy.path_key(p) in existing})
    if collisions:
        return [gates.Finding(
            "outcome", "existing-page-collision",
            f"the page(s) this meeting would create already exist in the repo: "
            f"{', '.join(collisions)} — the meeting or a decision needs a different title",
            locator=", ".join(collisions))]

    for path, (_stem, _pid, text) in zip(source_paths, source_parts, strict=True):
        _write_new(worktree, path, text)
    for path, declared in zip(decision_paths, outcome.decisions, strict=True):
        _write_new(worktree, path,
                  _build_decision_page(declared.get("title", ""), declared.get("body", ""),
                                       source_stem, created))
    _write_new(worktree, meeting_path,
              _build_meeting_page(outcome, meeting_title, source_stem, decision_stems, created))

    return {
        "source_stems": [stem for stem, _pid, _text in source_parts],
        # The producer's explicit chain identity per part path — `_stamp_meeting`
        # stamps it as `id:` and records it for the gate's output-equality check.
        "source_ids_by_path": {path: pid for path, (_stem, pid, _text)
                               in zip(source_paths, source_parts, strict=True)},
        "meeting_stem": meeting_stem,
        "decision_stems": decision_stems,
        "decisions_by_path": dict(zip(decision_paths, outcome.decisions, strict=True)),
    }


def _one_meeting_pass(conn, item, deps, material, meeting_meta, worktree, corrective, *,
                      linter_path: str = "", reused=None, reuse=None) -> tuple:
    """One meeting pass: call the (now structured, tool-less) meeting agent, have CODE write every
    page of the set, stamp, and run every gate over the whole diff. Returns `(result, findings,
    outcome)` — same contract as `_one_pass`.

    **The agent's only write, ever, is its own outcome file.** It reads the transcript (already in
    its prompt), the resolved entity registry and the meeting metadata, decides the decisions and
    their anchors, and drafts the meeting page's notes and each decision page's body — as DATA,
    returned in `.librarian-outcome.json`, never as files it creates itself. Code builds every page
    (source verbatim, meeting and decision pages from the structured content) and writes it. This
    is what collapses the older exploratory Write/Edit/Read/Glob/Grep
    loop into one structured call plus its corrective retry: there is nothing left in this repo
    for the agent to explore, because everything it needs was handed to it up front.
    """
    settings = deps.settings
    source_stem = _source_stem(meeting_meta)
    source_page_path = f"{MEETING_SOURCE_PREFIX}{source_stem}.md"
    if reused is not None:
        # The stored distillation from a previous park, re-filed with NO agent call.
        # Everything below this line is the ordinary path, unchanged and unaware — the page
        # builders, the stamp and all eight gates run over the reused content exactly as they run
        # over a fresh outcome, and `deps.registry` was loaded at THIS item's base commit, which is
        # the whole mechanism: the steward's newly minted entity is what changed, not the content.
        outcome = reused
    else:
        run = deps.agent.run_meeting(worktree=worktree, material=material,
                                     meeting_meta=meeting_meta, registry=deps.registry,
                                     source_page_path=source_page_path, corrective=corrective,
                                     reply=item.get("reply") or "")
        outcome = run.outcome
        agent_module.discard_outcome_file(worktree)
        if outcome is None:
            raise AgentError("the meeting agent produced no usable account of what it did")

    if outcome.decision == "triage":
        # The agent has no tool that can write a page at all (its allow-list is `Write`, confined
        # to the outcome file itself — `agent.confine_outcome_write`) — so a triage outcome cannot
        # leave a stray
        # page behind the way the ordinary flow's cooperative-agent check still guards against
        # (`_one_pass`'s `stray` check). Nothing to check here that is not already structural.
        return _triage_meeting(item, deps, outcome), [], outcome

    written = _write_meeting_pages(worktree, outcome, meeting_meta, material,
                                   source_stem=source_stem,
                                   created=meeting_meta.get("meeting_date") or deps.as_of())
    if isinstance(written, list):
        # `_write_meeting_pages` returns findings instead of a plan when a computed path collides
        # with a page that already exists in the repo — nothing was written, so this is exactly
        # like any other veto: the corrective retry (or the final refusal) reads it.
        return None, written, outcome

    ctx = gates.GateContext(
        worktree=worktree,
        entries=gitcmd.diff_entries(worktree),
        added=gitcmd.added_lines(worktree),
        material=material, outcome=outcome, registry=deps.registry,
        linter_path=linter_path, gitleaks_bin=settings.gitleaks_bin,
        write_prefixes=MEETING_WRITE_PREFIXES, creatable_types=MEETING_CREATABLE_TYPES,
        extra_folder_types=MEETING_EXTRA_FOLDER_TYPES, edits_allowed=False)

    if not ctx.entries:
        raise AgentError("the meeting flow wrote nothing and did not park the capture")

    # The meeting flow files only NEW pages — no additive edits to pages that already exist (the
    # page-set contract names no such mechanism for this flow, and the meeting page's own
    # Decisions section is what links the set together). `edits.apply_declared` is therefore not
    # invoked here — but that is
    # no longer the ONLY thing standing in the way. `edits_allowed=False` above is what makes
    # `gates.gate_zone` refuse a status-M entry outright (`meeting-edit-refused`) if one ever
    # reached this diff by any other route than `edits.apply_declared`, so the no-edit-mechanism
    # contract is enforced rather than merely true because this call is absent. See
    # `gates.GateContext.edits_allowed` and `gate_zone`'s own comment for why that finding is
    # terminal rather than a corrective brief.

    _stamp_meeting(ctx, deps, item, outcome, meeting_meta, written)

    ctx.entries = gitcmd.diff_entries(worktree)
    ctx.added = gitcmd.added_lines(worktree)
    findings = gates.run_gates(ctx) + _cross_check_meeting_outcome(ctx, outcome)
    if not gates.vetoes(findings):
        return (_file_meeting(conn, item, deps, ctx, outcome, findings, worktree,
                              written, reuse=reuse),
                [], outcome)
    return None, findings, outcome


def _decision_pages(ctx: gates.GateContext) -> list[str]:
    return sorted(p for p in ctx.in_lane_new_pages() if p.startswith("wiki/decisions/"))


def _source_pages(ctx: gates.GateContext) -> list[str]:
    return sorted(p for p in ctx.in_lane_new_pages() if p.startswith("sources/meetings/"))


def _meeting_pages(ctx: gates.GateContext) -> list[str]:
    return sorted(p for p in ctx.in_lane_new_pages() if p.startswith("wiki/meetings/"))


def _cross_check_meeting_outcome(ctx: gates.GateContext, outcome) -> list:
    """The page-SET's own atomicity contract: N >= 1 source
    pages, exactly one meeting page, N >= 0 decision pages, no page outside that set, and the
    date-in-wikilink convention.

    `_cross_check_outcome` (the ordinary flow's "exactly one page" veto) does not run here — a
    page SET is exactly the case that rule cannot express.

    **The source-page arity is N >= 1, not "exactly one".** CODE is the sole author of the source
    page, verbatim from the material, and splits it into N >= 1 cross-linked parts when it is over
    the contract's line cap (`_build_source_parts`) — so `source-page-count` vetoes only `< 1`
    (which cannot happen by construction; kept as a self-check, not a live path). "Exactly one
    meeting page" and the decision 1:1 link rule are unconditional.

    **Most of the checks an ADVERSARIAL author would need are gone, because that author is gone.**
    The agent used to write every page via its own Write/Edit tool calls and separately DECLARE
    what it wrote in its outcome JSON — two independent claims that could disagree (a declared
    decision the diff never created, a meeting page whose own "## Decisions" section linked
    something else, a claimed `source_page_path` that did not match the file on disk). CODE is now
    the sole author of every page in the set, from the SAME structured `outcome` this function
    reads — `_write_meeting_pages` cannot declare a
    decision it did not also write, or link one from the meeting page it did not also file, because
    the link list and the written-page list share one source. `duplicate-decision-declared`,
    `decision-set-mismatch`, `source-path-mismatch`, `meeting-path-mismatch` and
    `meeting-links-mismatch` are therefore absent: the disagreement they checked for is not
    reachable. Code writes the meeting page's "## Decisions" from the SAME `decision_stems` list it
    names the decision pages with (`_build_meeting_page`), so the two cannot disagree without
    `decision-count-mismatch` above catching the construction bug first.

    **The date-bearing body-link convention is not vetoed here either** — that a date-bearing page
    name belongs in `sources:`/`related:` frontmatter rather than body prose is style, not safety.
    The gardener's `date-bearing-body-link` check flags it as a finding over the committed
    corpus.
    """
    out = []
    new_pages = set(ctx.in_lane_new_pages())
    source_pages, meeting_pages, decision_pages = (set(_source_pages(ctx)), set(_meeting_pages(ctx)),
                                                    set(_decision_pages(ctx)))
    other = new_pages - source_pages - meeting_pages - decision_pages
    if other:
        out.append(gates.Finding(
            "outcome", "unexpected-page",
            f"created page(s) outside the meeting flow's set (one or more source-page parts, one "
            f"meeting page, any number of decision pages): {', '.join(sorted(other))}",
            locator=", ".join(sorted(other))))
    if len(source_pages) < 1:
        out.append(gates.Finding(
            "outcome", "source-page-count",
            "created no page under sources/meetings/; a meeting capture files at least one "
            "source-page part",
            locator="(none)"))
    if len(meeting_pages) != 1:
        out.append(gates.Finding(
            "outcome", "meeting-page-count",
            f"created {len(meeting_pages)} page(s) under wiki/meetings/; a meeting capture "
            f"files exactly one meeting page",
            locator=", ".join(sorted(meeting_pages)) or "(none)"))
    if len(decision_pages) != len(outcome.decisions):
        out.append(gates.Finding(
            "outcome", "decision-count-mismatch",
            f"the outcome describes {len(outcome.decisions)} decision(s) but "
            f"{len(decision_pages)} decision page(s) were written — code's own construction "
            f"disagreed with itself",
            locator=", ".join(sorted(decision_pages)) or "(none)"))

    return out


def _stamp_meeting(ctx: gates.GateContext, deps: Deps, item: dict, outcome,
                   meeting_meta: dict, written: dict) -> None:
    """Stamp every page this pass created — PER PAGE, because a decision page's `entity:` differs
    from its siblings'. Populates `ctx.page_declared`, `ctx.stamped_by_path` and
    `ctx.provenance_pages`, the three per-page facts `gate_zone`/`gate_anchoring`/
    `gate_frontmatter` read instead of the ordinary single-outcome fields (see each field's own
    comment on `GateContext`).

    `as_of` is the meeting's OWN date (`--date`), never today's date — the one
    place this flow's stamp differs from the ordinary fast lane's, which always uses "today".

    `written["decisions_by_path"]` replaces an older `{d["page_path"]: d}` lookup built
    from the outcome directly: decision paths are now code-computed (`_write_meeting_pages`), not
    agent-declared, so the map from a written page's path back to its anchoring comes from what
    code itself just wrote, not from a field the outcome no longer carries.
    """
    as_of = meeting_meta.get("meeting_date") or deps.as_of()
    source_pages, meeting_pages, decision_pages = (_source_pages(ctx), _meeting_pages(ctx),
                                                    _decision_pages(ctx))
    ctx.provenance_pages = frozenset(source_pages)
    decisions_by_path = written.get("decisions_by_path", {})

    source_ids_by_path = written.get("source_ids_by_path", {})
    for path in source_pages:
        page_id = str(source_ids_by_path.get(path) or "")
        ctx.page_declared[path] = {"page_type": "source"}
        digest = hashlib.sha256((ctx.material or "").encode("utf-8")).hexdigest()
        extracted_at = datetime.datetime.now(datetime.UTC).isoformat()
        _rewrite(ctx.worktree, path, lambda text, d=digest, e=extracted_at, pid=page_id:
                 page_policy.stamp_source_fields(text, submitted_by=item["submitted_by"],
                                                 as_of=as_of, content_hash=d, extracted_at=e,
                                                 page_id=pid))
        # Findings cycle 1, C1: the provenance group used to be ABSENT from `stamped_by_path`, so
        # `gate_frontmatter`'s output-equality post-condition — the check every OTHER stamped field
        # goes through, and the principle the gate's own docstring states ("a gate that checks the
        # OUTPUT cannot be defeated by a new way of spelling the input") — never ran over the one
        # field group whose forgery re-anchors the entire provenance chain. `content_hash` and
        # `extracted_at` are rendered exactly as `page.stamp_source_fields` writes them
        # (`f'"sha256:{digest}"'`/`f'"{extracted_at}"'` parse to the bare string `_as_text` expects
        # once YAML strips the quotes); `tier` is always `"1"` here — the meeting flow's only
        # source, a Granola transcript, is always a primary recording (`stamp_source_fields`'s own
        # default).
        ctx.stamped_by_path[path] = {
            "status": page_policy.FILED_STATUS, "as_of": as_of,
            "submitted_by": item["submitted_by"],
            "content_hash": f"sha256:{digest}", "extracted_at": extracted_at, "tier": "1",
            **({"id": page_id} if page_id else {})}

    for path in meeting_pages:
        ctx.page_declared[path] = {"page_type": "meeting"}   # no "anchoring" key: provenance only
        _rewrite(ctx.worktree, path, lambda text: page_policy.stamp_server_fields(
            text, submitted_by=item["submitted_by"], acl=None, as_of=as_of, entity=()))
        ctx.stamped_by_path[path] = {"status": page_policy.FILED_STATUS, "as_of": as_of,
                                     "submitted_by": item["submitted_by"], "entity": ()}

    for path in decision_pages:
        declared = decisions_by_path.get(path) or {}
        anchoring = declared.get("anchoring") or {}
        ctx.page_declared[path] = {"page_type": "decision", "anchoring": anchoring}
        entity_ids, unresolved = gates.resolve_entity_ids(anchoring, deps.registry)
        if str(anchoring.get("kind", "")).lower() == "entity" and (unresolved or not entity_ids):
            entity_ids = []   # same defence-in-depth `processing._stamp` takes for the ordinary flow
        acl = acl_rules.resolve(deps.acl_config, path)
        _rewrite(ctx.worktree, path, lambda text, e=entity_ids, a=acl: page_policy.stamp_server_fields(
            text, submitted_by=item["submitted_by"], acl=a, as_of=as_of, entity=e))
        ctx.stamped_by_path[path] = {"status": page_policy.FILED_STATUS, "as_of": as_of,
                                     "submitted_by": item["submitted_by"],
                                     "entity": entity_ids, "acl": acl}


def _rewrite(worktree: str, path: str, transform) -> None:
    full = os.path.join(worktree, path)
    try:
        with open(full, encoding="utf-8") as f:
            drafted = f.read()
    except (OSError, UnicodeDecodeError):
        return
    with page_policy.open_for_rewrite(full) as f:
        f.write(transform(drafted))


def _meeting_commit_message(item: dict, outcome, n_decisions: int) -> str:
    """One capture, one commit, one page SET. `Submitted-by:` names the operator who
    dropped the transcript, exactly like the ordinary flow's trailer."""
    return (f"feat(meeting): {_subject(outcome.meeting_title)}\n\n"
            f"Filed by the librarian's meeting distiller from capture #{item['id']}: 1 source "
            f"page, 1 meeting page, {n_decisions} decision page(s).\n\n"
            f"Submitted-by: {item['submitted_by']}\n")


def _file_meeting(conn, item, deps, ctx, outcome, findings, worktree, written,
                  *, reuse=None) -> Result:
    """The gates passed over the whole SET: commit every page in one App-bot commit, push, and
    report the set. `written` carries the code-computed source parts and decision-path-to-anchoring
    map — the outcome declares no page paths at all.

    `reuse` is what happened to a stored distillation on this item, and it exists so the report can
    say it. Two cases, and the second is the load-bearing one: a REUSE says the parked pass's
    decisions filed unchanged, and a RE-DISTILLATION owes the diff between what was parked and what
    is being filed now — because a fresh distillation looks perfectly plausible on its own, and
    diffing is the only way the loss is ever noticed at all."""
    from stigmergy.librarian import githubapp

    meeting_pages, decision_pages = _meeting_pages(ctx), sorted(_decision_pages(ctx))
    # In PART order (1, 2, 3, ...), not alphabetical — `-p2` sorts before the bare stem's `.md`,
    # so a plain `sorted()` over `ctx.in_lane_new_pages()` would list part 2 before part 1.
    # `written["source_stems"]` already carries the real order `_build_source_parts` produced it in.
    source_pages = [f"{MEETING_SOURCE_PREFIX}{stem}.md" for stem in written["source_stems"]]
    meeting_page = meeting_pages[0]
    message = _meeting_commit_message(item, outcome, len(decision_pages))
    author_name, author_email = githubapp.identity()
    # Same TOCTOU close as `_file`'s — and this lane is where it bites hardest,
    # because the page SET makes the window longer: more pages, more gate work, more time on disk.
    gitcmd.commit(worktree, message=message, author_name=author_name, author_email=author_email,
                  gated_entries=ctx.entries)

    remote_url, config_env = "", {}
    if githubapp.configured():
        slug = _repo_slug(deps.repo)
        remote_url = githubapp.push_url(slug)
        config_env = githubapp.push_config(githubapp.installation_token(), slug)

    if not queue.holds_lease(conn, item["id"], expected_attempts=item["attempts"]):
        raise LeaseLostError(
            f"the lease on submission {item['id']} (delivery {item['attempts']}) was lost while "
            f"this meeting was being processed; nothing was pushed")
    sha = gitcmd.push(worktree, branch=deps.settings.branch, remote_url=remote_url,
                      config_env=config_env, author_name=author_name, author_email=author_email)

    decisions_by_path = written.get("decisions_by_path", {})
    decisions = [{"path": path, "anchoring": (decisions_by_path.get(path) or {}).get("anchoring", {})}
                for path in decision_pages]
    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]
    notes += [f.message for f in findings if f.severity == gates.SEVERITY_NOTE]

    # Regenerate the touched entities' views, in the SAME run, right
    # after the meeting's own push — the `worktree` already sits at the sha that just landed, so
    # no second checkout is needed. Touched ids come from `ctx.stamped_by_path`, the SAME
    # server-resolved values `_stamp_meeting` wrote into each decision page's `entity:` field
    # (never `outcome.decisions[i]["anchoring"]["entities"]`, which is the agent's DECLARED
    # names, not the resolved ids — using it here would let an agent's own account decide which
    # views regenerate).
    #
    # Deliberately best-effort: the meeting page set is already committed and pushed by this
    # point — an irreversible, successful outcome — so a view-regeneration fault must never
    # turn a filed meeting into a `failed` capture. Flagged here rather than assumed.
    #
    # **CONTRACT NOTE, stated here for a future reader**: `branch` on `deps.settings.branch` does
    # NOT reliably tip at the meeting's own commit after `_file_meeting` returns — a successful
    # run of this block
    # pushes a SECOND commit (the view's) on top of the meeting's. `sha` above (captured before
    # this block runs) and `result_ref` below (`f"{meeting_page}@{sha}"`) still name the meeting's
    # OWN commit and remain the correct, stable handle for "what this capture filed" — but code
    # anywhere that reads "the branch tip" to learn what a capture just filed (rather than reading
    # `result_ref`/`sha` directly) is now wrong. See `views/index.md`'s own note.
    touched_ids = sorted({eid for path in decision_pages
                          for eid in (ctx.stamped_by_path.get(path, {}).get("entity") or [])})
    if touched_ids:
        try:
            # `views_regenerate.run` writes its own `job_runs` row (ok or error) via
            # `capture.ops.job_run`, which re-raises after recording — so the row already exists
            # by the time this `except` runs; nothing more to record here.
            asyncio.run(views_regenerate.run(
                worktree, conn, touched_ids, registry=deps.registry, branch=deps.settings.branch,
                guarded=False,
                job=f"{views_regenerate.JOB_NAME}-on-meeting"))
        except Exception:  # noqa: BLE001 — a best-effort post-step, see the comment above
            log.error("view regeneration failed after meeting %s filed (entities: %s)",
                      item.get("id"), touched_ids, exc_info=True)
    # `result_ref` names the MEETING PAGE (see `report.filed_meeting`'s own docstring): the
    # human's one door into the set, and what keeps
    # `dedup.Match.page_path`'s existing `rsplit("@")` contract working unchanged. The full page
    # list lives in the report (`report["filed_meeting"]`), not in `result_ref`.
    return Result(
        schema.FILED, f"{meeting_page}@{sha}",
        report.filed_meeting(source_pages=source_pages, meeting_page=meeting_page,
                             decisions=decisions, commit=sha,
                             agent_rationale=getattr(outcome, "summary", ""),
                             registry=deps.registry,
                             reuse=_reuse_note(reuse, outcome)),
        findings=notes)


def _reuse_note(reuse, outcome) -> dict:
    """The report's account of what happened to a parked distillation on this item.

    `{}` when no stored outcome was involved at all, which is every first pass — so the ordinary
    report carries no reuse block at all and no reader has to learn a new field for the
    common case.

    **The comparison is against the FIRST park, and it runs on BOTH branches.** A
    reuse that re-files a distillation an intermediate pass had already shrunk is not "preserved" —
    it preserves the *last* park and hides the loss before it. So `reused` is reported only when
    what actually filed still matches what the first park carried; otherwise the diff is reported
    even though no model ran on this pass, because the reader's question is "did this capture lose
    anything", not "did this pass call a model".

    `dropped`/`added` are exact-title comparisons: a decision whose title was merely REWORDED
    between passes reads as one dropped plus one added. That direction is deliberate — it
    over-reports rather than under-reports, and under-reporting is the failure this exists to
    prevent.
    """
    if reuse is None or not reuse.prior_titles:
        return {}
    now = _decision_titles(outcome)
    dropped = [t for t in reuse.prior_titles if t not in now]
    added = [t for t in now if t not in reuse.prior_titles]
    if reuse.reused and not dropped and not added:
        return {"reused": True, "decisions": list(now)}
    return {"reused": False, "model_ran": not reuse.reused,
            "dropped": dropped, "added": added,
            "kept": [t for t in now if t in reuse.prior_titles]}


def _triage_meeting(item: dict, deps: Deps, outcome) -> Result:
    """The meeting agent's own park (`decision: "triage"`) — one or several unresolved names, per
    `_ask_or_park_multi`'s routing rule (the same one-ask budget `_ask_or_park` enforces for the
    ordinary flow)."""
    parked = outcome.triage or {}
    rationale = getattr(outcome, "summary", "")
    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]
    names = [n for n in (parked.get("names") or []) if n] or [schema.UNNAMED_ENTITY_PLACEHOLDER]
    return _ask_or_park_multi(item, deps, names=names, agent_rationale=rationale, notes=notes)


def _ask_or_park_multi(item: dict, deps: Deps, *, names: list, agent_rationale: str,
                       notes: list) -> Result:
    """`_ask_or_park`'s plural sibling: the SAME one-ask budget (`asked_at`), naming every
    unresolved name at once. A single unresolved name still goes through the SINGULAR builders
    (`needs_input`/`triage_entity`) — byte-identical to the ordinary flow's own ask-back for the
    one-name case."""
    if item.get("asked_at"):
        rep = (report.triage_entity_multi(names=names, agent_rationale=agent_rationale,
                                          findings=notes, asked=True) if len(names) > 1 else
              report.triage_entity(name=names[0], agent_rationale=agent_rationale,
                                   findings=notes, asked=True))
        return Result(schema.TRIAGE, "", rep, findings=notes)
    candidates = gates.registry_candidates(deps.registry)
    shown = candidates if len(candidates) <= report.MAX_QUESTION_CANDIDATES else []
    if len(names) > 1:
        rep = report.needs_input_multi(submission_id=item["id"], names=names, candidates=shown,
                                       total_candidates=len(candidates),
                                       agent_rationale=agent_rationale, findings=notes)
    else:
        rep = report.needs_input(submission_id=item["id"], name=names[0], candidates=shown,
                                 total_candidates=len(candidates),
                                 agent_rationale=agent_rationale, findings=notes)
    return Result(schema.NEEDS_INPUT, "", rep, findings=notes)


def _refuse_meeting(item, findings, outcome, *, agent_attempts: int = 0,
                    diagnostics_path: str = "") -> Result:
    """Both meeting-agent passes vetoed. Reuses the ordinary flow's cause-based routing helpers
    (`_uncreatable_type`, `_frontmatter_only` — pure functions of the veto LIST, unaware of which
    flow produced it) wherever they still apply unmodified; the anchoring park is meeting-specific
    because a page SET can carry several unresolved anchors at once, one per decision page."""
    veto = gates.vetoes(findings)
    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]

    secret = next((f for f in veto if f.code == "secret"), None)
    if secret:
        return Result(schema.REJECTED, "",
                      report.rejected_secret(line=secret.locator.rsplit(":", 1)[-1],
                                             rule_id=secret.message.rsplit("rule: ", 1)[-1]
                                             .rstrip(")"),
                                             where="the drafted page"),
                      diagnostics_path=diagnostics_path)
    pii = next((f for f in veto if f.code == "pii"), None)
    if pii:
        label = pii.message.split("what looks like ", 1)[-1].split(" near line")[0]
        return Result(schema.REJECTED, "",
                      report.rejected_pii(line=pii.locator.rsplit(":", 1)[-1],
                                          pattern_label=label, where="the drafted page"),
                      diagnostics_path=diagnostics_path)

    # `f.repairable` excludes an UNREPAIRABLE zone finding from this branch, even
    # when the agent declared an injection category alongside it. `meeting-edit-refused`'s own
    # documented meaning is "no producer inside this flow — a worker defect or worktree
    # interference", i.e. a SYSTEM fault; routing it here on the mere coincidence of a declared
    # category would name the submitter's (possibly unrelated) capture as the cause of a fault
    # that has nothing to do with it, and would bury the real signal — an unexplained write into
    # an existing page inside an ephemeral worktree — under a steering report the operator then
    # investigates for the wrong reason. This also corrects `zone/body-rewrite` and
    # `zone/unreadable-edit`, which share the same class (`repairable=False`, no producer this
    # flow's agent could have been). A repairable zone finding — `outside-lane`,
    # `type-not-creatable` — still routes here, because there the diff really could be the agent
    # acting on injected text.
    zone = next((f for f in veto if f.gate == "zone" and f.repairable), None)
    categories = _injection_categories(outcome)
    if zone and categories:
        return Result(schema.REJECTED, "",
                      report.rejected_steering(path=zone.locator, category=categories[0],
                                               findings=notes),
                      diagnostics_path=diagnostics_path)

    if _frontmatter_only(veto):
        codes = {f.code for f in veto}
        builder = (report.rejected_malformed_frontmatter if codes == {"unparseable"}
                  else report.rejected_forged_field)
        return Result(schema.REJECTED, "", builder(findings=notes),
                      diagnostics_path=diagnostics_path)

    uncreatable = _uncreatable_type(veto)
    if uncreatable:
        return Result(schema.TRIAGE, "",
                      report.triage_type(judged_type=uncreatable,
                                         agent_rationale=getattr(outcome, "summary", ""),
                                         findings=notes),
                      findings=notes, diagnostics_path=diagnostics_path)

    # The meeting-specific park: is EVERY veto an anchoring-unresolved finding (one per decision
    # page that could not anchor)? Then this is the honest destination: the whole capture parked,
    # atomically — zero pages committed. Anything ELSE mixed in
    # (a binary-page veto, an unrelated dead link, a zone veto with no traceable steering) means
    # this is not provably the whole story, and it falls through to `failed`, exactly like
    # `_unanchorable`'s own posture for the ordinary flow.
    anchoring_vetoes = [f for f in veto
                       if f.gate == "anchoring" and f.code == gates.ANCHORING_UNRESOLVED]
    if anchoring_vetoes and len(anchoring_vetoes) == len(veto):
        names = []
        for finding in anchoring_vetoes:
            for value in (finding.values or (finding.locator,)):
                if value and value not in names:
                    names.append(value)
        names = names or [schema.UNNAMED_ENTITY_PLACEHOLDER]
        rationale = getattr(outcome, "summary", "")
        asked = bool(item.get("asked_at"))
        rep = (report.triage_entity_multi(names=names, agent_rationale=rationale,
                                          findings=notes, asked=asked) if len(names) > 1 else
              report.triage_entity(name=names[0], agent_rationale=rationale, findings=notes,
                                   asked=asked))
        return Result(schema.TRIAGE, "", rep, findings=notes, diagnostics_path=diagnostics_path)

    worst = veto[0] if veto else None
    return Result(schema.FAILED, "",
                  report.failed_system(attempts=item.get("attempts", 1),
                                       agent_attempts=agent_attempts,
                                       stage=worst.gate if worst else "gates",
                                       reason=worst.message if worst else "unknown"),
                  findings=notes, diagnostics_path=diagnostics_path)
