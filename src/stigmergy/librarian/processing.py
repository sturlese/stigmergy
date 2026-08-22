"""Processing ONE capture: the filing path, from queue row to commit — or to a refusal.

Cheapest-first: material -> retry collapse -> already-filed -> secrets/PII over the MATERIAL ->
worktree -> agent -> declared edits -> stamp -> gates -> [one corrective retry] -> commit -> push.
The scan runs over the MATERIAL because a capture containing a secret bounces WHOLE. Terminal
states split by CAUSE, not by gate: content `rejected`, system `failed`. A name nothing resolves
to does not stop the page: the agent declares the entity in its account, and
`identity.write_births` writes it in the same commit, confirmed by whoever captured (ADR 044).
"""
import asyncio
import dataclasses
import datetime
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field

from stigmergy.capture import queue, schema
from stigmergy.capture.errors import CaptureError

# `MAX_BODY_LINES`/`SPLIT_CHUNK_LINES` are IMPORTED: the linter and the splitter must agree.
from stigmergy.kernel.normalize import slugify
from stigmergy.kernel.page import MAX_BODY_LINES, SPLIT_CHUNK_LINES
from stigmergy.librarian import (
    acl_rules,
    base_inputs,
    config,
    dedup,
    edits,
    filing_port,
    gates,
    gather,
    gitcmd,
    identity,
    report,
)
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

# The first pass plus exactly one corrective retry; `config` owns it because the visibility
# timeout is computed from it.
MAX_AGENT_ATTEMPTS = config.MAX_AGENT_ATTEMPTS


@dataclass
class Result:
    """What `process_item` decided. `diagnostics_path` is NOT part of `report`: it names a local
    file for an operator, never for a submitter."""
    status: str
    result_ref: str = ""
    report: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    diagnostics_path: str = ""

    @property
    def error(self) -> str:
        """Why the row is where it is; empty on `filed`."""
        return "" if self.status == schema.FILED else self.report.get("summary", "")


@dataclass
class AgentPasses:
    """Agent passes STARTED and their summed cost — mutable and shared on purpose: `process_item`
    stamps both onto any `LibrarianError` on the way out."""
    count: int = 0
    cost_usd: float = 0.0


def _stamp_cost(result: "Result", passes: "AgentPasses") -> "Result":
    """`0.0` is a real answer: a park re-file or a pre-agent refusal spent nothing. `cost_usd` is
    the item's WHOLE model spend."""
    result.report["cost_usd"] = round(passes.cost_usd, 6)
    return result


@dataclass
class Deps:
    """Injected dependencies of `process_item`. `agent` is the PORT: this module is its only
    consumer, so the calls made on this field ARE the backend contract."""
    settings: object
    evidence: object
    agent: filing_port.FilingAgent
    registry: object
    acl_config: object = None
    repo: str = ""
    today: str = ""             # injectable clock: `as_of` must be reproducible in a test

    def as_of(self) -> str:
        return self.today or datetime.date.today().isoformat()


def _material(deps: Deps, item: dict) -> str:
    """The archived material: the blob, not `payload->>'text'` — retention does not purge blobs."""
    for key in item.get("blob_refs") or []:
        return deps.evidence.get(key).decode("utf-8", errors="replace")
    return (item.get("payload") or {}).get("text", "")


def _injection_categories(outcome) -> list[str]:
    """The categories the agent reported, filtered to the fixed set; an invented one is dropped."""
    found = []
    for finding in getattr(outcome, "findings", ()) or ():
        category = str((finding or {}).get("category", ""))
        if category in gates.INJECTION_CATEGORIES and category not in found:
            found.append(category)
    return found


def _stamp(ctx: gates.GateContext, deps: Deps, item: dict, *, cite_stem: str = "") -> dict:
    """Rewrite every NEW page's server-owned frontmatter, and return the values written. Must run
    BEFORE the gates: the returned dict is what `gate_frontmatter` re-reads the page against. If
    you change `entity` resolution here, change `report.filed`'s `_anchor_phrase` too. With
    `cite_stem` set, each stamped page also gains the `sources:` citation and a `ctx.page_declared`
    entry, which switches `gate_anchoring` to per-page mode. Stamps `entity: []` for an unresolved
    `entity`-kind outcome, so a partial list cannot survive a gate reordering.
    """
    anchoring = getattr(ctx.outcome, "anchoring", None) or {}
    kind = str(anchoring.get("kind", "")).lower() if isinstance(anchoring, dict) else ""
    # `ctx.registry`, never `deps.registry`: the former is the registry this commit PUBLISHES,
    # births included, and an entity born in this commit must stamp like any other.
    entity, unresolved = gates.resolve_entity_ids(anchoring, ctx.registry)
    if kind == "entity" and (unresolved or not entity):
        entity = []
    stamped = {"status": page_policy.FILED_STATUS, "as_of": deps.as_of(),
               "submitted_by": item["submitted_by"], "entity": entity}
    for path in ctx.in_lane_new_pages():
        if path in ctx.provenance_pages:
            continue    # stamped by `_stamp_attached_sources`, under the provenance group
        if path in ctx.born_entity_pages:
            continue    # an identity page carries its OWN anchor; `_declare_births` told the gates
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
            # `{**stamped}` snapshots THIS path's `acl`.
            ctx.page_declared[path] = {
                "page_type": str(getattr(ctx.outcome, "page_type", "") or ""),
                "anchoring": anchoring if isinstance(anchoring, dict) else {}}
            ctx.stamped_by_path[path] = {**stamped, "sources": list(cited)}
    return stamped


# A newline in a "title" forges a commit trailer: `x\n\nSubmitted-by: ceo@acme.com`.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# Trailer keys this message writes itself; the colon is what makes a collapsed subject still read
# as attribution in `git log --oneline`.
_RESERVED_TRAILER_RE = re.compile(r"(?i)\b(submitted-by|co-authored-by|signed-off-by)\s*:")


def _subject(title: str) -> str:
    """One safe commit subject from an agent-supplied title. Defang BEFORE truncating, or a
    forgery survives whenever it fits inside the limit."""
    collapsed = " ".join(_CONTROL_CHARS_RE.sub(" ", str(title or "")).split())
    defanged = _RESERVED_TRAILER_RE.sub(lambda m: f"{m.group(1)} ", collapsed)
    return " ".join(defanged[:60].split()).strip() or "capture"


def _commit_message(item: dict, outcome, page_path: str, *, n_sources: int = 0,
                    born=()) -> str:
    """One capture, one commit, one filed page — enforced by `_cross_check_outcome`. The type in
    the subject comes from the FOLDER the page landed in: the folder is the fact. An identity the
    commit CREATED is named in the body, so `git log` answers "where did this entity come from"
    with the capture that introduced it."""
    page_type = page_policy.type_for_folder(page_path) or "note"
    body = f"Filed by the librarian from capture #{item['id']}."
    if n_sources:
        body += f" {n_sources} source page(s) — the captured thread, verbatim — ride in it too."
    names = [_subject(str(e.get("name", ""))) for e in born if e.get("name")]
    if names:
        body += (f" Introduces {len(names)} new entity page(s) — {', '.join(names)} — born "
                 f"confirmed by the submitter; the registry is regenerated in this same commit.")
    return (f"feat({page_type}): {_subject(getattr(outcome, 'title', ''))}\n\n"
            f"{body}\n\n"
            f"Submitted-by: {item['submitted_by']}\n")


def _pii_label(finding) -> str:
    """The pattern label a PII finding names, read back out of the sentence the gate wrote."""
    return finding.message.split("what looks like ", 1)[-1].split(" near line")[0]


def _pii_line(finding) -> str:
    """The line a PII finding's locator ends with — the whole locator when it names no line."""
    return finding.locator.rsplit(":", 1)[-1]


def _pre_agent(conn, item: dict, deps: Deps, *, material: "str | None" = None) -> tuple:
    """Everything BEFORE any flow's agent runs: dedup (levels 1-2) and the material-level
    secrets/PII scan. Levels 1-2 match only rows with status='filed', so a rejected, parked or
    failed twin never collapses a live capture. Returns `(material, None)` to continue, or
    `(material, Result)` on a terminal state.
    """
    settings = deps.settings
    material = _material(deps, item) if material is None else material

    # level 1: retry collapse — no semantics at all
    retry = dedup.find_retry(conn, item, window_s=settings.dedup_window_s)
    if retry:
        return material, Result(schema.FILED, retry.result_ref,
                                report.filed_retry(original_id=retry.submission_id,
                                                   page_path=retry.page_path,
                                                   commit=retry.commit))

    # level 2: already in the graph
    already = dedup.find_already_filed(conn, item)
    if already:
        return material, Result(schema.REJECTED, "",
                                report.rejected_duplicate(page_path=already.page_path,
                                                          as_of=already.as_of))

    # secrets and PII over the MATERIAL: bounce the whole capture
    secret_hits = gates.scan_secrets(material, gitleaks_bin=settings.gitleaks_bin,
                                     label="your material")
    if secret_hits:
        # From `values`, never re-parsed out of `message`/`locator` — those are for humans.
        line, rule = secret_hits[0].values
        return material, Result(schema.REJECTED, "",
                                report.rejected_secret(line=line, rule_id=rule))

    pii_hits = gates.scan_pii([("your material", n, text)
                               for n, text in enumerate(material.splitlines(), start=1)])
    if pii_hits:
        hit = pii_hits[0]
        return material, Result(schema.REJECTED, "",
                                report.rejected_pii(line=_pii_line(hit),
                                                    pattern_label=_pii_label(hit)))
    return material, None


# How each flow finishes the shared stale-base sentence. Kept side by side so a reword of one is
# read against the other: the ordinary lane names the three repo-sourced inputs it would misjudge.
_STALE_BASE_TAIL_ORDINARY = (
    "against the ACL config, entity registry and contract linter of a commit the remote may have "
    "moved past hours ago. The likely cause is the GitHub App installation (revoked, or a token "
    "that has expired since this container started) or the network. The capture is left in the "
    "queue")
_STALE_BASE_TAIL_MEETING = (
    "against a commit the remote may have moved past. The capture is left in the queue")


def _resolve_filing_base(item: dict, deps: Deps, *, log_noun: str, stale_tail: str) -> tuple:
    """This item's base commit and the `deps` re-read at it, for either flow's preamble."""
    settings = deps.settings
    base = gitcmd.base_ref(deps.repo, settings.branch)
    # Deployed only: a non-remote base is a FAULT, not a fallback. Raising rather than returning
    # a `Result` releases the item instead of failing the row.
    if settings.require_remote_base and not base.remote:
        raise StaleBaseError(
            f"the base resolved to the local {base.describe()} instead of origin/{settings.branch} "
            f"— the fetch failed, so this deployed worker would judge capture #{item['id']} "
            f"{stale_tail}")
    log.info("filing %s %s against %s", log_noun, item["id"], base.describe())

    # Re-read per item at THIS item's base commit: a cached ACL config fails silently OPEN.
    return base, dataclasses.replace(deps,
                                     registry=base_inputs.load_registry(deps.repo, base),
                                     acl_config=base_inputs.load_acl(deps.repo, base))


def process_item(conn, item: dict, deps: Deps, *, material: "str | None" = None) -> Result:
    """Take one claimed queue row to a terminal state. Never raises for an ordinary refusal —
    every outcome is a `Result`; only an unexpected error propagates."""
    material, early = _pre_agent(conn, item, deps, material=material)
    if early is not None:
        return early
    settings = deps.settings

    base, deps = _resolve_filing_base(item, deps, log_noun="submission",
                                      stale_tail=_STALE_BASE_TAIL_ORDINARY)
    passes = AgentPasses()
    # The contract linter is materialized from THIS item's base commit, not the operator's disk.
    with base_inputs.linter_at(deps.repo, base) as linter_path, \
            gitcmd.ephemeral_worktree(deps.repo, base.sha, settings.worktree_root) as worktree:
        try:
            return _run_in_worktree(conn, item, deps, material, worktree, passes,
                                    linter_path=linter_path)
        except LibrarianError as ex:
            # Annotate, then re-raise unchanged: the worker owns the `failed` decision.
            ex.at_agent_attempt(passes.count, cost_usd=passes.cost_usd)
            raise


# ── the removal flow: a person's own deletion, performed by the ONE writer (ADR 044 D3) ───────
# The third kind that does not ride `process_item`, and the only one whose material is not material
# at all: a `delete` row carries the REASON a person gave, and its hints carry the pages. What it
# shares with every other row is everything that makes a queue worth having — a durable row, a
# lease, an attempt count, an audited submitter, and `brain_submissions` to read the outcome from.
#
# The judgment was made at the door: only an identity that can see the whole corpus may queue one,
# because a removal touches every page that refers to the ones it names. Nothing here re-decides
# that. What runs here is the part that needs a checkout and a credential — and this process is
# the only one that has either.
_STALE_BASE_TAIL_DELETE = (
    "against a commit the remote may have moved past, so the pages it would remove and the pages "
    "it would rewrite are both read from a stale tree. The removal is left in the queue")


def process_delete_item(conn, item: dict, deps: Deps) -> Result:
    """Take one claimed `delete` row to a terminal state.

    The sequence, and why each step is where it is:

    1. **`_pre_agent`, unchanged.** The reason is text a person wrote, so it is scanned for secrets
       and personal data exactly as any capture's material is — a token pasted into "why" would
       otherwise land in a commit message, where no gate looks.
    2. **Plan against THIS item's base**, in a fresh worktree: which pages go, and which pages
       refer to them. A refusal here is the person's to act on (a page that is not there, a path
       the lane may not touch), so it is `rejected` with the lane's own sentence, never `failed`.
    3. **A model writes the pages that stay** (`repair.sweep`), because dropping a reference is a
       prose problem: a sentence that cited a removed page still has to read.
    4. **The nine gates judge the diff**, told the two facts only this caller knows — which paths
       it may remove, and the exact bytes it computed for every page it rewrites.
    5. **Commit and push**, through the same lease-fenced seam every filing uses. The trailer names
       the person: this is the one write in the system a human decided (ADR 043 D2).

    The per-page diffs go into the row's `report`, and that is the whole of ADR 043 D5 in the new
    shape: nobody read that prose before it landed, so the reading happens afterwards, wherever the
    row is read back.
    """
    from stigmergy.repair import brief as repair_brief
    from stigmergy.repair import deletion as repair_deletion
    from stigmergy.repair import sweep as repair_sweep
    from stigmergy.repair.errors import RepairError
    from stigmergy.repair.settings import RepairSettings

    material, early = _pre_agent(conn, item, deps)
    if early is not None:
        return early
    settings = deps.settings
    paths = schema.delete_paths(item.get("hints"))
    base, deps = _resolve_filing_base(item, deps, log_noun="removal",
                                      stale_tail=_STALE_BASE_TAIL_DELETE)
    repair_settings = RepairSettings.from_env()

    with base_inputs.linter_at(deps.repo, base) as linter_path, \
            gitcmd.ephemeral_worktree(deps.repo, base.sha, settings.worktree_root) as worktree:
        spend: list = []
        try:
            ops = repair_deletion.plan(worktree, paths)
            oversize = repair_deletion.oversize_reason(ops, repair_settings.max_plan_bytes)
            if oversize:
                raise RepairError(oversize)
            ops = repair_sweep.write_sync(worktree, ops,
                                          skill_text=repair_brief.read_skill(worktree),
                                          model_name=repair_settings.model, spend=spend)
            diffs = repair_deletion.unified_diffs(worktree, ops)
            edited, findings = repair_deletion.apply_declared(worktree, ops)
        except RepairError as ex:
            # Every sentence this lane raises is written to be published (its own module
            # docstrings), so it travels verbatim into a report the person who asked reads back.
            return Result(schema.REJECTED, "", report.rejected_unremovable(reason=str(ex)))
        if findings:
            return Result(schema.REJECTED, "", report.rejected_unremovable(
                reason=f"the pages moved under this removal "
                       f"({', '.join(sorted({f.code for f in findings}))}) — nothing was deleted"))
        return _commit_delete(conn, item, deps, worktree, ops, diffs, edited, material,
                              linter_path=linter_path, model_calls=len(spend))


def _commit_delete(conn, item: dict, deps: Deps, worktree: str, ops: list, diffs: dict,
                   edited: list, material: str, *, linter_path: str, model_calls: int) -> Result:
    """The gates, the commit and the push for a performed removal.

    `material=""` and `outcome=None` are honest: nothing was captured and no filing agent wrote
    here — the ops came off a row a person typed. Every gate that reads either is scoped to CREATED
    pages, and this diff has none.
    """
    from stigmergy.repair import deletion as repair_deletion

    settings = deps.settings
    ctx = gates.GateContext(
        worktree=worktree, entries=gitcmd.diff_entries(worktree),
        added=gitcmd.added_lines(worktree),
        material="", outcome=None, registry=deps.registry,
        linter_path=linter_path, gitleaks_bin=settings.gitleaks_bin,
        # The three caller-scoped facts this flow is allowed to declare, each derived from the ops
        # that were just performed: the lane the plan spans, the paths it may REMOVE, the bytes it
        # computed for every page it rewrites, and — among those — the machine-zone pages whose
        # provenance stamps it only ever removes a link from.
        write_prefixes=repair_deletion.lane_for(ops),
        deletions_allowed=frozenset(repair_deletion.deleted_paths(ops)),
        expected_bytes=repair_deletion.expected_bytes(ops),
        provenance_pages=repair_deletion.provenance_scrubs(ops))
    veto = gates.vetoes(gates.run_gates(ctx))
    if veto:
        return Result(schema.REJECTED, "", report.rejected_unremovable(
            reason=f"the gates refused this removal, so nothing was committed or pushed: "
                   f"{'; '.join(f'{f.gate}/{f.code}' for f in veto)}"))
    surviving = _surviving_dead_links(worktree, linter_path, ops)
    if surviving:
        return Result(schema.REJECTED, "", report.rejected_unremovable(reason=surviving))

    message = _delete_commit_message(item, ops, material)
    sha = _commit_and_push(conn, item, deps, ctx, worktree, message, what="this removal")
    return Result(schema.FILED, f"{repair_deletion.deleted_paths(ops)[0]}@{sha}",
                  report.filed_delete(deleted=repair_deletion.deleted_paths(ops),
                                      rewritten=diffs, commit=sha, model_calls=model_calls))


def _surviving_dead_links(worktree: str, linter_path: str, ops: list) -> str:
    """The knowledge repo's OWN linter, over the whole tree, asked one question: does anything
    still link to a page this sweep removed? Returns the refusal, or `""`.

    `gate_contract` filters the linter's findings to the pages a diff TOUCHED, which is right for
    every other flow and blind for this one — a deletion's blast radius is the whole graph, and a
    page the sweep never planned is exactly where a missed reference would sit. Scoped to the
    deleted stems rather than vetoing on ANY error: a corpus that already carries an unrelated
    contract error is not this removal's fault.
    """
    from stigmergy.repair import deletion as repair_deletion

    stems = {repair_deletion.page_stem(path) for path in repair_deletion.deleted_paths(ops)}
    report_json = gates.lint_report(worktree, linter_path)
    surviving = set()
    for finding in report_json.get("findings", []):
        if finding.get("check") != gates.DEAD_LINKS_CHECK or finding.get("severity") != "error":
            continue
        target = gates.dead_link_target(
            gates.Finding("contract", gates.DEAD_LINKS_CHECK, str(finding.get("message", ""))))
        if target and repair_deletion.link_stem(target) in stems:
            surviving.add((str(finding.get("file", "")), target))
    if not surviving:
        return ""
    named = ", ".join(f"{path} still links [[{target}]]" for path, target in sorted(surviving))
    return (f"this removal would leave the corpus with a dead link, so nothing was committed or "
            f"pushed: {named}. The sweep did not plan a rewrite of that page — if it happens "
            f"twice, the reference is spelled in a shape the sweep does not read")


def _delete_commit_message(item: dict, ops: list, reason: str) -> str:
    """The commit a removal lands as. `Approved-by:` names the person who asked for it — this is
    the ONE write in the system a human decided, and the trailer is half of how `git log` answers
    who authorized a change to the corpus (the other half is the App author line).

    `reason` is the MATERIAL the flow already read and already scanned for secrets, handed in
    rather than re-read off the row here: the bytes that go into a permanent commit message must be
    the same bytes `_pre_agent` cleared, and a second read is a second chance for the two to be
    different text.
    """
    from stigmergy.repair import deletion as repair_deletion

    removed = repair_deletion.deleted_paths(ops)
    stems = [repair_deletion.page_stem(path) for path in removed]
    first = stems[0] if stems else "the knowledge repo"
    subject = (f"chore(repair): delete {len(removed)} page(s) — {first}"
               + ("…" if len(stems) > 1 else ""))
    reason = " ".join(str(reason or "").split())
    actor = " ".join(str(item.get("submitted_by") or "").split())
    return (f"{subject}\n\n{reason}\n\n"
            f"Capture #{item['id']}.\nApproved-by: {actor}")


def _run_in_worktree(conn, item: dict, deps: Deps, material: str, worktree: str,
                     passes: "AgentPasses | None" = None, *, linter_path: str = "") -> Result:
    """The retry POLICY: one pass, one corrective pass, then refuse. `OutcomeShapeError` reaches
    the corrective retry by the same road a gate veto takes."""
    settings = deps.settings
    corrective, findings, outcome, diagnostics = "", [], None, ""
    passes = passes if passes is not None else AgentPasses()

    for attempt in range(1, MAX_AGENT_ATTEMPTS + 1):
        passes.count = attempt
        try:
            result, findings, outcome = _one_pass(conn, item, deps, material, worktree, corrective,
                                                  passes=passes, linter_path=linter_path)
        except OutcomeShapeError as ex:
            # A backend that parses its own dict may leave an outcome file behind.
            result, findings, outcome = None, list(ex.findings), None
            agent_module.discard_outcome_file(worktree)
        if result is not None:
            return _stamp_cost(result, passes)

        # A veto naming no repair the agent can perform does not spend the retry.
        blocked = gates.unrepairable(findings)
        if attempt < MAX_AGENT_ATTEMPTS:
            if not blocked:
                corrective = gates.corrective_brief(findings)
                _reset_for_retry(worktree)
                continue
            log.warning("item %s: skipping the corrective retry after agent pass %s — %s name(s) "
                        "no repair the agent can perform", item.get("id"), attempt,
                        ", ".join(sorted({f"{f.gate}/{f.code}" for f in blocked})))

        diagnostics = preserve_refused_diff(worktree, item, findings,
                                            root=settings.refused_diff_root)
        break

    # `passes.count`, not `MAX_AGENT_ATTEMPTS`: the loop can end after one pass.
    return _stamp_cost(_refuse(item, findings, outcome, agent_attempts=passes.count,
                               diagnostics_path=diagnostics), passes)


def _reset_for_retry(worktree: str) -> None:
    """Put the worktree back to the commit it branched from, untracked leftovers included."""
    gitcmd.run("reset", "--hard", "HEAD", cwd=worktree)
    gitcmd.run("clean", "-fdq", cwd=worktree)


# The gathered block's wording for an EXPLORING backend. Name no individual tools — which tools
# exist is the BACKEND's fact.
_SEEDED_GATHERED_SENTENCES = {
    "preface": (
        "\nWhat this brain already holds, gathered from the checkout by the worker before this "
        "call — a STARTING POINT, not a boundary: your own tools reach the same checkout this was "
        "read from, so look further whenever this is not enough."),
    "all_trimmed_advice": (
        "Search the checkout for what this material overlaps with and read what you find, rather "
        "than judging overlap from `link_names` and `neighbourhood` alone."),
}


def _declared_port_attr(agent, name: str, purpose_clause: str) -> bool:
    """One filing-port capability, read off the backend and REFUSED when absent."""
    if not hasattr(agent, name):
        raise AgentError(
            f"the configured agent ({type(agent).__name__}) declares no `{name}`, which is a "
            f"required member of the filing port (librarian/filing_port.py): {purpose_clause}. A "
            f"backend declares it as a class attribute; a WRAPPER around one must copy it from "
            f"what it wraps")
    return bool(getattr(agent, name))


def _wants_gathered(agent) -> bool:
    """Does this backend want the gatherer's block? DECLARED, never defaulted: a
    `getattr(..., False)` would silently hand a wrapper's backend an empty seed."""
    return _declared_port_attr(
        agent, "wants_gathered",
        "the worker cannot tell whether to build this item's gathered context for it")


def _one_pass(conn, item: dict, deps: Deps, material: str, worktree: str,
              corrective: str, *, passes: "AgentPasses | None" = None,
              linter_path: str = "") -> tuple:
    """ONE agent pass: run it, apply its declared edits, stamp, and run every gate. Returns
    `(result, findings, outcome)`; a non-`None` `result` is TERMINAL, otherwise `findings` is what
    the corrective brief (or the final refusal) is built from.
    """
    settings = deps.settings
    # DECLARED, never defaulted: `True` means the account carries the page's text home.
    structured = _declared_port_attr(
        deps.agent, "structured_ordinary",
        "the worker cannot tell whether to gather a context for it and whether it writes its own "
        "page")
    # `None` for every capture whose door did not assert a source attachment.
    attachment = _source_attachment(item)
    # Server-composed. Left implicit, the brief's genre rules make a whole document read as
    # `type: source` and the agent parks a capture whose source half code already took.
    flow_note = "" if attachment is None else (
        f"SYSTEM NOTE (from the pipeline, not from the submitter): this capture arrived through "
        f"the {attachment.source_kind} door as a whole document. The system itself attaches the "
        f"VERBATIM material as a `sources/` page set in this same commit — the source half is "
        f"already handled; it is not yours to write, and it is not a reason to park. Your whole "
        f"job is the SYNTHESIS: file exactly one note/decision/concept page distilling what this "
        f"document establishes, anchored through the registry as always; the system will make "
        f"your page cite the attached source.")
    # Built HERE, never inside a backend: both must share one context builder and one fence
    # discipline. Re-run on the corrective pass, because `_reset_for_retry` resets the tree.
    gathered = ""
    if structured or _wants_gathered(deps.agent):
        gathered = agent_module.render_gathered(
            gather.gather(worktree, deps.registry, material,
                          top_k=settings.gather_top_k,
                          excerpt_lines=settings.gather_excerpt_lines),
            **({} if structured else _SEEDED_GATHERED_SENTENCES))
    try:
        run = deps.agent.run(worktree=worktree, material=material,
                             hints=(item.get("hints") or {}).get("client", {}),
                             submitted_by=item["submitted_by"], corrective=corrective,
                             flow_note=flow_note,
                             gathered=gathered)
    except AgentError as ex:
        # A pass that died mid-run still spent real money.
        if passes is not None:
            passes.cost_usd += getattr(ex, "run_cost_usd", 0.0)
        raise
    if passes is not None:
        passes.cost_usd += run.cost_usd
    outcome = run.outcome
    # Consume the outcome file before the diff is taken: it must never reach a commit.
    agent_module.discard_outcome_file(worktree)
    if outcome is None:
        raise AgentError("the agent produced no usable account of what it did")

    # CODE writes the page on the structured path. Must stay BEFORE the `GateContext` and
    # `edits.apply_declared`, so the stamp and gates judge it as they judge the agent's.
    if structured:
        page_findings = _require_page_content(outcome)
        if page_findings:
            # RETURNED, not raised: `OutcomeShapeError` reaches the retry with `outcome = None`,
            # which would lose a recorded steering attempt.
            return None, page_findings, outcome
        written_page = _write_ordinary_page(worktree, outcome, created=deps.as_of())
        if isinstance(written_page, list):
            return None, written_page, outcome
        # The plan's own `path` is NOT carried forward: `_file` reads `page_path` off the DIFF.

    # The identities the account introduces, created BEFORE the diff the gates judge is taken, so
    # the entity pages and the regenerated registry land in the same diff as the note — and the
    # gates resolve against the registry this commit will PUBLISH.
    births = identity.write_births(
        worktree, outcome=outcome, base_registry=deps.registry, material=material,
        hints=(item.get("hints") or {}).get("client", {}), today=deps.as_of(),
        registration=schema.registration_from_hints(item.get("hints")),
        approver=str(item.get("submitted_by") or ""),
        related=[outcome.title] if outcome.title else ())
    if isinstance(births, list):
        return None, births, outcome

    # With the attachment ON, the lane widens by exactly the attachment's own folder; with
    # births, by exactly the identity zone and the registry file.
    write_prefixes = gates.ALLOWED_WRITE_PREFIXES
    creatable_types = frozenset(page_policy.FAST_LANE_TYPES)
    extra_folder_types = {}
    if attachment is not None:
        write_prefixes += (attachment.prefix,)
        creatable_types |= {"source"}
        extra_folder_types[attachment.prefix.rstrip("/")] = "source"
    ctx = gates.GateContext(
        worktree=worktree,
        entries=gitcmd.diff_entries(worktree),
        added=gitcmd.added_lines(worktree),
        material=material, outcome=outcome, registry=births.registry,
        # The linter materialized from this item's base commit — NOT `settings.linter_path`.
        linter_path=linter_path, gitleaks_bin=settings.gitleaks_bin,
        write_prefixes=write_prefixes, creatable_types=creatable_types,
        extra_folder_types=extra_folder_types)
    _declare_births(ctx, births)

    if not ctx.entries:
        raise AgentError("code wrote nothing for a capture the agent decided to file"
                         if structured else
                         "the agent wrote nothing for a capture it decided to file")

    # Code's own additive edits, from the agent's DECLARATION.
    # Applied before the diff the gates judge is taken, so the edits land in the same diff.
    edited, edit_findings = edits.apply_declared(
        worktree, outcome.edits, new_pages=ctx.in_lane_new_pages())

    # After `apply_declared` (its edits validated against the agent's own pages only) and before
    # `_stamp` (the whole set stamped and judged as one diff).
    written_sources = None
    if attachment is not None:
        written_sources = _write_attached_sources(worktree, attachment, outcome, material)
        if isinstance(written_sources, list):
            return None, written_sources, outcome
        ctx.provenance_pages = frozenset(written_sources["paths"])
    ctx.entries = gitcmd.diff_entries(worktree)
    ctx.added = gitcmd.added_lines(worktree)

    if attachment is not None:
        _stamp_attached_sources(ctx, deps, item, written_sources["ids_by_path"])
        ctx.stamped = _stamp(ctx, deps, item, cite_stem=written_sources["stems"][0])
    else:
        ctx.stamped = _stamp(ctx, deps, item)

    # Stamping changed the pages the gates are about to judge.
    ctx.entries = gitcmd.diff_entries(worktree)
    ctx.added = gitcmd.added_lines(worktree)
    findings = gates.run_gates(ctx) + edit_findings + _cross_check_outcome(ctx)
    if not gates.vetoes(findings):
        return (_file(conn, item, deps, ctx, outcome, findings, worktree, edited=edited,
                      source_pages=tuple(written_sources["paths"]) if written_sources else (),
                      births=births),
                [], outcome)
    return None, findings, outcome


def _declare_births(ctx: gates.GateContext, births: identity.Births) -> None:
    """Tell the gates what `identity.write_births` did — the lane, the byte proofs, which
    entity-zone entries are this run's own, and who each one names. Told, never inferred: a gate
    that worked out on its own which entity page was "probably ours" would be a gate the agent
    could talk to."""
    if not births.touched():
        return
    ctx.write_prefixes = ctx.write_prefixes + births.lane
    ctx.creatable_types = ctx.creatable_types | {page_policy.ENTITY_PAGE_TYPE}
    ctx.extra_folder_types = {**ctx.extra_folder_types,
                              identity.ENTITY_ZONE_PREFIX.rstrip("/"): page_policy.ENTITY_PAGE_TYPE}
    ctx.derived_files = ctx.derived_files | births.derived_files
    ctx.expected_bytes = {**ctx.expected_bytes, **births.expected_bytes}
    ctx.born_entity_pages = frozenset(births.entity_pages)
    ctx.confirmed_entity_pages = dict(births.confirmed)
    for path, entity_id in births.entity_pages.items():
        # What `gate_frontmatter` re-reads the page against: its own anchor and its own state.
        ctx.stamped_by_path[path] = {"status": page_policy.FILED_STATUS, "entity": [entity_id],
                                     "approved_by": births.confirmed.get(path, "")}



# Nothing in the account can name a path: `page.FOLDER_BY_TYPE` decides the folder, the title the
# filename.


def _require_page_content(outcome) -> list:
    """The REQUIRED half of the outcome envelope for a `structured_ordinary` backend. Returns
    findings rather than raising; `[]` means code can write a page from it."""
    page = getattr(outcome, "page", None)
    if page is None or not (page.body or "").strip():
        return [gates.Finding(
            agent_module._OUTCOME_GATE, "missing-field",
            "declares a filing but carries no `page.body`: on this backend the worker writes the "
            "page from your account, so the page's own text has to be in it — there is no file you "
            "wrote for the worker to find instead",
            brief='return the page\'s whole text in `page`: {"title": …, "page_type": …, "body": '
                  '"…the page below its H1…"}. The worker writes the file, the frontmatter and the '
                  'H1 itself; you never name a path.')]
    return []


def _ordinary_stem(title: str) -> str:
    """The filename this page is filed under: its TITLE, not a slug — a wikilink resolves by bare
    basename. Only the EDGES are trimmed: collapsing interior whitespace would make
    `evil\\nSubmitted-by: ...` a legal filename instead of letting `page.unnameable_reason` refuse it.
    """
    return str(title or "").strip()


def _build_ordinary_page(title: str, page_type: str, body: str, links, created: str) -> str:
    """One fast-lane page's DRAFT. `status` is server-owned and deliberately absent: `_stamp`
    writes it. Brackets are stripped before being re-added, or `[[X]]` would become `[[[[X]]]]`."""
    related = [f"[[{str(name).strip().strip('[]').strip()}]]"
               for name in (links or ()) if str(name).strip().strip("[]").strip()]
    front = [
        f"type: {page_type}",
        f"title: {_yaml_str(title)}",
        f"created: {created}",
        f"updated: {created}",
        f"tags: [{page_type}]",
        f"related: [{', '.join(_yaml_str(link) for link in related)}]",
        "sources: []",
    ]
    body_text = (body or "").strip()
    return "---\n" + "\n".join(front) + "\n---\n\n" + f"# {title}\n\n{body_text}\n"


def _write_ordinary_page(worktree: str, outcome, *, created: str) -> "dict | list":
    """Write the one page this capture files, or hand back one veto finding having written nothing.
    Four refusals, cheapest first: uncreatable type, unnameable title, existing page, path outside
    the checkout. The folder is DERIVED, never declared, so lane confinement is construction; the
    last check RESOLVES the path, since one inside the lane by spelling can be outside by resolution.
    """
    declared_type = str(outcome.page_type or "")
    policy = page_policy.classify_page_type(declared_type)
    if not policy.creatable:
        # Routed to the STEWARD, not `failed`. `locator` is EMPTY because nothing was written yet,
        # so `_refuse`'s steering branch must skip it.
        return [gates.Finding(
            "zone", gates.TYPE_NOT_CREATABLE,
            f"the account asks for a {policy.page_type or 'typeless'!r} page, which the fast lane "
            f"cannot create: {policy.reason}",
            locator="", values=(policy.page_type,))]

    # `outcome.title`, never `outcome.page.title`, or filename and commit subject could disagree.
    stem = _ordinary_stem(outcome.title)
    unnameable = page_policy.unnameable_reason(stem)
    if unnameable:
        return [gates.Finding(
            agent_module._OUTCOME_GATE, "unnameable-page",
            f"the page's title cannot be a filename: {unnameable}",
            locator=stem)]

    path = f"{policy.folder}/{stem}.md"
    if page_policy.path_key(path) in page_policy.path_keys(gitcmd.tracked_paths(worktree)):
        # `path_key`, not `==`: the filesystem folds case and Unicode, so a re-spelled title
        # names an EXISTING page.
        return [gates.Finding(
            agent_module._OUTCOME_GATE, "existing-page-collision",
            f"a page already exists at {path} — this material may already be filed, or the title "
            f"needs to be the one that distinguishes this page from it",
            locator=path,
            brief=f"`{path}` already exists and the worker never writes over a page. If this "
                  f"material genuinely adds to that page, declare an `overlap` edit against it and "
                  f"file this one under a title that says what is new; if it IS that page, park "
                  f"the capture instead of filing a second copy.")]

    if not page_policy.is_inside(worktree, path):
        # `repairable=False`: the escape is a symlinked directory component, not this title.
        return [gates.Finding(
            agent_module._OUTCOME_GATE, "outside-worktree",
            f"{path} does not resolve inside this capture's own checkout — a directory on that "
            f"path is a symlink out of it; nothing was written",
            locator=path, repairable=False)]

    _write_new(worktree, path,
               _build_ordinary_page(stem, policy.page_type, outcome.page.body,
                                    outcome.links_created, created))
    return {"path": path, "stem": stem}


# The refused diff, preserved. The asymmetry IS the safety property: REMOVED lines are kept
# verbatim (already-committed content), ADDED lines are only counted (untrusted captured material
# must never be written beside the queue). Bounded twice, because a diff is attacker-sized.
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
    """Write the digest beside the queue and return its path, `""` when it could not be written.
    Never raises: losing diagnostics must not change an item's outcome."""
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
    # The PATH only — this line names a file rather than quoting one.
    log.error("item %s was refused; the refused diff is preserved at %s", item.get("id"), path)
    return path


def _cross_check_outcome(ctx: gates.GateContext) -> list:
    """The agent's account must AGREE with the diff — the diff decides. `page_path` must be empty
    or a page the diff created, and the outcome must have created EXACTLY one page in
    the lane: `_file` takes `in_lane_new_pages()[0]` for `page_path`, `result_ref`, the commit
    subject and the dedup pointer, so a second page lands on no surface a human reads and can ride
    past `gate_anchoring`. `ctx.provenance_pages` is excluded, or attached captures are all vetoed.
    """
    new_pages = [p for p in ctx.in_lane_new_pages()
                 if p not in ctx.provenance_pages and p not in ctx.born_entity_pages]
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


def _commit_and_push(conn, item, deps, ctx, worktree, message, *, what: str) -> str:
    """The gated commit and its push, for either flow; returns THE sha the push produced."""
    from stigmergy.librarian import githubapp

    author_name, author_email = githubapp.identity()
    # `gated_entries`: the diff the gates approved is the diff that lands, BYTES included. Pass
    # the entries `run_gates` was handed — re-deriving samples after the window being checked.
    gitcmd.commit(worktree, message=message, author_name=author_name, author_email=author_email,
                  gated_entries=ctx.entries)

    remote_url, config_env = "", {}
    if githubapp.configured():
        slug = githubapp.repo_slug(deps.repo)
        remote_url = githubapp.push_url(slug)
        # Minted as late as possible and handed over in the ENVIRONMENT, never in argv.
        config_env = githubapp.push_config(githubapp.installation_token(), slug)

    # Re-assert the lease immediately before the push, the only irreversible step: a lost lease
    # means the row was redelivered, and `finish` would refuse it only after the commit landed.
    # The window is longest on the meeting flow: more pages, more gate work.
    if not queue.holds_lease(conn, item["id"], expected_attempts=item["attempts"]):
        raise LeaseLostError(
            f"the lease on submission {item['id']} (delivery {item['attempts']}) was lost while "
            f"{what} was being processed; nothing was pushed")
    # THE sha from the push, not the local commit: after a rebase-and-retry the pre-push sha
    # names nothing reachable.
    return gitcmd.push(worktree, branch=deps.settings.branch, remote_url=remote_url,
                       config_env=config_env, author_name=author_name, author_email=author_email)


def _file(conn, item, deps, ctx, outcome, findings, worktree, *, edited=(),
          source_pages=(), births=None) -> Result:
    """The gates passed: commit, push, and say what happened. `page_path` comes from the DIFF,
    never the outcome. `source_pages` arrives in PART order from the writer's own plan, and is
    excluded when picking `page_path` because `sources/` sorts before `wiki/`; the identity pages
    this run created are excluded for the same reason — the note is the page this capture filed.
    """
    page_path = [p for p in ctx.in_lane_new_pages()
                 if p not in ctx.provenance_pages and p not in ctx.born_entity_pages][0]
    born = births.entities if births is not None else []
    added_aliases = births.aliases if births is not None else []
    message = _commit_message(item, outcome, page_path, n_sources=len(source_pages),
                              born=born)
    sha = _commit_and_push(conn, item, deps, ctx, worktree, message, what="this item")

    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]
    notes += [f.message for f in findings if f.severity == gates.SEVERITY_NOTE]
    return Result(
        schema.FILED, f"{page_path}@{sha}",
        # `ctx.registry`, the registry this commit PUBLISHED: a newborn entity's name renders
        # from it, where `deps.registry` (the base commit's) would print the bare id.
        report.filed(page_path=page_path, commit=sha,
                     anchoring=outcome.anchoring or {}, registry=ctx.registry,
                     links=list(outcome.links_created),
                     overlaps=[dict(o) for o in outcome.overlaps],
                     pages_edited=list(edited),
                     agent_rationale=getattr(outcome, "summary", ""),
                     findings=notes,
                     source_pages=list(source_pages),
                     entities_born=born, aliases_added=added_aliases,
                     entities_updated=births.updates if births is not None else []),
        findings=notes)


def _route_refusal(item, findings, outcome, *, agent_attempts: int = 0,
                   diagnostics_path: str = "") -> Result:
    """Which terminal state a surviving veto earns — the routing BOTH flows share. Two states:
    `rejected` when the cause is the submitter's to fix, `failed` when it is the librarian's.
    An anchor that still does not resolve after the corrective pass is the librarian's — the brief
    told the agent it could propose the entity, and it did not."""
    veto = gates.vetoes(findings)
    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]

    # GATE AND CODE, never code alone: `gate_contract` builds its codes verbatim from the
    # knowledge repo's linter JSON, so a check named `secret` would land here with no values.
    secret = next((f for f in veto if f.gate == "secrets" and f.code == "secret"), None)
    if secret:
        # From `values`: a hit visible only after adjacent lines were rejoined has no locator line.
        line, rule = secret.values
        return Result(schema.REJECTED, "",
                      report.rejected_secret(line=line, rule_id=rule,
                                             where="the drafted page"),
                      diagnostics_path=diagnostics_path)
    # Gate AND code, and irreversible if wrong: `reason_code=pii` is in `schema.WITHHELD_REASONS`,
    # so `worker._finish` purges the submitter's payload and hints outright.
    pii = next((f for f in veto if f.gate == "pii" and f.code == "pii"), None)
    if pii:
        return Result(schema.REJECTED, "",
                      report.rejected_pii(line=_pii_line(pii), pattern_label=_pii_label(pii),
                                          where="the drafted page"),
                      diagnostics_path=diagnostics_path)

    # `repairable` AND `locator`: this feeds `report.rejected_steering(path=…)`, so a finding
    # naming no path belongs on `_uncreatable_type`'s road. `repairable` also excludes the
    # UNREPAIRABLE zone findings: no agent could have produced them, so they are system faults and
    # must not read as steering just because a category was declared alongside.
    zone = next((f for f in veto if f.gate == "zone" and f.repairable and f.locator), None)
    categories = _injection_categories(outcome)
    if zone and categories:
        # The veto fired AND the material carries a traceable steering attempt: content-actionable.
        return Result(schema.REJECTED, "",
                      report.rejected_steering(path=zone.locator, category=categories[0],
                                               findings=notes),
                      diagnostics_path=diagnostics_path)

    if _frontmatter_only(veto):
        # Two sentences, ONE reason code either way — a read path may branch on the code, not prose.
        codes = {f.code for f in veto}
        builder = (report.rejected_malformed_frontmatter if codes == {"unparseable"}
                  else report.rejected_forged_field)
        return Result(schema.REJECTED, "", builder(findings=notes),
                      diagnostics_path=diagnostics_path)

    # Anything else is the librarian failing at its job, not the submitter's problem.
    worst = veto[0] if veto else None
    return Result(schema.FAILED, "",
                  report.failed_system(attempts=item.get("attempts", 1),
                                       agent_attempts=agent_attempts,
                                       stage=worst.gate if worst else "gates",
                                       reason=worst.message if worst else "unknown",
                                       # Into the REPORT, not only onto the `Result`: the report is
                                       # what `queue.finish` persists.
                                       findings=notes),
                  findings=notes, diagnostics_path=diagnostics_path)


def _refuse(item, findings, outcome, *, agent_attempts: int = 0,
            diagnostics_path: str = "") -> Result:
    """Both ordinary attempts vetoed."""
    return _route_refusal(item, findings, outcome, agent_attempts=agent_attempts,
                          diagnostics_path=diagnostics_path)


def _frontmatter_only(veto) -> bool:
    """Is "the frontmatter gate refused this page" the WHOLE story? Every code it emits is
    content-caused, so the destination is `rejected`; any other gate alongside forces `failed`."""
    return bool(veto) and all(f.gate == "frontmatter" for f in veto)


def failure_result(item: dict, stage: str, reason: str, *, agent_attempts: int = 0,
                   cost_usd: float = 0.0) -> Result:
    """The worker's wrapper for an unexpected error; always a system fault. `agent_attempts`
    defaults to 0 because these can be raised before the agent ran, and the report then omits it."""
    return Result(schema.FAILED, "",
                  report.failed_system(attempts=item.get("attempts", 1), stage=stage,
                                       reason=reason, agent_attempts=agent_attempts,
                                       cost_usd=cost_usd))


# The KNOWN ways processing can fail, so the report names a stage instead of "unexpected".
# `CaptureError` is here for `EvidenceError`: a purged blob is diagnosable, not a bug.
PROCESSING_ERRORS = (AgentError, GitError, WorktreeError, LeaseLostError, CaptureError)


# The fast lane's source ATTACHMENT: a parameter, never a third flow. The door rule: material with
# independent documentary existence files a `sources/` page beside the synthesis; a conversational
# capture leaves none. With the parameter OFF the fast lane builds the ctx it always would.

SLACK_SOURCE_PREFIX = "sources/slack/"
DOCUMENT_SOURCE_PREFIX = "sources/documents/"


@dataclass(frozen=True)
class SourceAttachment:
    """Which `sources/` page set one fast-lane capture attaches. Built ONLY from facts a DOOR
    asserted server-side: `capture.schema.reject_source_provenance_hints` refuses these hints at
    the client seam, which is what makes keying a FLOW decision on a hint sound.
    """
    prefix: str          # the zone folder, trailing slash included ("sources/slack/")
    source_kind: str     # the contract's `source_kind:` enum value
    tags: tuple
    url: str             # `url:` on every part; "" when the door sent none
    suffix: str          # titles read "<title> — <suffix>", stems "<slug>-<suffix>"


def _source_attachment(item: dict) -> "SourceAttachment | None":
    """The parameter's ON/OFF switch, decided per item from the row's own `kind` or from a fact
    a DOOR asserted server-side; `None` for every ordinary capture. Two ON positions: a
    `document` (the kind says the material has documentary existence of its own; `source_url` is
    the submitter's claim of where, attributed like the material — ADR 044 D4) and the Slack door
    (the `source_client` hint, which only that transport may assert).
    """
    client = (item.get("hints") or {}).get("client") or {}
    if item.get("kind") == schema.DOCUMENT:
        return SourceAttachment(prefix=DOCUMENT_SOURCE_PREFIX, source_kind="upload",
                                tags=("source", "document"),
                                url=str(client.get("source_url") or ""), suffix="document")
    if client.get("source_client") != schema.SLACK_DOOR:
        return None
    return SourceAttachment(prefix=SLACK_SOURCE_PREFIX, source_kind="slack",
                            tags=("source", "slack-thread"),
                            url=str(client.get("source_permalink") or ""), suffix="thread")


def _write_attached_sources(worktree: str, attachment: SourceAttachment, outcome,
                            material: str) -> "dict | list":
    """CODE writes the attached source page(s), verbatim, through the shared `_build_source_parts`
    writer. All-or-nothing: a collision returns one veto finding with nothing written. Returns the
    plan in PART order, which `_file`'s report and the `sources:` citation rely on.
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


def _stamp_one_source(ctx: gates.GateContext, path: str, *, submitted_by: str, as_of: str,
                      digest: str, extracted_at: str, page_id: str) -> None:
    """Stamp ONE `sources/` page with the provenance group — THE source stamp for every flow."""
    ctx.page_declared[path] = {"page_type": "source"}
    _rewrite(ctx.worktree, path, lambda text: page_policy.stamp_source_fields(
        text, submitted_by=submitted_by, as_of=as_of, content_hash=digest,
        extracted_at=extracted_at, page_id=page_id))
    # The provenance group MUST appear here — `gate_frontmatter`'s output-equality check covers
    # only what `stamped_by_path` records, and forging this group re-anchors the whole chain.
    # Render each value exactly as `page.stamp_source_fields` writes it.
    ctx.stamped_by_path[path] = {
        "status": page_policy.FILED_STATUS, "as_of": as_of, "submitted_by": submitted_by,
        "content_hash": f"sha256:{digest}", "extracted_at": extracted_at, "tier": "1",
        **({"id": page_id} if page_id else {})}


def _stamp_attached_sources(ctx: gates.GateContext, deps: Deps, item: dict,
                            ids_by_path: dict) -> None:
    """Stamp the attachment's source pages with the PROVENANCE group (never the fast-lane group
    `_stamp` writes) and record each page's stamped fields for `gate_frontmatter`'s output-equality
    check. `tier` stays `"1"`: a captured thread is a primary recording.
    """
    digest = hashlib.sha256((ctx.material or "").encode("utf-8")).hexdigest()
    extracted_at = datetime.datetime.now(datetime.UTC).isoformat()
    for path in sorted(ctx.provenance_pages):
        _stamp_one_source(ctx, path, submitted_by=item["submitted_by"], as_of=deps.as_of(),
                          digest=digest, extracted_at=extracted_at,
                          page_id=str(ids_by_path.get(path) or ""))


# The meeting flow: a page SET (source + meeting + N decisions), atomically or nothing. A SEPARATE
# entry point because it contradicts `process_item`'s invariant of exactly one new page per
# capture. The lane is flow-scoped on the `GateContext`, never the global `page.FOLDER_BY_TYPE`,
# so an ordinary capture claiming `type: meeting` still parks.

# EXACTLY these three folders, not the fast-lane set plus two: the knowledge-repo meeting brief and
# `agent.MEETING_ALLOWED_WRITE_RE` both name three, and widening this would make the injected
# prompt's claim false. If you change these, change both of those too.
MEETING_SOURCE_PREFIX = "sources/meetings/"
MEETING_MEETING_PREFIX = "wiki/meetings/"
MEETING_DECISION_PREFIX = "wiki/decisions/"
MEETING_WRITE_PREFIXES = (MEETING_SOURCE_PREFIX, MEETING_MEETING_PREFIX, MEETING_DECISION_PREFIX)
MEETING_CREATABLE_TYPES = frozenset({"source", "meeting", "decision"})
MEETING_EXTRA_FOLDER_TYPES = {"sources/meetings": "source", "wiki/meetings": "meeting"}


def process_meeting_item(conn, item: dict, deps: Deps) -> Result:
    """`process_item`'s sibling for `kind == "meeting"` rows; same never-raises contract."""
    material, early = _pre_agent(conn, item, deps)
    if early is not None:
        return early
    settings = deps.settings

    base, deps = _resolve_filing_base(item, deps, log_noun="meeting submission",
                                      stale_tail=_STALE_BASE_TAIL_MEETING)
    passes = AgentPasses()
    meeting_meta = _meeting_meta(item)
    with base_inputs.linter_at(deps.repo, base) as linter_path, \
            gitcmd.ephemeral_worktree(deps.repo, base.sha, settings.worktree_root) as worktree:
        try:
            return _run_meeting_in_worktree(conn, item, deps, material, meeting_meta, worktree,
                                            passes, linter_path=linter_path)
        except LibrarianError as ex:
            ex.at_agent_attempt(passes.count, cost_usd=passes.cost_usd)
            raise


def _meeting_meta(item: dict) -> dict:
    """The drop CLI's hints — the agent's HINTS, never instructions: they bind no placement."""
    client = (item.get("hints") or {}).get("client", {})
    return {k: client.get(k, "") for k in ("title", "meeting_date", "attendees", "source_label")}




def _run_meeting_in_worktree(conn, item, deps, material, meeting_meta, worktree, passes,
                             *, linter_path: str = "") -> Result:
    """`_run_in_worktree`'s meeting sibling: one pass, one corrective pass, then refuse."""
    settings = deps.settings
    corrective, findings, outcome, diagnostics = "", [], None, ""

    for attempt in range(1, MAX_AGENT_ATTEMPTS + 1):
        passes.count = attempt
        try:
            result, findings, outcome = _one_meeting_pass(
                conn, item, deps, material, meeting_meta, worktree, corrective,
                passes=passes, linter_path=linter_path)
        except OutcomeShapeError as ex:
            result, findings, outcome = None, list(ex.findings), None
            agent_module.discard_outcome_file(worktree)
        if result is not None:
            return _stamp_cost(result, passes)

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

    return _stamp_cost(_refuse_meeting(item, findings, outcome, agent_attempts=passes.count,
                                       diagnostics_path=diagnostics), passes)


# Code writes every page in the set; the agent drafts free text as DATA. Its two free-text fields
# still pass through `gate_secrets`/`gate_pii`: code writing the container does not make the
# model's prose trusted.
def _yaml_str(value: str) -> str:
    """One frontmatter scalar, safely quoted — JSON scalars are valid YAML. A bare `f'"{v}"'`
    breaks on a title containing a `"`."""
    return json.dumps(str(value or ""))


def _source_stem(meeting_meta: dict) -> str:
    """Decided by CODE before the agent runs, so the path can be handed to it. No date prefix:
    only the MEETING page's filename carries one."""
    return f"{slugify(meeting_meta.get('title') or 'meeting')}-transcript"


def _meeting_stem(meeting_date: str, title: str) -> str:
    """The meeting page's stem — the one filename in this set that DOES carry a date
    (`YYYY-MM-DD-<slug>`), from the agent's own `meeting_title` and the operator's `--date`."""
    base = f"{meeting_date}-{title}" if meeting_date else title
    return slugify(base)


def _decision_stems(titles: list) -> list:
    """One filesystem-safe stem per decision title; same-slug titles get `-2`, `-3`, ... The
    SUFFIXED stem is registered too: counting bases alone lets `["Pricing", "Pricing",
    "Pricing (2)"]` mint `pricing-2` twice, and `O_EXCL` then raises `FileExistsError`, which is
    not a `LibrarianError` and escapes every handler in this flow."""
    taken: set = set()
    stems = []
    for title in titles:
        base = slugify(title) or "decision"
        stem, n = base, 1
        while stem in taken:
            n += 1
            stem = f"{base}-{n}"
        taken.add(stem)
        stems.append(stem)
    return stems


_SOURCE_SPLIT_LOOKBACK = 30


def _chunk_source_body(lines: list, budget: int) -> list:
    """Greedy, preferring a blank-line boundary within `_SOURCE_SPLIT_LOOKBACK` and never
    breaking inside a fenced code block."""
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
    """The source page(s), verbatim from the archived material — CODE writes this, never the agent:
    a model copying the transcript back out can drop, reorder or normalise a line. THE source-page
    writer for every flow that needs one.

    A body over `MAX_BODY_LINES` is split into cross-linked parts: the filename stem carries the
    `-p<n>` suffix and every part declares a chain identity, `<stem>` then `<stem>#p<n>`. If you
    change that convention, change `index.corpus`, which prefers the declared `id:` over the stem.
    Returns a DRAFT whose server-owned fields `page_policy.stamp_source_fields` overwrites.
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
    """A decision page's DRAFT — frontmatter code owns, body the agent drafted. `created`/`updated`
    are contract-required but NOT server-owned, so code drafts them from the date `_stamp_meeting`
    stamps as `as_of`; `sources/meetings/` pages are exempt, hence `_build_source_parts` omits them.
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
    """The meeting (provenance) page, built entirely by CODE; only "## Notes" is the agent's. A
    links/decisions mismatch is impossible: the link list IS the decision list code just wrote."""
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
    """Create one brand-new page inside `worktree`, or raise `WorktreeError` naming the stage. THE
    write every page-building flow goes through, so both guards live here: `page.is_inside` RESOLVES
    the path (`O_NOFOLLOW` only sees the leaf, so a symlinked directory component would be written
    through), and every `OSError` becomes a NAMED stage rather than an `unexpected` fault.
    """
    if not page_policy.is_inside(worktree, rel_path):
        raise WorktreeError(
            f"refusing to write {rel_path}: it does not resolve inside this capture's own "
            f"checkout — a directory component on that path is a symlink out of the worktree")
    full = os.path.join(worktree, rel_path)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with page_policy.open_for_new(full) as f:
            f.write(text)
    except OSError as ex:
        # The class name and the path only — never `str(ex)`, which can carry a filesystem path
        # this package does not put on the wire.
        raise WorktreeError(
            f"could not write {rel_path} ({ex.__class__.__name__}); nothing was committed") from ex


def _write_meeting_pages(worktree: str, outcome, meeting_meta: dict, material: str, *,
                         source_stem: str, created: str):
    """CODE writes every page in the set. All-or-nothing: every path is checked against the repo's
    existing pages before the first byte is written. Returns the plan, or one veto `Finding`."""
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
        # Chain identity per part path; `_stamp_meeting` stamps it as `id:`.
        "source_ids_by_path": {path: pid for path, (_stem, pid, _text)
                               in zip(source_paths, source_parts, strict=True)},
        "meeting_stem": meeting_stem,
        "decision_stems": decision_stems,
        "decisions_by_path": dict(zip(decision_paths, outcome.decisions, strict=True)),
    }


def _edits_with_resolved_links(outcome, written: dict) -> list:
    """The declared edits, with any `link` naming one of THIS capture's own decisions replaced by
    the stem the worker actually filed that decision under.

    **The agent cannot spell the filename, and must not have to.** It declares a decision's
    `title`; `_decision_stems` slugifies that title into the page's basename, and a wikilink
    resolves by basename — so `[[<the title>]]` resolves to nothing. Without this translation a
    perfectly correct declaration is refused by `edits.validate` as a dead link, which refuses the
    WHOLE set and spends the capture's one corrective retry on a naming convention the agent was
    never shown. Observed: `linking [[Q3 sync — decision 1]], which resolves to no page in the
    graph`, twice, then `failed`.

    Telling the brief to slugify instead was the alternative and it is the weaker one: `slugify`
    strips accents, folds punctuation AND truncates at 60 characters, so for a real title the rule
    is a guess, and a wrong guess costs the same retry. The worker names the page, so the worker
    resolves the name — the same reason it, not the agent, builds the meeting page's own
    `## Decisions` links out of `decision_stems`.

    A `link` that is already a resolvable page name is passed through untouched, so both spellings
    work and an existing page is never shadowed by a decision that merely shares its title:
    lookup is by the capture's declared titles only, and a miss keeps what the agent wrote.
    """
    declared = list(outcome.edits or ())
    if not declared:
        return declared
    stem_by_title = {}
    for decision, stem in zip(outcome.decisions or (), written.get("decision_stems") or (),
                              strict=False):
        title = str(decision.get("title", "")).strip()
        if title:
            stem_by_title.setdefault(title, stem)
    return [{**edit, "link": stem_by_title.get(str(edit.get("link", "")).strip(),
                                               edit.get("link", ""))}
            for edit in declared]


def _one_meeting_pass(conn, item, deps, material, meeting_meta, worktree, corrective, *,
                      passes: "AgentPasses | None" = None, linter_path: str = "") -> tuple:
    """One meeting pass: call the tool-less meeting agent, have CODE write every page of the set,
    stamp, gate. Same return contract as `_one_pass`; the agent's only write is its outcome file."""
    settings = deps.settings
    source_stem = _source_stem(meeting_meta)
    source_page_path = f"{MEETING_SOURCE_PREFIX}{source_stem}.md"
    # Built HERE, never inside a backend — `_one_pass`' own reason: both flows must share one
    # context builder and one fence discipline. Unconditional, unlike the ordinary flow's
    # `wants_gathered` branch: no backend on this flow holds a tool, so `render_gathered`'s
    # no-tools defaults are simply the truth here and there is no second shape to declare.
    # Re-run on the corrective pass, because `_reset_for_retry` resets the tree.
    gathered = agent_module.render_gathered(
        gather.gather(worktree, deps.registry, material,
                      top_k=settings.gather_top_k,
                      excerpt_lines=settings.gather_excerpt_lines))
    try:
        run = deps.agent.run_meeting(worktree=worktree, material=material,
                                     meeting_meta=meeting_meta, registry=deps.registry,
                                     source_page_path=source_page_path,
                                     corrective=corrective, gathered=gathered)
    except AgentError as ex:
        if passes is not None:
            passes.cost_usd += getattr(ex, "run_cost_usd", 0.0)
        raise
    if passes is not None:
        passes.cost_usd += run.cost_usd
    outcome = run.outcome
    agent_module.discard_outcome_file(worktree)
    if outcome is None:
        raise AgentError("the meeting agent produced no usable account of what it did")

    written = _write_meeting_pages(worktree, outcome, meeting_meta, material,
                                   source_stem=source_stem,
                                   created=meeting_meta.get("meeting_date") or deps.as_of())
    if isinstance(written, list):
        return None, written, outcome

    # The identities the account introduces — the same writer the ordinary flow uses, so a meeting
    # whose decisions are about something new lands with that something created beside them.
    # `related` names pages by STEM, which is what a wikilink resolves by: the decision pages
    # this set wrote (slugified, never their titles), or the meeting page when there is none.
    decision_stems = [os.path.basename(path)[:-len(".md")]
                      for path in written.get("decisions_by_path", {})]
    births = identity.write_births(
        worktree, outcome=outcome, base_registry=deps.registry, material=material,
        hints=meeting_meta, today=deps.as_of(),
        registration=schema.registration_from_hints(item.get("hints")),
        approver=str(item.get("submitted_by") or ""),
        related=decision_stems or [written["meeting_stem"]])
    if isinstance(births, list):
        return None, births, outcome

    # `edits_allowed` keeps its `True` default: this flow HAS an edit mechanism now, the same one
    # the fast lane has — declared in the account, performed by `edits.apply_declared` below,
    # judged additive by `gate_body_rewrite` like any other modification.
    ctx = gates.GateContext(
        worktree=worktree,
        entries=gitcmd.diff_entries(worktree),
        added=gitcmd.added_lines(worktree),
        material=material, outcome=outcome, registry=births.registry,
        linter_path=linter_path, gitleaks_bin=settings.gitleaks_bin,
        write_prefixes=MEETING_WRITE_PREFIXES, creatable_types=MEETING_CREATABLE_TYPES,
        extra_folder_types=dict(MEETING_EXTRA_FOLDER_TYPES))
    _declare_births(ctx, births)

    if not ctx.entries:
        raise AgentError("the meeting flow wrote nothing for a transcript it decided to file")

    # Code's own additive edits, from the agent's DECLARATION — `_one_pass`' call, unchanged, on
    # the set's own new pages. `edits.validate` admits the three EDITABLE folders (`page.
    # FOLDER_BY_TYPE`), which is a wider set than this flow's own lane: an edit to `wiki/notes/` or
    # `wiki/concepts/` passes there and is then refused by `gate_zone` as out-of-lane, so
    # `wiki/decisions/` is the one folder a meeting can really edit. The lane is deliberately NOT
    # widened to match — it is the BUILDER's range and `test_gates_unit.py` pins it as such — and
    # the brief tells the agent which pages it may name.
    edited, edit_findings = edits.apply_declared(
        worktree, _edits_with_resolved_links(outcome, written),
        new_pages=ctx.in_lane_new_pages())
    ctx.entries = gitcmd.diff_entries(worktree)
    ctx.added = gitcmd.added_lines(worktree)

    _stamp_meeting(ctx, deps, item, outcome, meeting_meta, written)

    ctx.entries = gitcmd.diff_entries(worktree)
    ctx.added = gitcmd.added_lines(worktree)
    findings = (gates.run_gates(ctx) + edit_findings
                + _cross_check_meeting_outcome(ctx, outcome))
    if not gates.vetoes(findings):
        return (_file_meeting(conn, item, deps, ctx, outcome, findings, worktree,
                              written, edited=edited, births=births),
                [], outcome)
    return None, findings, outcome


def _decision_pages(ctx: gates.GateContext) -> list[str]:
    return sorted(p for p in ctx.in_lane_new_pages() if p.startswith("wiki/decisions/"))


def _source_pages(ctx: gates.GateContext) -> list[str]:
    return sorted(p for p in ctx.in_lane_new_pages() if p.startswith("sources/meetings/"))


def _meeting_pages(ctx: gates.GateContext) -> list[str]:
    return sorted(p for p in ctx.in_lane_new_pages() if p.startswith("wiki/meetings/"))


def _cross_check_meeting_outcome(ctx: gates.GateContext, outcome) -> list:
    """The page-SET's atomicity contract: N >= 1 source pages, exactly one meeting page, N >= 0
    decision pages, nothing else. Thin because CODE authors every page from this same `outcome`.

    The date-bearing body-link convention is deliberately NOT vetoed here: only the meeting page's
    filename carries a date, and a date-bearing name in body prose is style rather than safety, so
    the gardener's `date-bearing-body-link` check flags it over the committed corpus instead."""
    out = []
    new_pages = set(ctx.in_lane_new_pages()) - ctx.born_entity_pages
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
    from its siblings'. `as_of` is the meeting's OWN date, never today's."""
    as_of = meeting_meta.get("meeting_date") or deps.as_of()
    source_pages, meeting_pages, decision_pages = (_source_pages(ctx), _meeting_pages(ctx),
                                                    _decision_pages(ctx))
    ctx.provenance_pages = frozenset(source_pages)
    decisions_by_path = written.get("decisions_by_path", {})

    source_ids_by_path = written.get("source_ids_by_path", {})
    digest = hashlib.sha256((ctx.material or "").encode("utf-8")).hexdigest()
    extracted_at = datetime.datetime.now(datetime.UTC).isoformat()
    for path in source_pages:
        _stamp_one_source(ctx, path, submitted_by=item["submitted_by"], as_of=as_of,
                          digest=digest, extracted_at=extracted_at,
                          page_id=str(source_ids_by_path.get(path) or ""))

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
        # `ctx.registry`, the registry this commit publishes: a decision about an entity born in
        # this same commit resolves exactly like one about an old entity.
        entity_ids, unresolved = gates.resolve_entity_ids(anchoring, ctx.registry)
        if str(anchoring.get("kind", "")).lower() == "entity" and (unresolved or not entity_ids):
            entity_ids = []   # same defence in depth `_stamp` takes for the ordinary flow
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
    """One capture, one commit, one page SET."""
    return (f"feat(meeting): {_subject(outcome.meeting_title)}\n\n"
            f"Filed by the librarian's meeting distiller from capture #{item['id']}: 1 source "
            f"page, 1 meeting page, {n_decisions} decision page(s).\n\n"
            f"Submitted-by: {item['submitted_by']}\n")


def _file_meeting(conn, item, deps, ctx, outcome, findings, worktree, written,
                  *, edited=(), births=None) -> Result:
    """The gates passed over the whole SET: one commit, push, report. `written` carries the
    code-computed source parts and the decision-path-to-anchoring map; `edited` is what
    `edits.apply_declared` actually changed on pages that already existed — the one surface a human
    reads that names a page this capture touched without creating it."""
    meeting_pages, decision_pages = _meeting_pages(ctx), sorted(_decision_pages(ctx))
    # In PART order, not alphabetical — `-p2` sorts before the bare stem's `.md`.
    source_pages = [f"{MEETING_SOURCE_PREFIX}{stem}.md" for stem in written["source_stems"]]
    meeting_page = meeting_pages[0]
    message = _meeting_commit_message(item, outcome, len(decision_pages))
    sha = _commit_and_push(conn, item, deps, ctx, worktree, message, what="this meeting")

    decisions_by_path = written.get("decisions_by_path", {})
    decisions = [{"path": path, "anchoring": (decisions_by_path.get(path) or {}).get("anchoring", {})}
                for path in decision_pages]
    notes = [report.injection_finding(c) for c in _injection_categories(outcome)]
    notes += [f.message for f in findings if f.severity == gates.SEVERITY_NOTE]

    # Touched ids come from `ctx.stamped_by_path` — the server-RESOLVED values, never the agent's
    # declared names. Best-effort: the set is already pushed, so a regeneration fault must not turn
    # a filed meeting into a `failed` capture. It pushes a SECOND commit on top, so the branch tip
    # does NOT name what this capture filed — `result_ref`/`sha` do.
    touched_ids = sorted({eid for path in decision_pages
                          for eid in (ctx.stamped_by_path.get(path, {}).get("entity") or [])})
    if touched_ids:
        try:
            # `views_regenerate.run` writes its own `job_runs` row before re-raising.
            asyncio.run(views_regenerate.run(
                worktree, conn, touched_ids, registry=deps.registry, branch=deps.settings.branch,
                guarded=False,
                job=f"{views_regenerate.JOB_NAME}-on-meeting"))
        except Exception:  # noqa: BLE001 — a best-effort post-step, see the comment above
            log.error("view regeneration failed after meeting %s filed (entities: %s)",
                      item.get("id"), touched_ids, exc_info=True)
    # `result_ref` names the MEETING PAGE, keeping `dedup.Match.page_path`'s `rsplit("@")` contract.
    return Result(
        schema.FILED, f"{meeting_page}@{sha}",
        report.filed_meeting(source_pages=source_pages, meeting_page=meeting_page,
                             decisions=decisions, commit=sha, pages_edited=list(edited),
                             agent_rationale=getattr(outcome, "summary", ""),
                             registry=ctx.registry,
                             entities_born=births.entities if births else [],
                             aliases_added=births.aliases if births else [],
                             entities_updated=births.updates if births else []),
        findings=notes)


def _refuse_meeting(item, findings, outcome, *, agent_attempts: int = 0,
                    diagnostics_path: str = "") -> Result:
    """Both meeting passes vetoed — the same two terminal states as the ordinary flow."""
    return _route_refusal(item, findings, outcome, agent_attempts=agent_attempts,
                          diagnostics_path=diagnostics_path)
