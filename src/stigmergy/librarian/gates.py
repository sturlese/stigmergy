"""The code vetoes: what refuses a commit, and why.

The agent judges — placement, wikilinks, anchoring, duplication, what deserves a page. These do
not judge. They run over the DIFF the agent produced, check one property each, and either pass
or refuse. **Gates check; they never interpret.** A gate that started "understanding" the
material would be the LLM reviewer the design retired: a check an agent performs on its own
output carries no independent ground truth, and the material is untrusted.

One module rather than eight files because they share one input (the diff), one output type
(`Finding`) and one lifecycle, and because "everything code refuses" is worth being able to read
top to bottom in one sitting. Each gate is a function of `(context) -> list[Finding]`;
`run_gates` is the only orchestration.

**Severity is the whole contract.** A `veto` finding refuses the commit. A `note` finding is
recorded on the submission and changes nothing. The agent gets exactly ONE corrective retry
with the veto findings handed back to it; if the second pass still vetoes, the item reaches a
terminal state and nothing is committed.

**And a finding carries TWO texts, because it has two audiences**: `message` is written for a
HUMAN reading a report, `brief` for the AGENT that must repair. See `corrective_brief` for the
measurement that forced the distinction and for what a brief owes that a message does not.

**A veto may also name no repair at all**, and then it must not spend the retry. `repairable=False`
says the agent has no way to act on this finding — it judges part of the diff the agent cannot
write, or it diagnoses the system rather than the draft — and `processing._run_in_worktree`
terminates without the second pass. Burning a pass on something unfixable is strictly worse than
refusing immediately: it costs the run, delays the honest answer, and for the body-rewrite pair it
hands the agent an instruction to repair something it did not do (see `unrepairable`).

The retry resets the worktree (a hard reset plus `git clean -fdq`), which restores the TRACKED
tree and removes untracked files. It does not undo a write into `.git/` itself, and nothing here
should be read as promising that it does. What makes that irrelevant is `agent.confined_write`:
the agent cannot write there at all, because writes are allow-listed to `.md` pages in the three
fast-lane folders **that do not exist yet**, rather than merely confined to the worktree.

**Some of what these gates judge is now CODE's own work.** Since 2026-07-26 the additive edits to
pages that already exist are declared by the agent and performed by `edits.py`, so a modified page in
the diff came from this process rather than from the model. Nothing is exempted for it: what changed
is the single veto surface, and `gate_body_rewrite` in particular is the right place for a check
nobody — not a model, not this code — can talk their way out of.

**"The diff" means git's STRUCTURED output, never its rendering.** `diff_entries` (`--raw -z`) and
per-path blob reads are unforgeable; a unified diff classified by prefix tests is not, because git
prefixes content with a single `+`/`-` and a page line can therefore be spelled to look exactly like
diff metadata. That pattern has now cost three separate gates on this branch — a trailing tab in a
header path, a NUL byte rendering a page binary, and a page line impersonating both a `+++ b/` file
header and a removed `related:` line — so `gate_body_rewrite` reads blobs and `gitcmd.added_lines`
counts hunks. `diff_text` survives for `processing.refused_diff_digest`, where it renders a
human-facing diagnostic and a parsing slip is cosmetic.

**Refusal reasons cross to a human**, so every message here is written to be safe: it names a
path, a line number, a rule id or a category — never the offending value. A secret in an error
message is a secret in a log.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field

import yaml

from stigmergy import text as textutil
from stigmergy.capture import schema as capture_schema
from stigmergy.librarian import gitcmd
from stigmergy.librarian import page as page_policy
from stigmergy.librarian.errors import LibrarianConfigError

log = logging.getLogger(__name__)

# Zones the fast lane may write to at all. Everything else — `sources/`, `views/`, `ops/`,
# `meta/`, `.github/`, `.claude/`, the repo root — is refused by absence rather than by a
# blocklist: a new machine zone added tomorrow is out of bounds by default, which is the safe
# direction for the list to be wrong in.
ALLOWED_WRITE_PREFIXES = tuple(f"{folder}/" for folder in page_policy.FOLDER_BY_TYPE.values())

SEVERITY_VETO = "veto"
SEVERITY_NOTE = "note"

# The fixed set of injection categories a finding may name. The report renders the category and
# never a substring of the planted text — a report that reproduces the payload is a second copy
# of the injection, delivered to a human.
INJECTION_CATEGORIES = ("declare-canonical", "write-outside-lane", "reveal-credentials")


@dataclass(frozen=True)
class Finding:
    """One thing a gate observed, in two texts for two audiences.

    `message` is the diagnosis a HUMAN reads: `report.py` composes the submitter-facing sentence
    around it, and it obeys this module's safety rules — a path, a line number, a rule id, never
    the offending value.

    `brief` is the repair instruction the AGENT reads on its one corrective retry. Optional: a
    message that already reads as an instruction ("`{path}`: dead link: `[[X]]`" — verifiable with
    a glob, repairable by unlinking) needs no second text, and `corrective_brief` falls back to
    `message`. A message that only DIAGNOSES does need one; see `corrective_brief` for what that
    costs when it is missing.

    `repairable` is the question underneath both texts: is there anything the agent could do
    differently that would clear this? Default `True`, because that is what a gate that has not
    thought about it should get — a wasted retry is recoverable, a retry silently taken away from a
    finding the agent COULD have fixed is not. A gate sets it `False` where it knows better; see
    `unrepairable`.

    `values` carries the VERBATIM identifier(s) this finding is about, for a
    reader that needs to compare identity rather than display one — `locator` is `_one_line`'d
    (whitespace-collapsed, control-stripped, clamped to `MAX_BRIEF_NAME_LEN` with an ellipsis) for
    a human or a prompt, and a presentation transform is exactly what silently broke
    `processing._unanchorable`'s identity comparison for a plural anchor, an NFD-vs-NFC spelling,
    or a name over the clamp. Empty by default — only `gate_anchoring`'s unresolved finding
    populates it today.
    """
    gate: str
    code: str
    message: str
    severity: str = SEVERITY_VETO
    locator: str = ""
    brief: str = ""
    repairable: bool = True
    values: tuple[str, ...] = ()


@dataclass
class GateContext:
    """Everything the gates read. Assembled once per attempt by `processing.py`.

    `entries` is the authoritative view of the diff — status AND mode, from one `git diff --raw`
    parse. `changes` is a derived read-only view kept for the gates that only care about status;
    it is a property rather than a second field so the two can never drift out of step, which is
    how a page replaced by a symlink slipped past the body-rewrite gate.
    """
    worktree: str
    entries: list                      # [gitcmd.DiffEntry, ...] from gitcmd.diff_entries
    added: list                        # [(path, lineno, text), ...] from gitcmd.added_lines
    material: str                      # the captured material, from the evidence store
    outcome: object                    # the agent's own account of what it did (agent.Outcome)
    registry: object                   # kernel.registry.Registry
    linter_path: str = ""
    gitleaks_bin: str = "gitleaks"
    stamped: dict = field(default_factory=dict)   # the server-owned values `_stamp` wrote
    findings: list = field(default_factory=list)
    # There is no carve-out here for a page a caller declares it is entitled to rewrite. The
    # invariant is the simple one: an edit to an already-filed page may only GROW its `related:`
    # list or ADD a callout, and every other frontmatter line survives byte for byte. What a gate
    # is allowed to know about its caller is stated explicitly in
    # `write_prefixes`/`creatable_types`/`edits_allowed` below — a gate is TOLD a fact, it never
    # infers one.

    # ── the flow-scoped write whitelist ───────────────────────────────────────────────────
    # `ALLOWED_WRITE_PREFIXES` stays the module-level DEFAULT — every caller that does not set
    # these three fields gets EXACTLY today's global behaviour, byte for byte. The meeting flow
    # (`processing.process_meeting_item`) is the only caller that ever widens them, and it does so
    # on the CONTEXT, not by mutating the module constant or `page.FOLDER_BY_TYPE` globally — the
    # same "a gate is TOLD a fact, it never infers one" posture. This is what keeps an ORDINARY
    # capture claiming `type: meeting` parked rather than filed: its `ctx` is built with the
    # defaults below, so `meeting` is not in `creatable_types` and
    # `sources/meetings/`/`wiki/meetings/` are not in `write_prefixes` for that run.
    # `ALLOWED_WRITE_PREFIXES`'s own comment says a new machine zone should be "out of bounds by
    # default" — this is that default, made per-flow instead of per-process, without weakening it
    # for the flow that never opts in.
    write_prefixes: tuple = ALLOWED_WRITE_PREFIXES
    creatable_types: frozenset = field(default_factory=lambda: frozenset(page_policy.FAST_LANE_TYPES))
    # `{folder (no trailing slash): type}` for types the CTX creates that have no GLOBAL folder in
    # `page.FOLDER_BY_TYPE` (`meeting`, and `source` for the meeting flow's one transcript page —
    # `source` is globally reserved with no folder, and this is the only `sources/` path the
    # meeting flow may write). Consulted BEFORE the global `page.type_for_folder`, never instead
    # of it, so every ordinary fast-lane folder keeps resolving exactly as it always has.
    extra_folder_types: dict = field(default_factory=dict)
    # `{new page path: {"page_type": ..., "anchoring": {...}}}` — the meeting flow's PER-PAGE
    # declarations, one entry per new page it created. Empty for every ordinary capture, which is
    # what keeps `gate_zone`'s per-page type check and `gate_anchoring` reading the single
    # `ctx.outcome.page_type`/`ctx.outcome.anchoring` — see each gate's own docstring for the
    # fallback.
    page_declared: dict = field(default_factory=dict)
    # `{new page path: {field: value}}` — the meeting flow's PER-PAGE server-stamped values
    # (`processing._stamp_meeting` calls `page.stamp_server_fields` once per page, since each
    # decision page's `entity:` differs). `gate_frontmatter` reads this per path when present and
    # falls back to the single `ctx.stamped` dict otherwise — same fallback shape as
    # `page_declared` above, for the same reason.
    stamped_by_path: dict = field(default_factory=dict)
    # Paths where `content_hash` is a LEGITIMATE, server-stamped field rather than a forged
    # one — the meeting flow's one `sources/meetings/` source page (the provenance field group,
    # `page.stamp_source_fields`). Empty for every ordinary capture, so `FORBIDDEN_PAGE_KEYS`'s
    # `content_hash` check is unchanged there.
    provenance_pages: frozenset = field(default_factory=frozenset)
    # `True` (the default) is the fast lane's posture: its additive-edit allowance
    # (`edits.apply_declared`) is a real, designed mechanism, and every gate that reads a
    # status-`M` entry (`gate_body_rewrite` above all) judges it on its merits.
    # `processing.process_meeting_item` is the only caller that ever sets
    # this `False` — the meeting-distiller brief gives that flow's agent no tool that can touch an
    # existing page (`allowed-tools: Write`, confined to `.librarian-outcome.json`) and no mechanism
    # for declaring an edit at all, so a status-`M` entry reaching the gates from THAT flow is never
    # legitimate, additive or not. `gate_zone`'s `meeting-edit-refused` finding is what reads this;
    # see its own comment for why the finding is terminal rather than a corrective brief.
    #
    # NOTE: this FIELD is generic — a fact about the CALLER, true of any no-edit flow — but the
    # finding CODE it drives, `meeting-edit-refused`, names the one caller that exists today. If a
    # second caller ever sets `edits_allowed=False`, the finding code AND its message (which reads
    # as "no edit mechanism exists here", written for the meeting flow specifically) must be
    # revisited then — do not let a second flow silently inherit a name and a sentence that tell
    # its operator the wrong story.
    edits_allowed: bool = True

    # ── the shared base: one place that knows what "in the lane" means ────────────────────
    @property
    def changes(self) -> list[tuple[str, str]]:
        return [(entry.status, entry.path) for entry in self.entries]

    def in_lane(self) -> list:
        """Every entry whose path is under THIS RUN's write prefixes (`write_prefixes` — the
        global fast-lane folders, or the meeting flow's widened set). The gates that judge CONTENT
        all scope themselves through here; the gates that judge PLACEMENT look at everything,
        which is the whole reason an out-of-lane write is caught at all."""
        return [e for e in self.entries if e.path.startswith(self.write_prefixes)]

    def in_lane_writes(self) -> list:
        """In-lane additions and modifications — the surface a secret or a PII pattern can reach.
        A deletion carries no content to scan and is refused by the zone gate on sight."""
        return [e for e in self.in_lane() if e.status in ("A", "M")]

    def new_pages(self) -> list[str]:
        return [e.path for e in self.entries if e.status == "A"]

    def in_lane_new_pages(self) -> list[str]:
        """The pages this capture CREATED inside the lane — the ones the server stamped, and the
        only ones whose whole content is this capture's own doing."""
        return [e.path for e in self.in_lane() if e.status == "A"]

    def in_lane_modified_pages(self) -> list[str]:
        return [e.path for e in self.in_lane() if e.status == "M"]

    def touched_pages(self) -> list[str]:
        return [e.path for e in self.entries]


# ── zone and path: writes stay in the lane ────────────────────────────────────────────────────
# The code `processing._uncreatable_type` routes on, named here for the same reason
# `ANCHORING_UNRESOLVED` and `ORPHAN_CHECK` are: the message is this module's to reword, the code is
# its contract. It is the one zone veto whose destination is `triage` rather than `failed` — the
# page's own folder says which governed type was minted, so nothing has to be judged to route it.
TYPE_NOT_CREATABLE = "type-not-creatable"


def gate_zone(ctx: GateContext) -> list[Finding]:
    """Writes confined to the whitelisted `wiki/` folders; no deletions; existing pages
    edited additively only, and only where the caller grants an edit mechanism at all; only the
    three creatable types minted.

    This is the gate the diff exists for. It runs over what the agent DID, not what it said it
    did, so an agent that reports `decision` and writes into `wiki/entities/` is caught by
    the path.

    **A status-`M` entry is refused outright (`meeting-edit-refused`) when `ctx.edits_allowed`
    is `False`**, before `gate_body_rewrite` ever gets to ask whether the edit was additive — see
    the check's own comment below for why that ordering matters and why the finding is terminal.
    Its position WITHIN this loop is GENUINELY LAST among every per-entry check this gate
    performs — after `unsupported-change`, `outside-lane`, `not-a-regular-file`, `not-a-page` AND
    `unnameable-page` — because it is a catch-all and a catch-all runs last.
    """
    out = []
    for entry in ctx.entries:
        status, path = entry.status, entry.path
        if status == "D":
            out.append(Finding("zone", "deletion",
                               f"deleted {path}: the librarian never deletes a file",
                               locator=path))
            continue
        # Anything that is not a plain add or modify — a typechange (`T`, a page replaced by a
        # symlink), a copy, an unmerged entry, a status git grows tomorrow — is refused BY NAME
        # rather than falling through. `T` used to skip the body-rewrite gate entirely, because
        # that gate only looked for status `M`.
        if status not in ("A", "M"):
            out.append(Finding("zone", "unsupported-change",
                               f"changed {path} in a way the fast lane does not file "
                               f"(git status {status!r}): only adding a page and additively "
                               f"editing one are allowed",
                               locator=path))
            continue
        if not path.startswith(ctx.write_prefixes):
            out.append(Finding("zone", "outside-lane",
                               f"wrote {path}, which is outside the fast lane's folders "
                               f"({', '.join(ctx.write_prefixes)})",
                               locator=path))
            continue
        # A page is an ordinary file. An executable bit, a symlink or a gitlink under a page's
        # name is not a page whatever the path says, and `100755` on a `.md` is a smell with no
        # legitimate cause here.
        if entry.new_mode not in ("", gitcmd.REGULAR_FILE_MODE):
            out.append(Finding("zone", "not-a-regular-file",
                               f"wrote {path} with file mode {entry.new_mode}: a page is an "
                               f"ordinary file ({gitcmd.REGULAR_FILE_MODE})",
                               locator=path))
            continue
        # A path under an allowed prefix is not automatically a page. `wiki/notes/
        # .gitattributes` implies (and declares) `note` and used to pass every check here — and
        # `* -diff` in that file makes every LATER diff in the folder binary, reproducing the
        # blind-diff failure permanently for every capture filed into it.
        basename = path.rsplit("/", 1)[-1]
        if basename.startswith(".") or not basename.endswith(".md"):
            out.append(Finding("zone", "not-a-page",
                               f"wrote {path}, which is not a page: the fast lane writes "
                               f"`.md` files and never a dotfile",
                               locator=path))
            continue
        # A page filename IS the page title — what a wikilink resolves by and what `git log` shows.
        # So a name that cannot be spelled is REFUSED here rather than approximated somewhere
        # upstream: a title sanitizer that quietly dropped non-ASCII put "Reuni n" in a filename, an
        # H1, a `title` field and a commit subject, permanently. Accents pass; a path separator and
        # a control byte do not.
        unnameable = page_policy.unnameable_reason(basename)
        if unnameable:
            out.append(Finding("zone", "unnameable-page",
                               f"wrote {path}, which cannot be filed under that name: "
                               f"{unnameable}",
                               locator=path))
            continue
        # A status-`M` entry from a caller that never grants an edit mechanism at all. The
        # fast lane's additive-edit allowance a few lines and one gate down (`gate_body_rewrite`'s
        # callout/back-link carve-out) is real there — `edits.apply_declared` is a designed
        # mechanism, built and gated on its own merits. It is not designed for the meeting
        # flow: that agent has no tool that can touch an existing page (`allowed-tools: Write`,
        # confined to its own outcome file) and the meeting-distiller brief gives it no way to
        # declare an edit either, so `ctx.edits_allowed=False` there (see `GateContext.
        # edits_allowed`) is a fact about the CALLER, not an inference from this diff's shape. A
        # genuinely additive edit — an appended callout, nothing else — would sail through every
        # rule `gate_body_rewrite` runs; refusing it here, before that gate ever reads it, is what
        # makes "this flow files only new pages" a checked property instead of an absence nobody
        # calls.
        #
        # **Position — the PRINCIPLE:
        # `meeting-edit-refused` is the LAST-RESORT explanation for a status-`M` entry, so it runs
        # GENUINELY LAST among every per-entry check `gate_zone` performs — CATCH-ALL, THEREFORE
        # LAST.** Its meaning is "the only thing wrong with this entry is that it is a
        # modification, and modifications are not permitted in this flow at all." Every more
        # specific diagnosis this gate can make on a status-`M` entry — `outside-lane` (wrote where
        # it may not write at all), `not-a-regular-file` (a mode bit or a symlink where a page
        # should be), `not-a-page` (a dotfile or a non-`.md` path), `unnameable-page` (a filename
        # that cannot be spelled) — names something wrong IN ADDITION TO being a modification, and
        # that extra fact is what the operator needs, so every one of them gets first say.
        # (`unsupported-change` is NOT in this list, on purpose: it fires on `status not in ("A",
        # "M")`, which is mutually exclusive with this check's own `status == "M"` guard — the two
        # can never both apply to the same entry, so no ordering between them was ever reachable
        # and this check's old position never shadowed it.) It used to sit right after `deletion`
        # — an artifact of where the block was first pasted, never a decision — and from there it
        # shadowed all four: a MODIFIED path outside the lane, carrying an anomalous mode bit,
        # living under a dotfile path, or unspellable by name was reported as `meeting-edit-refused`
        # ("no edit mechanism exists here") instead of the stronger, more specific signal.
        # `not-a-page`'s shadow was not theoretical: that
        # check's own comment records a real precedent — `wiki/notes/.gitattributes`, a
        # legacy in-lane dotfile that "used to pass every check here" — so a pre-existing tracked
        # dotfile, modified, hits this refusal today, exactly as a `100755` mode bit does.
        # `not-a-regular-file` still matters most of the four to get right: a symlink is how
        # unconfined reads reach the App private key, so "something appeared that is not a regular
        # file" outranks "an edit happened" every time.
        #
        # **This is the END of the per-entry chain for a status-`M` entry, not a chosen stopping
        # point.** Nothing in `gate_zone` follows `unnameable-page` for such an entry:
        # `_check_created_type` runs only `if status == "A"`, a few lines down, so a status-`M`
        # entry that clears every check above has no further check left to defer to. The position
        # is therefore FORCED by the principle, not selected among alternatives.
        #
        # Composed with `_refuse_meeting`'s `f.repairable` filter, shadowing here would not be
        # merely a wrong label: `meeting-edit-refused` is `repairable=False`, so it would make an
        # out-of-lane MODIFICATION coinciding with a declared injection category unreachable
        # through `rejected_steering` — the same capture as an out-of-lane ADDITION still routed
        # there, only the modification lost coverage. Running this check genuinely last means every
        # status-`M` entry that reaches it has ALREADY cleared every more specific check `gate_zone`
        # runs, so the composed steering path (and any future one like it) is restored by
        # construction rather than by enumerating cases. A future check added to this loop has an
        # obvious answer to "where does mine go?": before this one, unless it is ALSO a catch-all
        # for something status-`M`-shaped, in which case the two catch-alls' relative order is a
        # new question, not one this comment answers.
        #
        # This position is NOT about `gate_body_rewrite` — that is a different gate function, later
        # in `ALL_GATES`, and any position in THIS loop already ran before it; it is about
        # precedence AMONG `gate_zone`'s own findings for the same entry.
        #
        # **Why `repairable=False`, and the RIGHT reason for it.** It is NOT that the retry "has
        # no way to make the diff stop containing a status-`M` entry it never wrote":
        # `processing._reset_for_retry` runs `reset --hard` + `clean -fdq` before the corrective
        # pass, so a transient external write that produced this `M` WOULD clear on pass 2 with no
        # agent action at all — the same calculus that keeps `deletion` (a few lines up) repairable.
        # The true rationale is EVIDENCE PRESERVATION plus FAIL-CLOSED: `processing.
        # preserve_refused_diff` runs only on the terminal, blocked path — never before a retry — so
        # a repairable finding here would let that reset silently erase the only trace of an
        # unexplained write into the worktree (a possible compromise indicator) and then commit over
        # it if pass 2 happens to come back clean. For a modification the meeting agent holds no
        # tool to have produced, fail-closed WHILE KEEPING THE EVIDENCE is the right posture; a
        # silent second pass that might destroy it is not. (A corrective brief would also be
        # ceremony aimed at something that cannot act — the agent has no tool that writes to
        # `path` — but that alone argues for a repairable finding whose brief nobody reads, not for
        # `repairable=False`; the evidence argument is what actually earns the terminal refusal.)
        #
        # **Parity with `deletion` / `unsupported-change` / `not-a-regular-file` (all left
        # repairable — see `unrepairable`'s own docstring) is an OPEN QUESTION.** Those three also
        # have no known producer; whether their own reset-before-retry destroys evidence of
        # interference the same way this one's does has not been investigated, so their terminal
        # behavior stays as it is rather than being changed on an unexamined analogy.
        #
        # This finding is a WORKER SELF-CHECK: its `message` is written for the operator reading the
        # refusal report and for the log, not for the agent, and `unrepairable()` is what turns that
        # into a terminal refusal — diagnostics preserved — after one pass rather than a silent
        # second one that could destroy them.
        if status == "M" and not ctx.edits_allowed:
            # The message is the operator's WHOLE briefing (it reaches them
            # verbatim through `report.failed_system`'s `reason`), so it names the cause-space —
            # a worker defect or worktree interference, never the submitted material — and that
            # the refused diff was kept (`processing.preserve_refused_diff` runs on this path;
            # `diagnostics_path` names it on the `Result`). Path only, never page content, same
            # rule as every other finding here.
            out.append(Finding("zone", "meeting-edit-refused",
                               f"modified {path}: no edit mechanism exists here — a worker "
                               f"defect or worktree interference, not the material; the refused "
                               f"diff is preserved for inspection",
                               locator=path, repairable=False))
            continue
        if status == "A":
            out.extend(_check_created_type(ctx, path))
    return out


def _check_created_type(ctx: GateContext, path: str) -> list[Finding]:
    """The type half of the zone gate for one created page.

    Two things, and both are REQUIRED rather than opt-in. The folder decides the type — it is
    what the agent actually did — and `page.ensure_creatable` is the write guard that answers
    whether the fast lane may mint one at all, asked over the real diff no matter what the agent
    reported. A missing `page_type` used to skip the comparison entirely, which in a milestone
    that insists "silence is not an outcome" is the wrong direction to be wrong in.

    **`unknown-folder` and `type-not-creatable` are both DEFENSIVE**, and honestly so: they can only
    fire if the two derived views of `page.PAGE_TYPES` disagree. `ALLOWED_WRITE_PREFIXES` and
    `type_for_folder` are both built from `FOLDER_BY_TYPE`, which holds only types that carry a
    folder, and `classify_page_type` makes exactly those creatable — so today `ensure_creatable`
    cannot raise for a type `type_for_folder` just returned. Kept because that table is meant to
    grow (a fourth fast-lane type, a governed type that gains a folder), and a guard that costs
    nothing is how the growth stays safe. A capture the agent JUDGES to be a governed type does not
    come through here at all: it parks itself (`processing._triage`), and the
    fast lane's folders are not where an `entity` page would land even if it tried — that write is
    `outside-lane`.
    """
    implied = _type_for_path(ctx, path)
    if not implied:
        # Under an allowed prefix but not in one of the known folders: only reachable if the
        # prefix table and the folder table ever disagree, and a silent pass here would be a
        # page of no known type.
        return [Finding("zone", "unknown-folder",
                        f"created {path}, whose folder implies no page type the fast lane files",
                        locator=path)]
    # **Flow-scoped**: creatability is asked of `ctx.creatable_types`, never of the global
    # `page.ensure_creatable` directly — see `GateContext.creatable_types`'s own comment. An
    # ordinary capture's `ctx` carries the unwidened default.
    if implied not in ctx.creatable_types:
        reason = page_policy.classify_page_type(implied).reason or "it is not a fast-lane type"
        return [Finding("zone", TYPE_NOT_CREATABLE,
                        f"{path}: the fast lane cannot create a {implied!r} page: {reason}",
                        locator=path)]

    # The declared type comes off THIS PAGE's own declaration when the caller supplied one
    # (`ctx.page_declared`, the meeting flow's per-page shape); every ordinary capture has no
    # entry there and falls back to the single `ctx.outcome.page_type`.
    declared = str((ctx.page_declared.get(path) or {}).get("page_type", "")
                   or getattr(ctx.outcome, "page_type", "") or "").lower()
    if not declared:
        return [Finding("zone", "undeclared-type",
                        f"created {path} without declaring a page type: every filed page states "
                        f"its own type, so the folder it landed in can be checked against it",
                        locator=path)]
    if declared != implied:
        return [Finding("zone", "type-folder-mismatch",
                        f"filed {path} as a {declared!r} page, but that folder holds "
                        f"{implied!r} pages",
                        locator=path)]
    return []


def _type_for_path(ctx: GateContext, path: str) -> str:
    """The type a created page's folder implies, THIS RUN's extra folders consulted first.

    `ctx.extra_folder_types` (the meeting flow's `sources/meetings` -> `source`,
    `wiki/meetings` -> `meeting`) is checked before the global `page.type_for_folder`, never
    instead of it — every folder in `page.FOLDER_BY_TYPE` keeps resolving exactly as it always
    has, for every flow.
    """
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    if folder in ctx.extra_folder_types:
        return ctx.extra_folder_types[folder]
    return page_policy.type_for_folder(path)


def _base_text(worktree: str, path: str) -> str | None:
    """One modified page as it stands in the commit the worktree branches from, or `None`.

    `git show HEAD:<path>` in the worktree, which is the base commit by construction
    (`ephemeral_worktree` checks out `--detach` at it, and the corrective retry's `reset --hard
    HEAD` does not move it). Structured output, not a rendering — nothing in a page's content can
    change what this returns.
    """
    try:
        proc = gitcmd.run("show", f"HEAD:{path}", cwd=worktree, check=False)
    except (UnicodeDecodeError, ValueError):
        return None         # a base blob that is not text: `unreadable-edit`, not an exception
    return proc.stdout if proc.returncode == 0 else None


def _appended_callout_only(base_body: list[str], new_body: list[str]) -> bool:
    """Is `new_body` `base_body` plus nothing but appended callout lines?

    Two conditions, both mechanical: every base line survives in place (a prefix, byte for byte),
    and every line beyond it is blank or a `>` quote line — which is the only shape
    `page.with_callout` can emit (`\\n\\n> [!NOTE] Overlaps with [[X]]\\n> <note>`), repeated once
    per declared callout. Anything else in the tail — a heading, a sentence, a link line — is not
    a callout and is not admitted, whatever produced it.
    """
    if new_body[:len(base_body)] != base_body:
        return False
    return all(not line.strip() or line.startswith(">") for line in new_body[len(base_body):])


def _related_growth_ok(base_text: str, new_text: str) -> bool:
    """Did the page's `related:` field change only by GAINING links?

    The one field an additive edit is allowed to rewrite, and the reason it needs a semantic rather
    than a byte rule: every page in the knowledge repo spells `related:` as a flow list
    (`related: ["[[A]]"]`), so a reciprocal link cannot be added without replacing that one line.
    A byte rule therefore cannot express "a YAML list gained an item", which is exactly the edit the
    page contract requires on both sides of an overlap.

    So it is a proof, not an exemption: identical bytes need nothing proved; otherwise the link set
    must be a STRICT superset of what it was. A dropped link, a reorder, a same-length swap, a
    reformat, a value neither side can parse, and a dropped field are all refused — and so is a
    duplicate `related:` key whose second declaration disappears, because that second block is part
    of the frontmatter compared byte-for-byte by the caller.
    """
    base_block, base_links = page_policy.related_declaration(base_text)
    new_block, new_links = page_policy.related_declaration(new_text)
    if base_block == new_block:
        return True
    if base_links is None or new_links is None:
        return False        # an unprovable before/after must never read as "nothing was lost"
    return set(base_links) < set(new_links)




def gate_body_rewrite(ctx: GateContext) -> list[Finding]:
    """An existing page may gain `related:` links and callouts — never a rewritten body.

    **Proven against the base BLOB, not against a rendered diff.** This used to classify diff lines
    with prefix tests, and that is defeatable from page content: git prefixes every content line
    with a single `-`, so a deleted body line beginning with `--` renders as `---…` and was skipped
    as a header, and any removed line merely *shaped* like `related: [...]` was handed to a superset
    proof that read the frontmatter's own list — position-blind, so a body line spelled that way was
    admitted whenever the real field happened to be a superset. Three human-authored lines were
    deleted with this gate silent. A gate whose input is attacker-influenced text parsed by prefix
    matching will keep failing that way, so the input changed: read what the page WAS out of the
    object database, read what it IS off disk, and compare directly.

    Five rules, in order:

    0. the NEW frontmatter must parse as YAML at all, and to a mapping —
       checked BEFORE any of the span-based comparisons below trust it (`unparseable`, see below);
    1. the base version must be readable at all — a modification whose "before" cannot be
       established is refused (`unreadable-edit`) rather than assumed additive;
    2. the body must be the base body plus nothing but appended callout lines;
    3. every frontmatter line must survive byte for byte, except the single top-level `related:`
       block;
    4. and that block must have GAINED links (`_related_growth_ok`).

    This gate guards CODE's work rather than the agent's: the agent cannot touch an existing page
    at all, and the additive edits it declares are performed by `edits.py`. Nothing here is
    exempted for that — the callout append and the one `related:`
    replacement are exactly what rules 2 and 4 admit, and they are admitted because they are
    provable, not because of who wrote them.

    **Which is why every finding here is `repairable=False`.** This gate reads status-`M`
    entries only, and the agent cannot produce one on the FAST LANE: a modified page in that diff
    came from `edits.apply_declared` or from nothing. So the message — *"you
    rewrote existing content in X"* — describes work the agent did not do and cannot reach, and
    handing it back as a corrective brief instructs it to repair somebody else's action. The
    reachable cause is a target page whose `related:` block cannot be proved to have grown (an
    unparseable value, a duplicated key), which is a fact about the graph and about this module's
    applier, not about the draft. `failed` after ONE pass is the honest answer, and the preserved
    diff is where the diagnosis lives.

    **Rule 3 has no carve-out at all**: every frontmatter line but the one `related:` block
    survives byte for byte, with no caller entitled to declare an exception.
    """
    out = []
    for path in sorted({e.path for e in ctx.entries if e.status == "M"}):
        base_text = _base_text(ctx.worktree, path)
        if base_text is None:
            out.append(Finding("zone", "unreadable-edit",
                               f"the version of {path} this capture started from could not be read "
                               f"out of the base commit, so an additive edit cannot be "
                               f"distinguished from a rewrite; refusing rather than assuming",
                               locator=path, repairable=False))
            continue
        try:
            with open(os.path.join(ctx.worktree, path), encoding="utf-8") as f:
                new_text = f.read()
        except (OSError, UnicodeDecodeError):
            out.append(Finding("zone", "unreadable-edit",
                               f"the modified page {path} could not be read back as text, so what "
                               f"changed in it cannot be established; refusing rather than "
                               f"assuming",
                               locator=path, repairable=False))
            continue

        # Confirm the NEW frontmatter still parses as YAML
        # BEFORE any of the span-based comparisons below trust it. Those comparisons (the
        # byte-for-byte frontmatter check and `_related_growth_ok`'s `related:` superset proof)
        # operate on LINES, never on a parsed structure — so an indented line placed immediately
        # after a flow-style `related:` list is silently absorbed as a continuation of that list
        # rather than read as a new field, and a page can be committed whose frontmatter a real
        # YAML parser rejects outright, with every check below reporting nothing wrong (reproduced
        # with pure-ASCII content — this is not about confusable spellings).
        # `gate_frontmatter`'s own `unparseable` finding does not catch it because that gate
        # scopes itself to NEW pages only (`ctx.in_lane_new_pages()`), and a modified page never
        # reaches it.
        #
        # Modeled directly on `gate_frontmatter`'s `unparseable` finding (same two branches: a
        # parse error, or a value that parses to something other than a mapping) — the same
        # "refuse what you cannot represent faithfully" pattern, applied here for the first time
        # to an EDIT rather than a creation.
        #
        # **`repairable=False`, unconditionally.** The fast lane is the only caller, and a
        # modified page in its diff comes only from `edits.apply_declared` — work the agent
        # cannot see or touch — so `repairable=True` would burn its ONE corrective retry on a page
        # it cannot write.
        new_front_block, _ = page_policy.split_frontmatter(new_text)
        try:
            parsed_front = yaml.safe_load(new_front_block) if new_front_block.strip() else {}
            if parsed_front is not None and not isinstance(parsed_front, dict):
                raise yaml.YAMLError("frontmatter does not parse to a mapping")
        except yaml.YAMLError:
            message = (f"{path}: the frontmatter this edit would commit is not valid YAML, so "
                      f"what this page would actually declare cannot be established — refusing "
                      f"before comparing it against the base version")
            out.append(Finding("zone", "unparseable", message, locator=path, repairable=False))
            continue

        if not _appended_callout_only(page_policy.body_lines(base_text),
                                      page_policy.body_lines(new_text)):
            out.append(Finding("zone", "body-rewrite",
                               f"rewrote existing content in {path}: edits to a page that already "
                               f"exists may only ADD a back-link or a callout",
                               locator=path, repairable=False))
            continue

        base_front, new_front = (page_policy.frontmatter_lines(base_text),
                                 page_policy.frontmatter_lines(new_text))
        base_block, _ = page_policy.related_declaration(base_text)
        new_block, _ = page_policy.related_declaration(new_text)
        base_rest = _without(base_front, base_block)
        new_rest = _without(new_front, new_block)

        if base_rest != new_rest:
            out.append(Finding("zone", "body-rewrite",
                               f"rewrote existing frontmatter in {path}: edits to a page that "
                               f"already exists may only ADD a back-link or a callout",
                               locator=path, repairable=False))
            continue
        if not _related_growth_ok(base_text, new_text):
            out.append(Finding("zone", "body-rewrite",
                               f"changed the `related:` field of {path} without adding to it: an "
                               f"edit to a page that already exists may only GROW its link list",
                               locator=path, repairable=False))
    return out


def _without(lines: list[str], block: list[str]) -> list[str]:
    """`lines` with the first contiguous occurrence of `block` removed. `block` is the top-level
    `related:` span `related_declaration` located in the very same list, so this is a deletion by
    position expressed as one — and an empty block removes nothing."""
    if not block:
        return list(lines)
    for index in range(len(lines) - len(block) + 1):
        if lines[index:index + len(block)] == block:
            return lines[:index] + lines[index + len(block):]
    return list(lines)


# ── a page is text: the precondition every content gate depends on ────────────────────────────
def gate_binary_page(ctx: GateContext) -> list[Finding]:
    """Every page this capture wrote must be readable UTF-8 text with no NUL bytes.

    This runs BEFORE the secrets gate, and it is the structural half of the fix for a defect that
    silently disabled four gates at once. `git diff` emits no content lines for a blob it
    considers binary, and one NUL byte is enough to make it decide that — so a page carrying a
    credential produced an empty added-lines list, which the secrets gate, the PII gate, the
    body-rewrite gate and the additive half of the trace gate each read as "nothing to object to".
    `--text` on the diff invocations fixes the rendering; this gate removes the class of problem,
    because a page that is not text has no legitimate reason to exist in `wiki/` at all.

    The message names the PATH only — never a byte offset into content we have just decided we
    cannot safely characterize.
    """
    out = []
    for entry in ctx.in_lane_writes():
        full = os.path.join(ctx.worktree, entry.path)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            out.append(Finding("binary", "unreadable-page",
                               f"{entry.path} could not be read back after it was written",
                               locator=entry.path))
            continue
        if b"\x00" in data:
            out.append(Finding("binary", "binary-page",
                               f"{entry.path} contains a NUL byte: a page is UTF-8 text, and a "
                               f"page git treats as binary cannot be gate-checked at all",
                               locator=entry.path))
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            out.append(Finding("binary", "binary-page",
                               f"{entry.path} is not valid UTF-8: a page is UTF-8 text, and a "
                               f"page git treats as binary cannot be gate-checked at all",
                               locator=entry.path))
    return out


# ── secrets: gitleaks, the independent ground truth ───────────────────────────────────────────
def ensure_scanner(gitleaks_bin: str) -> None:
    """Startup check. A missing scanner is a CONFIG error, not a per-item failure: without it
    the secrets gate would silently pass everything, which is the one way this gate must never
    fail. Loud, once, before anything is claimed."""
    try:
        proc = subprocess.run([gitleaks_bin, "version"], capture_output=True, text=True)
    except (OSError, ValueError) as ex:
        raise LibrarianConfigError(
            f"the secret scanner {gitleaks_bin!r} is not runnable ({ex.__class__.__name__}) — "
            f"install gitleaks or set $STIGMERGY_GITLEAKS_BIN. The librarian will not run without "
            f"it: a secrets gate that silently passes is worse than no gate") from ex
    if proc.returncode != 0:
        raise LibrarianConfigError(
            f"the secret scanner {gitleaks_bin!r} exited {proc.returncode} on `version`")


def _gitleaks_dir(scratch: str, gitleaks_bin: str) -> list[dict]:
    """THE shared base: run gitleaks over one directory and return its raw hits.

    `--redact` on purpose: the finding carries the rule id and the line, never the matched
    value. `--exit-code 0` because a hit is a normal outcome here, not a failure to run — the
    findings are read from the JSON report, so a non-zero exit would be indistinguishable from
    the binary crashing.
    """
    proc = subprocess.run(
        [gitleaks_bin, "dir", scratch, "--no-banner", "--redact", "--exit-code", "0",
         "--report-format", "json", "--report-path", "-"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise LibrarianConfigError(
            f"the secret scanner failed to run (rc={proc.returncode}); refusing to file without "
            f"a working secrets gate")
    try:
        return json.loads(proc.stdout or "[]") or []
    except json.JSONDecodeError:
        raise LibrarianConfigError("the secret scanner returned unparseable output") from None


# gitleaks matches WITHIN a line, so a credential with a newline inside it matches no rule at all:
# `ghp_<16 chars>\n<20 chars>` is not `ghp_[0-9a-zA-Z]{36}` on either line. One newline was the
# whole bypass, and it is not an exotic one — text extraction hard-wraps a long token at a layout
# boundary, so a credential inside a dropped PDF or DOCX arrives already split, and a capture's
# material is copied VERBATIM into a committed `sources/` page. Every surface is therefore scanned
# twice: as written, and with adjacent lines rejoined, both in one gitleaks run.
#
# PAIRS of adjacent lines, never the whole document glued into one: a token broken across one
# break is the real failure mode, and handing entropy rules a kilometre of text no human wrote is
# how this gate would start bouncing someone's real work. The benign twins in
# `test_gates_unit.py::TestSecretsAcrossALineBreak` are what keep that honest.
_REJOINED_SUBDIR = ".stigmergy-rejoined"


def _rejoined(text: str) -> str:
    lines = (text or "").split("\n")
    return "\n".join(a.rstrip() + b.lstrip() for a, b in zip(lines, lines[1:], strict=False))


def _write_rejoined(scratch: str, rel: str, text: str) -> None:
    destination = os.path.join(scratch, _REJOINED_SUBDIR, rel)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as f:
        f.write(_rejoined(text))


def _hit_surface(scratch: str, hit) -> tuple[str, bool]:
    """One hit's path relative to the scanned surface, and whether it came from a rejoined copy.

    gitleaks reports `File` as the path it was handed — an absolute one inside the scratch tree —
    so this is also what keeps the scratch directory out of a message a person reads.
    """
    relative = os.path.relpath(str(hit.get("File", "")), scratch)
    parts = relative.split(os.sep)
    if parts[0] == _REJOINED_SUBDIR:
        return os.sep.join(parts[1:]), True
    return relative, False


def _secret_findings(scratch: str, hits, label_for) -> list[Finding]:
    # A secret sitting on ONE line is found by both copies. The real copy's finding is strictly
    # better — it carries the author's own line number — so the rejoined copy only ever speaks
    # about a (page, rule) the real one had nothing to say about, which is exactly the case it
    # exists for.
    on_one_line = {(_hit_surface(scratch, h)[0], h.get("RuleID", "unknown"))
                   for h in hits if not _hit_surface(scratch, h)[1]}
    out = []
    for hit in hits:
        relative, rejoined = _hit_surface(scratch, hit)
        label, rule = label_for(relative), hit.get("RuleID", "unknown")
        if rejoined and (relative, rule) in on_one_line:
            continue
        # `values` carries (line, rule) STRUCTURALLY, because `report.py` needs both and the
        # locator is a presentation transform — this dataclass's own docstring records what
        # parsing one back out already cost once. An empty line means "this was only visible once
        # adjacent lines were rejoined", which is a fact about the finding, not a missing value.
        if rejoined:
            out.append(Finding("secrets", "secret",
                              f"a likely secret was matched in {label}, split across a line "
                              f"break (rule: {rule})", locator=label, values=("", rule)))
        else:
            line = str(hit.get("StartLine", "?"))
            out.append(Finding("secrets", "secret",
                              f"a likely secret was matched near line {line} of {label} "
                              f"(rule: {rule})", locator=f"{label}:{line}", values=(line, rule)))
    return out


def scan_secrets(text: str, *, gitleaks_bin: str, label: str) -> list[Finding]:
    """Run gitleaks over one blob of text. `label` names the surface for the refusal message."""
    with tempfile.TemporaryDirectory(prefix="stigmergy-gitleaks-") as scratch:
        # A file, not stdin: `gitleaks stdin` reports no line numbers, and the whole point of
        # the message is telling a person WHERE to look in their own material.
        with open(os.path.join(scratch, "capture.md"), "w", encoding="utf-8") as f:
            f.write(text or "")
        _write_rejoined(scratch, "capture.md", text or "")
        hits = _gitleaks_dir(scratch, gitleaks_bin)
        return _secret_findings(scratch, hits, lambda _rel: label)


def scan_worktree_files(worktree: str, rel_paths, *, gitleaks_bin: str) -> list[Finding]:
    """gitleaks over COPIES OF THE FILES THEMSELVES, on disk, not over a rendered diff.

    The point is independence: whatever git decided to show in a diff, these are the bytes that
    would be committed. The paths are reproduced inside a scratch directory so a hit's `File`
    maps straight back to the real page and the locator a person reads is the page they wrote.
    """
    rel_paths = [p for p in rel_paths if p]
    if not rel_paths:
        return []
    with tempfile.TemporaryDirectory(prefix="stigmergy-gitleaks-disk-") as scratch:
        for rel in rel_paths:
            destination = os.path.join(scratch, rel)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            try:
                shutil.copyfile(os.path.join(worktree, rel), destination)
            except OSError:
                continue        # `gate_binary_page` already vetoed anything unreadable
            # The copy above stays byte-identical on purpose — "these are the bytes that would be
            # committed". The rejoined variant beside it is a text view, and anything that will
            # not decode has already been vetoed as binary.
            try:
                with open(destination, encoding="utf-8") as f:
                    _write_rejoined(scratch, rel, f.read())
            except (OSError, UnicodeDecodeError):
                continue
        hits = _gitleaks_dir(scratch, gitleaks_bin)
        return _secret_findings(scratch, hits, lambda rel: rel or "the drafted page")


def gate_secrets(ctx: GateContext) -> list[Finding]:
    """gitleaks over what reached the page — from two independent surfaces.

    The material is scanned separately and EARLIER (`processing.py`, before the agent runs) —
    a capture carrying a secret bounces whole, whether or not the agent copied it onto the page,
    and burning an agent run on it first would be waste. This is the veto half.

    **Two surfaces, because one of them can be made to show nothing.**

    - *New pages, on disk.* A created page is entirely this capture's doing, so the whole file is
      fair to scan, and scanning the bytes rather than a diff means no rendering decision of
      git's can hide anything.
    - *Added lines, from the diff.* The right unit for an edit to a page that ALREADY existed: a
      secret that was already in the repo is not this capture's doing, and refusing someone's
      work for a pre-existing condition is the false positive this gate must not have.

    **An empty added-lines list is a VETO, not a pass**, whenever the diff claims an in-lane
    write. It used to return `[]` — so any condition that emptied the diff (one NUL byte; a
    `.gitattributes` carrying `* -diff`) turned this gate off silently. Refusing unscanned is the
    only safe direction: `ensure_scanner` already says a secrets gate that silently passes is
    worse than no gate, and this was the path that reached it.

    That veto is `repairable=False`, and it is the clearest case of the class: it says nothing
    about the draft at all. It is this gate reporting that it could not run over an EDIT to a page
    the agent cannot write, for a reason (git's rendering of a diff) the agent has no access to.
    There is no sentence to hand back, so no pass is spent looking for one.
    """
    new_pages = ctx.in_lane_new_pages()
    out = list(scan_worktree_files(ctx.worktree, new_pages, gitleaks_bin=ctx.gitleaks_bin))

    # Added lines of pages that ALREADY existed. New pages are deliberately excluded here: the
    # on-disk pass above already covered every byte of them, with a locator naming the real page
    # instead of "the drafted page", so scanning them twice would only report each secret twice.
    edited = set(ctx.in_lane_modified_pages())
    added = "\n".join(text for path, _, text in ctx.added if path in edited)
    if added.strip():
        out += scan_secrets(added, gitleaks_bin=ctx.gitleaks_bin, label="the drafted page")
    elif edited:
        paths = ", ".join(sorted(edited))
        out.append(Finding("secrets", "unscanned-diff",
                           f"the diff produced no readable added lines for {paths}; refusing "
                           f"rather than passing unscanned",
                           repairable=False))
    return out


# ── PII v0: four high-value patterns, deliberately short ──────────────────────────────────────
# Emails and personal names do NOT bounce: they are the normal tissue of a company brain and
# `submitted_by` is literally an email. Presidio is out of v0 — in a brain whose org chart is
# pages about people, name-level detection would refuse legitimate work constantly, and a gate
# that cries wolf is a gate people route around.
_PII_PATTERNS = (
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("an IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b")),
    ("a DNI/NIE number", re.compile(r"\b(?:[0-9]{8}[A-HJ-NP-TV-Z]|[XYZ][0-9]{7}[A-HJ-NP-TV-Z])\b")),
)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    """Luhn checksum. A 16-digit number that FAILS it is not a card — an order id, a phone
    number, a hash prefix — and must not bounce someone's work (the benign twin of this gate)."""
    total, alternate = 0, False
    for char in reversed(digits):
        value = int(char)
        if alternate:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alternate = not alternate
    return total % 10 == 0


def scan_pii(lines) -> list[Finding]:
    """The four patterns over `[(path, lineno, text), ...]`. Names the KIND and a locator,
    never the value."""
    out = []
    for path, lineno, text in lines:
        for label, pattern in _PII_PATTERNS:
            if pattern.search(text):
                out.append(Finding("pii", "pii",
                                   f"what looks like {label} near line {lineno}",
                                   locator=f"{path}:{lineno}"))
        for match in _CARD_RE.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if 13 <= len(digits) <= 19 and _luhn(digits):
                out.append(Finding("pii", "pii",
                                   f"what looks like a card number near line {lineno}",
                                   locator=f"{path}:{lineno}"))
    return out


def gate_pii(ctx: GateContext) -> list[Finding]:
    """The four patterns over the diff's added lines — plus the whole of every NEW page on disk.

    Same two surfaces and the same empty-input veto as `gate_secrets`, for the same reason: this
    gate read `ctx.added` and nothing else, so every condition that emptied the diff turned it
    off. The patterns are pure functions of text, so the on-disk half needs no subprocess.
    """
    out = []
    for path in ctx.in_lane_new_pages():
        try:
            with open(os.path.join(ctx.worktree, path), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue            # `gate_binary_page` already vetoed it
        out += scan_pii([(path, n, text) for n, text in enumerate(lines, start=1)])

    edited = set(ctx.in_lane_modified_pages())
    added = [(path, n, text) for path, n, text in ctx.added if path in edited]
    if added:
        out += scan_pii(added)
    elif edited:
        paths = ", ".join(sorted(edited))
        out.append(Finding("pii", "unscanned-diff",
                           f"the diff produced no readable added lines for {paths}; refusing "
                           f"rather than passing unscanned",
                           repairable=False))
    return _dedupe(out)


def _dedupe(findings) -> list[Finding]:
    """Drop exact duplicates. The two scanning surfaces overlap by design (a new page is read
    both as added lines and as a file), and a person reading a refusal should see one line per
    real problem, not one per surface that noticed it."""
    seen, out = set(), []
    for finding in findings:
        key = (finding.gate, finding.code, finding.message)
        if key not in seen:
            seen.add(key)
            out.append(finding)
    return out


# Two of the linter's own check ids, named here rather than matched on the message, because the
# message is the linter's to reword and the check id is its contract. `ORPHAN_CHECK` is "no inbound
# links from any content page" (dropped for a page just born — see `gate_contract`);
# `DEAD_LINKS_CHECK` is "this page links a page that does not exist". `processing._unanchorable`
# used to admit it alongside an anchoring veto as the same news in two vocabularies; that
# implication held only under a wikilink-scanning mechanism that no longer exists (see that
# function's docstring). Kept here as the linter's own check id — still a fact worth naming in a
# test or a message — with no privileged reader left.
ORPHAN_CHECK = "orphans"
DEAD_LINKS_CHECK = "dead_links"

# `stigmergy_lint.py`'s own message shape for a dead-link finding: `"dead link: [[{target}]]"`,
# prefixed here with `"{file}: "` (see `gate_contract` below). Parsed rather than carried as a
# second field on `Finding` because the linter is the source of truth for its own wording — this
# regex is the one place that wording is read back apart from a human.
_DEAD_LINK_TARGET_RE = re.compile(r"dead link: \[\[(.+)\]\]\s*$")


def dead_link_target(finding: "Finding") -> str:
    """The wikilink TARGET a `("contract", "dead_links")` finding names — as opposed to
    `finding.locator`, which is the FILE the dead link lives on, not what it points at. `""` when
    the message does not match the linter's own shape (defensive; every real finding does).

    Findings cycle 1, group 4.2: `processing._unanchorable` needs this to decide whether a dead
    link co-occurring with an anchoring veto names the SAME unresolved entity (the agent wrote
    the wikilink it also declared, per the knowledge repo's `SKILL.md`) or something unrelated —
    only the former is still "the anchoring veto is the whole story".
    """
    m = _DEAD_LINK_TARGET_RE.search(finding.message or "")
    return m.group(1).strip() if m else ""


def gate_contract(ctx: GateContext) -> list[Finding]:
    """`stigmergy_lint.py` over the worktree, filtered to the files this capture touched.

    The linter is the knowledge repo's own gate — the same script CI runs — so the librarian is
    held to exactly the standard a human PR is, no more and no less. It scans the WHOLE repo,
    which is why the findings are filtered by path: 23 pre-existing warnings on `sources/`
    pages are not this capture's problem, and refusing someone's work for them would be absurd.

    **Run with an explicit environment.** It is a script from the repo the librarian curates, so
    the same rule applies to it as to the agent: it gets what any process needs to run and nothing
    else — never the GitHub App private key, never the queue DSN.

    Only `error` severity vetoes. The linter's warnings (a thin page, an empty section) are
    judgment calls it deliberately does not block CI on, and the librarian does not get to be
    stricter than the repo's own gate — they are recorded as notes instead.

    **One warning is dropped rather than recorded**: `orphans` on a page this capture just created.
    It fires on EVERY filed page by construction — nothing in the repo can link a page that has
    just been born — so surfacing it taught the only reader of these notes that librarian warnings
    are noise, which is worse than not having them. It is suppressed only for created pages: the
    same warning on a page that already existed is a real observation about the graph.
    """
    if not ctx.linter_path or not os.path.exists(ctx.linter_path):
        raise LibrarianConfigError(
            f"the contract linter is missing at {ctx.linter_path!r} — it is the knowledge "
            f"repo's own gate and the librarian will not file without it")
    proc = subprocess.run(
        ["python3", ctx.linter_path, "--repo", ctx.worktree, "--json"],
        capture_output=True, text=True,
        # An EXPLICIT environment, for exactly the reason `agent.agent_env` has one: this is a
        # Python script out of the repo the librarian CURATES, executed by the worker, and it must
        # not inherit `STIGMERGY_LIBRARIAN_PRIVATE_KEY` or the queue DSN just because it happens to be
        # our contract gate. It once ran with no `env=` at all.
        # The list is `gitcmd.SUBPROCESS_BASE_ENV` — "what any process needs to run at all" — shared
        # with the agent's own allow-list rather than retyped here.
        env=gitcmd.base_env())
    if proc.returncode == 2 or not proc.stdout.strip():
        raise LibrarianConfigError(
            f"the contract linter could not scan the worktree (rc={proc.returncode})")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LibrarianConfigError("the contract linter returned unparseable output") from None

    touched = set(ctx.touched_pages())
    born_here = set(ctx.in_lane_new_pages())
    out = []
    for finding in report.get("findings", []):
        if finding.get("file") not in touched:
            continue
        if finding.get("check") == ORPHAN_CHECK and finding.get("file") in born_here:
            continue
        severity = SEVERITY_VETO if finding.get("severity") == "error" else SEVERITY_NOTE
        out.append(Finding("contract", finding.get("check", "contract"),
                           f"{finding.get('file')}: {finding.get('message')}",
                           severity=severity, locator=str(finding.get("file", ""))))
    return out


# ── anchoring: nothing is filed ownerless ─────────────────────────────────────────────────────
# The code `processing._refuse` routes on. Named here rather than matched on the message, for the
# same reason `ORPHAN_CHECK` is: the message is this module's to reword, the code is its contract.
ANCHORING_UNRESOLVED = "unresolved"


def _declared_entities(anchoring: dict) -> list[str]:
    """The raw `anchoring.entities` list, defensively — `agent.parse_outcome` already normalizes
    this to a list of strings before a `gate_anchoring` ever sees it, but this module reads
    `ctx.outcome` off whatever backend produced it (the offline double included), so the same
    defence-in-depth `report._as_list` takes behind its own boundary applies here too."""
    raw = anchoring.get("entities") if isinstance(anchoring, dict) else None
    if raw is None:
        return []
    return [str(v) for v in raw] if isinstance(raw, (list, tuple)) else [str(raw)]


def resolve_entity_ids(anchoring: dict, registry) -> tuple[list[str], list[str]]:
    """The page's `entity:` value for one anchoring outcome — `(ids, unresolved)`: the RESOLVED
    canonical ids, deduplicated preserving order (`["acme", "Acme Corp"]` resolving to the same id
    must not stamp `entity: ["acme", "acme"]`), and the DECLARED values that did not resolve.
    `([], [])` for company-wide scope, which is itself a checked, explicit declaration.

    **The return type is a PAIR, not `ids` alone.** `ids` alone folds two states that must never be
    confused onto the same value, `[]`: company-wide (a checked, explicit
    declaration — the RIGHT `[]`) and "declared an entity anchor but nothing in it resolved" (a
    state that must never reach a committed page at all — the WRONG `[]`, indistinguishable from
    the right one once returned). `unresolved` is what lets a caller tell them apart without
    re-deriving the resolution itself.

    THE one place this resolution happens for the STAMP: `processing._stamp` calls this to compute
    what a filed page's frontmatter carries, and `gate_anchoring` below now builds its finding from
    the SAME call — so the veto and the value it is vetoing come from one source, not two loops
    that could disagree. (`report.filed`'s `_anchor_phrase` is a SEPARATE, independent
    implementation over the same declared list and the same registry — see that function's own
    docstring. "One source" is a claim about the resolution RULE agreeing, not about there being
    only one call site.)

    Raises whatever `registry.canonical_id` raises when `registry` is falsy/`None` — deliberately
    not caught here. A missing registry at this point is a CONFIG fault (the same one
    `gate_anchoring` would hit resolving its own declared list against `ctx.registry`), not a
    content problem to route around; silently returning `([], [])` used to make it indistinguishable
    from an ordinary company-wide capture.
    """
    anchoring = anchoring if isinstance(anchoring, dict) else {}
    if str(anchoring.get("kind", "")).lower() != "entity":
        return [], []
    ids, unresolved = [], []
    for raw in _declared_entities(anchoring):
        cid = registry.canonical_id(raw)
        if cid:
            ids.append(cid)
        else:
            unresolved.append(raw)
    return list(dict.fromkeys(ids)), unresolved


def gate_anchoring(ctx: GateContext) -> list[Finding]:
    """Every filed page declares an anchoring outcome, and the declaration is CHECKED.

    Two halves, because a declaration nobody verifies is a comment:
    - `kind: "entity"` — EVERY declared value must resolve through `ops/entity-registry.json`,
      read at the base commit. This replaced an earlier mechanism: `gate_anchoring` used to
      require a WIKILINK on the page to resolve, treating the page's own links as evidence for a
      declaration that lived only in
      the agent's outcome; the declaration is now stamped onto the page directly
      (`page.stamp_server_fields`), so what is checked is the declaration itself, and nothing
      about the page's body is read here at all.
    - `kind: "company"` — an explicit company-wide scope with a WRITTEN reason. Silence is not
      an outcome, and neither is an empty string.

    **Residual, recorded rather than silently assumed away: `entity: []` is called "checked" for
    the company-wide case, but the ONLY thing actually checked is that `reason` is a non-empty
    string — never that the reason is TRUE of the material.** The librarian judges and code
    vetoes, which is why this gate does not and should not try to verify
    that a capture genuinely belongs to no entity; that is exactly the judgment call code cannot
    make. So captured material CAN drive a page to `entity: []` for a reason that reads well but
    is wrong, and nothing here — or anywhere in the write path — detects that. The submitter's own
    report (`report.filed`'s company-wide phrase) is the only detection surface: a human who reads
    "company-wide scope (`<reason>`)" against material they know is about one specific customer is
    the only check this state ever gets. Not a defect to fix here; a residual to know about.

    `undeclared` and `no-reason` are malformed OUTCOMES — the agent said nothing usable — and
    `processing.py` turns them into a corrective retry and then a system fault. `unresolved` is
    different in kind: it is a real anchoring judgment that did not land, so a veto surviving both
    passes routes to `triage` — when the librarian believes the material is about an entity but
    cannot resolve it, the submission parks with the unresolved name, the same destination the
    agent reaches by parking the capture itself. That is why this finding
    always carries a `locator`, even when NOTHING was declared at all: an outcome declaring
    `kind: "entity"` with an empty `entities` list must still reach that same steward park rather
    than a system fault (`processing._unanchorable`, `_unresolved_name` below) — "nothing here
    anchors" is exactly the parked outcome that situation calls for.

    **PER-PAGE, when `ctx.page_declared` says so.** A meeting about two customers
    yields two `decision` pages belonging to different entities — one outcome-wide anchoring
    declaration cannot express that. When the caller populated `ctx.page_declared` (the meeting
    flow, never an ordinary capture), this gate asks a SEPARATE anchoring question of every new
    page that declares one there, instead of the single `ctx.outcome.anchoring` question below.
    Every ordinary capture's `ctx.page_declared` is empty, so it falls straight through to the
    single-outcome check below.
    """
    new_pages = ctx.in_lane_new_pages()
    if not new_pages:
        return []

    if ctx.page_declared:
        return _per_page_anchoring(ctx, new_pages)

    anchoring = getattr(ctx.outcome, "anchoring", None) or {}
    kind = str(anchoring.get("kind", "")).lower()
    if kind == "company":
        if not str(anchoring.get("reason", "")).strip():
            return [Finding("anchoring", "no-reason",
                            "declared company-wide scope with no written reason: a page that "
                            "belongs to no entity must say why in a sentence")]
        return []
    if kind != "entity":
        return [Finding("anchoring", "undeclared",
                        "filed a page without declaring an anchoring outcome: every page names "
                        "the entity it belongs to, or declares company-wide scope with a reason")]

    declared = _declared_entities(anchoring)
    # Findings cycle 1, 4.4: built from the SAME call `processing._stamp` uses to compute the
    # stamped `entity:` value, not a second loop over `ctx.registry.canonical_id` that could
    # disagree with it. `_ids` is unused here — the finding only needs which values did NOT
    # resolve — but computing it from `resolve_entity_ids` is the point: one function decides
    # what "resolves" means for both the write and the veto.
    _ids, unresolved = resolve_entity_ids(anchoring, ctx.registry)
    if unresolved or not declared:
        # `values`: the SAME list `_unresolved_name` picks its display
        # locator from (`unresolved or declared`), but VERBATIM and in full — every one of them,
        # not just the first — so `processing._unanchorable` can match a companion dead link
        # against any of several declared entities, not only whichever one `_unresolved_name`
        # happened to pick for display.
        return [Finding("anchoring", ANCHORING_UNRESOLVED,
                        _unresolved_message(declared, unresolved),
                        locator=_unresolved_name(declared, unresolved),
                        brief=anchoring_brief(ctx, declared),
                        values=tuple(unresolved or declared))]
    return []


def _per_page_anchoring(ctx: GateContext, new_pages: list[str]) -> list[Finding]:
    """One anchoring veto PER new page that declares an anchoring outcome in `ctx.page_declared`.

    A page with no `"anchoring"` key in its declaration (the meeting flow's source/meeting pages
    — provenance, never a knowledge destination) is not asked this question at all: every
    `decision` page must anchor; a provenance record has nothing to anchor.
    The meeting flow is the one caller responsible for putting an `"anchoring"` entry only on the
    pages that need one (`processing._meeting_page_specs`).
    """
    out = []
    for path in sorted(new_pages):
        declared_for_page = ctx.page_declared.get(path) or {}
        if "anchoring" not in declared_for_page:
            continue
        anchoring = declared_for_page.get("anchoring") or {}
        kind = str(anchoring.get("kind", "")).lower()
        if kind == "company":
            if not str(anchoring.get("reason", "")).strip():
                out.append(Finding("anchoring", "no-reason",
                                   f"{path}: declared company-wide scope with no written reason: "
                                   f"a page that belongs to no entity must say why in a sentence",
                                   locator=path))
            continue
        if kind != "entity":
            out.append(Finding("anchoring", "undeclared",
                               f"{path}: filed a page without declaring an anchoring outcome: "
                               f"every filed page names the entity it belongs to, or declares "
                               f"company-wide scope with a reason",
                               locator=path))
            continue
        declared = _declared_entities(anchoring)
        _ids, unresolved = resolve_entity_ids(anchoring, ctx.registry)
        if unresolved or not declared:
            out.append(Finding("anchoring", ANCHORING_UNRESOLVED,
                               f"{path}: {_unresolved_message(declared, unresolved)}",
                               locator=_unresolved_name(declared, unresolved),
                               brief=anchoring_brief(ctx, declared),
                               values=tuple(unresolved or declared)))
    return out


def _unresolved_message(declared: list[str], unresolved: list[str]) -> str:
    """The human-facing finding: names every unresolved id, not just the
    first, so a corrective retry — or the steward reading the eventual park — can fix every one in
    a single pass rather than discovering the second unresolved id only after "fixing" the first."""
    if not declared:
        return ('declared an entity anchor, but "anchoring.entities" names no entity at all')
    ids = unresolved or declared
    quoted = [f'"{_one_line(i, MAX_BRIEF_NAME_LEN)}"' for i in ids[:MAX_BRIEF_ITEMS]]
    phrase = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + f" and {quoted[-1]}"
    rest = len(ids) - len(quoted)
    if rest > 0:
        phrase += f", and {rest} more"
    noun = "anchor" if len(ids) == 1 else "anchors"
    verb = "does" if len(ids) == 1 else "do"
    return (f"declared entity {noun} {phrase} {verb} not resolve in the entity registry read at "
            f"the base commit")


# ── the corrective brief: written for an agent that must repair ────────────────────────────────
# How much of what a gate examined its brief reproduces. Wikilinks come off a page the agent drafted
# from UNTRUSTED material, so they are clamped and stripped of control characters on the same seam
# every other echoed value in this repo uses (`index.rank.sanitize`, via `report._clean` for the
# human surfaces): a brief goes into the next pass's PROMPT, where a name carrying newlines could
# otherwise forge the brief's own structure.
MAX_BRIEF_ITEMS = 12
MAX_BRIEF_NAME_LEN = 80

# Whether the brief LISTS the registry's names or only counts them. The knowledge repo's registry
# holds three entities, and the whole point of a brief is to make the available outcome concrete, so
# the full list is the most actionable thing it can carry. Past this size it gives the count instead:
# a TRUNCATED candidate list is worse than none, because "not in the list" would then read as proof
# that re-anchoring is unavailable when the name is merely unlisted.
MAX_BRIEF_REGISTRY_NAMES = 40


def _one_line(text: str, limit: int) -> str:
    """One bounded, single-line, control-character-free rendering of an untrusted name.

    A DISPLAY transform, not an identity key — `len(text) > limit` truncates with an ellipsis, so
    two different names over `limit` can render identically, and a DEcomposed vs COMPOSED spelling
    of the same accented name renders as two different strings. `processing._unanchorable` used to
    compare THIS output for identity and inherited both blind spots; it now
    compares `normalize_identifier`'s output instead, which is built for that job.
    """
    out = " ".join(textutil.sanitize(str(text or "")).split())
    return out if len(out) <= limit else out[:limit].rstrip() + "…"


def normalize_identifier(text: str) -> str:
    """NFC + casefold + whitespace-collapse — the same doctrine `page.path_key` already applies to
    a PATH (NFC + casefold), for the same reason: two spellings of one name must compare equal.
    Whitespace-collapse is added here because the two producers being compared (a declared
    anchoring value, a wikilink target) need not agree on incidental spacing either.

    Findings cycle 2, B2: `processing._unanchorable` used to compare `Finding.locator` — a DISPLAY
    string (`_one_line`, above) built for a human or a prompt, clamped and ellipsised — as if it
    were an identity key. This is the identity key: no clamp, no ellipsis, and normalized so an
    NFD-composed accented name matches its NFC-composed twin the way an accent-carrying brain (the
    stated normal case) needs it to.
    """
    return " ".join(unicodedata.normalize("NFC", text or "").split()).casefold()


def _listed_names(names, *, limit: int) -> str:
    """`a, b, c` — bounded, and saying so when it stopped early rather than trailing off."""
    names = [_one_line(name, MAX_BRIEF_NAME_LEN) for name in names]
    shown = names[:limit]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f", and {rest} more" if rest > 0 else "")


def registry_candidates(registry) -> list[dict]:
    """Every entity the registry would resolve, as `{"name": ..., "aliases": [...]}`.

    THE one reading of "which entities exist" that anything outside `kernel.registry` does.
    Two readers with different audiences share it deliberately: `anchoring_brief` lists the names
    for the AGENT that must repair an anchor, and `report.needs_input` lists them with their aliases
    for the PERSON being asked which one their material is about. They are the same fact about the
    same loaded registry, and the whole argument against a second implementation is written above
    `MAX_BRIEF_REGISTRY_NAMES`: a candidate list that quietly differs from the one the gate actually
    asks is worse than no list, because "not in the list" then reads as proof rather than as an
    artifact of which code path built it.

    Aliases matter to the human surface specifically. The commonest real ask-back is a name that IS
    registered under a spelling the reader did not use, and a list of canonical names alone invites
    the answer "it's new" — which mints a duplicate entity, the exact failure the registry exists
    to prevent.

    Duck-typed: a gate must not crash on the shape of a collaborator it only ever asks one question
    of.
    """
    entities = getattr(registry, "entities", None) or {}
    out = []
    for entity in entities.values():
        if not isinstance(entity, dict) or not entity.get("name"):
            continue
        aliases = entity.get("aliases")
        out.append({"name": str(entity["name"]),
                    "aliases": [str(a) for a in (aliases if isinstance(aliases, list) else [])]})
    return sorted(out, key=lambda e: e["name"])


def _registry_id_names(registry) -> list[tuple[str, str]]:
    """`(id, name)` for every entity the registry holds, sorted by id — what `anchoring_brief`
    lists: the brief asks the agent to declare an ID, so the ids themselves are what it has to
    show, not only the display names `_registry_names` gives
    `report.needs_input`'s very different (non-technical, id-free) reader.

    Duck-typed like `registry_candidates`, for the same reason: a gate must not crash on the
    shape of a collaborator it only ever asks one question of.
    """
    entities = getattr(registry, "entities", None) or {}
    out = [(str(cid), str(e["name"])) for cid, e in entities.items()
          if isinstance(e, dict) and e.get("name")]
    return sorted(out)


def _unresolved_name(declared: list[str], unresolved: list[str]) -> str:
    """WHICH name could not be resolved — the one a parked report tells a steward to register.

    An unresolved declared value first, because that is the one this finding is actually about;
    the first declared value at all as the fallback, for the case where every declared value
    DOES resolve individually except this call only runs when at least one does not (see
    `gate_anchoring`). Falls back to a fixed, non-empty placeholder — never `""` — when NOTHING
    was declared at all (`kind: "entity"` with an empty `entities` list): `processing.
    _unanchorable` requires a truthy locator before it parks, and the whole point is that this
    case reaches the steward park ("nothing here anchors") rather than falling through to a
    worse-sounding system fault for want of a name to
    report. `capture_schema.UNNAMED_ENTITY_PLACEHOLDER` is the same fallback word
    `processing._triage` already uses for an agent-declared park with no name — one spelling for
    "nothing was named", not two, and shared centrally specifically so
    `entities.cli._suggestable` can refuse to offer it as a fillable `--name` — it is syntactically
    an ordinary name and would otherwise be suggested straight into a ready-to-run
    `stigmergy-entities approve ... --name "something unnamed"`.

    Bounded and stripped like anything else that came off a drafted outcome — `anchoring.entities`
    is bounded by `parse_outcome`, this is defence in depth behind that.
    """
    for name in [*unresolved, *declared]:
        cleaned = _one_line(name, MAX_BRIEF_NAME_LEN)
        if cleaned:
            return cleaned
    return capture_schema.UNNAMED_ENTITY_PLACEHOLDER



def anchoring_brief(ctx: GateContext, declared: list[str]) -> str:
    """What an agent whose entity anchor did not resolve is told, so it can repair it.

    Written against what the gate actually does: it does not read the page's body at all, so this
    brief is built entirely from the OUTCOME's own declared `anchoring.entities` list and the
    registry — never from a wikilink.

    Public and separately testable because it is the reference shape every other gate's brief
    copies. It says three things the `message` does not:

    1. **what the gate examined** — the declared entities themselves, and the registry it asked.
       Naming the registry's CONTENTS rather than its path matters here: pointing at a PATH is the
       weaker move even now that `base_inputs` reads the registry and the diff at the SAME base
       commit (they used to disagree: the registry came from the local working tree while the diff
       was built from `origin/<branch>`) — "go and read
       `ops/entity-registry.json`" still sends the agent to whatever a LOCAL checkout happens to
       hold, not to the base-commit snapshot `ctx.registry` actually is, and an ephemeral worktree
       may have no such path to read at all. This list sidesteps the question entirely. It shows
       IDS now, not display names — outcome 1 below asks the agent to declare an id, and a brief
       that showed only names would leave it guessing at a slug.
    2. **which acceptable outcomes are available** — an empty registry is a different situation
       from a misspelled name, and the agent could not previously tell them apart. When nothing is
       registered, re-anchoring is not a repair that exists, and the brief says so instead of
       leaving it to be discovered by a second failure.
    3. **the smallest edit that reaches one** — two of the three outcomes need no change to the
       page at all, only to the outcome file. The measured failure was an agent that rewrote the
       page, renamed it and re-declared its entities three times over, which is precisely what a
       brief that names no repair invites.

    The last line is there for the same reason: the one thing the agent tried on every measured
    retry is the one thing that cannot work — restated for the new mechanism: rewriting the page
    cannot help either, because the page is no longer read.
    """
    id_names = _registry_id_names(ctx.registry)
    if not id_names:
        registry_line = ("the registry loaded for this run holds NO entities at all, so outcome "
                         "1 below is unavailable — no id can resolve on this pass")
    elif len(id_names) <= MAX_BRIEF_REGISTRY_NAMES:
        # Findings cycle 1, 4.3: this used to join `cid`/`name` raw. Both come off the loaded
        # registry — repo-controlled text, same as `declared_line` above — and this string goes
        # straight into the NEXT agent pass's prompt, where a name carrying a newline forges the
        # brief's own structure (the module's own rule at the top of this section: "a brief goes
        # into the next pass's PROMPT... a name carrying newlines could otherwise forge the
        # brief's own structure"). Routed through the same `_one_line`/`MAX_BRIEF_NAME_LEN` seam
        # `declared_line` already uses, so both halves of this brief hold the same guarantee.
        rendered = ", ".join(f"{_one_line(cid, MAX_BRIEF_NAME_LEN)} — {_one_line(name, MAX_BRIEF_NAME_LEN)}"
                             for cid, name in id_names[:MAX_BRIEF_REGISTRY_NAMES])
        registry_line = (f"the registry loaded for this run resolves exactly these "
                         f"{len(id_names)} ids (id — name): {rendered}")
    else:
        registry_line = (f"the registry loaded for this run resolves {len(id_names)} ids, too "
                         f"many to list here; none of them is what this outcome declared")
    declared_line = (_listed_names(declared, limit=MAX_BRIEF_ITEMS)
                     if declared else
                     'nothing — "anchoring.entities" is empty')

    lines = [
        'no entity anchor resolved. The gate read THIS OUTCOME\'S declared "anchoring.entities" '
        'list and asked the entity registry (loaded at the base commit — this list, not the file '
        'in the checkout, is what the gate asks) to resolve each one; none resolved.',
        f"  declared — {declared_line}",
        f"  registry — {registry_line}",
        "Three outcomes satisfy this gate. Pick the one that is TRUE of the material, not the one "
        "that files:",
    ]
    if id_names:
        lines.append(
            '  1. ANCHOR — declare a registry id from the list above, spelled exactly as it is '
            'listed there (an id, never a display name or alias): "anchoring": {"kind": "entity", '
            '"entities": ["<that id>"]}. This is a change to the OUTCOME only — nothing on the '
            'page itself is checked or needs to change.')
    else:
        lines.append(
            "  1. ANCHOR — unavailable on this pass: the registry is empty, so no id resolves.")
    lines += [
        '  2. COMPANY-WIDE — if the material genuinely belongs to no single entity, change only '
        'the outcome: "anchoring": {"kind": "company", "reason": "<one sentence saying why it '
        'belongs to no entity>"}. The reason is required; an empty string is not one.',
        '  3. PARK — if the material really is about a specific entity nobody has registered yet, '
        'do NOT file it: "decision": "triage", "triage": {"kind": "unresolved-entity", "name": '
        '"<the name>"}, and write no page. Nothing is committed and a steward registers the '
        'entity. This is a correct outcome, not a failure.',
        "Adding a wikilink, rewriting the body, or renaming the page does not change this gate's "
        "answer: it reads the declared \"entities\" list against the registry named above, and "
        "only that registry.",
    ]
    return "\n".join(lines)


# ── server-owned frontmatter: the post-condition, checked by a real YAML parser ────────────────
# Never present on a filed page at all, whatever they parse to. `owner` is the one that matters
# most: it is the field that says who ANSWERS for a page, and a captured document must not be able
# to assign that to a third party (spec resolved question 4). `id` and `content_hash` belong to
# the source-page provenance group and are not fast-lane fields.
#
# `tier` and `extracted_at` sit beside `content_hash` here — the rest of the page contract's
# provenance group, and the only fields the meeting flow writes to a committed page that no other
# flow does. They used to be neither forbidden (a `tier:`/`extracted_at:` on a
# `decision` or `meeting` page passed silently) nor stripped (`page.stamp_server_fields` only
# strips `SERVER_OWNED_KEYS`, which never included them) — the one provenance field this module
# DID police (`content_hash`) got both a forbidden-elsewhere rule and a duplicate-declaration
# backstop; its two siblings got neither. `PROVENANCE_PAGE_KEYS` (below) is the
# group both this tuple's exemption and the duplicate-key backstop now read, so a fourth
# provenance field never has to be added to two places by hand again.
FORBIDDEN_PAGE_KEYS = ("owner", "id", "content_hash", "tier", "extracted_at")

# The provenance group that is legitimate ONLY on the meeting flow's one `sources/meetings/`
# source page (`ctx.provenance_pages`) — never
# inferred from the diff's own shape, but told to this gate by the caller that populated
# `provenance_pages` (see that field's own comment on `GateContext`, and the correction on
# `_frontmatter_findings`'s own use of it below). Named once so `FORBIDDEN_PAGE_KEYS`'s
# provenance exemption and the duplicate-declaration backstop (`_frontmatter_findings`) read the
# SAME set rather than two hand-kept lists that could drift.
# `id` (ADR 028 D6): a source part's explicit chain identity, stamped by
# `page.stamp_source_fields` from the producer's own computation and verified below by the same
# output-equality check every stamped field gets. Legitimate ONLY on `ctx.provenance_pages` —
# on any other page a declared `id:` stays the forbidden field it always was.
PROVENANCE_PAGE_KEYS = ("content_hash", "tier", "extracted_at", "id")

# Two shapes refused OUTRIGHT, before any YAML parsing, because neither has
# ANY legitimate reason to appear in this repo's page dialect at all — unlike the duplicate-key
# and forbidden/forged-field checks below, these do not need to collide with a server-owned key to
# be refused.
_BOM = "\ufeff"  # U+FEFF, the UTF-8 BOM
_EXPLICIT_KEY_RE = re.compile(r"^\?\s")

# A top-level YAML merge key (`<<: *anchor`) joins the outright
# refusals for the same reason — it has no legitimate use in this repo's page dialect, and
# `page.duplicate_top_level_keys` cannot see a key it contributes (see that function's own
# docstring for why: the merged key lives on the aliased node, one level below `root.value`).
_MERGE_KEY_RE = re.compile(r"^<<\s*:")

# The WHITELIST that replaces the confusables table (`page._HOMOGLYPH_FOLD`) as the actual gate.
# Enumerating individual confusable spellings (mixed case, then Cyrillic homoglyphs, then
# quoting/escaping/BOM tricks) does not converge — a Greek small omicron (`οwner:`), a Greek
# capital epsilon
# (`Εntity:`), a Turkish dotless ı (`entıty:`), small-caps letterforms (`ᴇɴᴛɪᴛʏ:`), a zero-width
# joiner inside the word, and a combining accent instead of a precomposed character all survive
# `page.stamp_server_fields`' strip (none of them fold to an ASCII `SERVER_OWNED_KEYS` name) and
# produced ZERO findings from this gate before this check existed. Rather than add a seventh,
# eighth, ninth entry to a table that will never finish, this inverts the control: a top-level
# frontmatter key must look like a plain, lowercase, ASCII identifier, or the page is refused
# outright — not because any specific spelling is known to be dangerous, but because nothing
# this repo's page dialect legitimately needs looks like anything else. Verified against the real
# corpus (`wiki/`, `sources/`, `views/` and every `ops/templates/*.md` in the knowledge repo):
# every key in real use matches.
_ALLOWED_KEY_RE = re.compile(r"^[a-z_][a-z0-9_.-]*$")


def gate_frontmatter(ctx: GateContext) -> list[Finding]:
    """The stamped frontmatter is what a YAML PARSER says it is — not what our line editor thinks.

    `page.stamp_server_fields` rewrites server-owned fields line by line, and that is the right
    implementation: a YAML round-trip would reformat a block humans diff. But its matcher reads
    bare keys only, so a page declaring `"owner": "ceo@acme.com"` — a QUOTED key — survived the
    strip untouched, and PyYAML then reads it as a real `owner`. That is precisely the thing the
    module docstring claims is impossible, and it is the one field whose forgery reassigns
    accountability to somebody who never agreed to it.

    So the stamp keeps its line-based implementation and gains a post-condition: parse the filed
    page's frontmatter the way every consumer will, and refuse if what comes out disagrees with
    what the server wrote. A gate that checks the OUTPUT cannot be defeated by a new way of
    spelling the input, which is why this is a parser and not another regex.

    **A SECOND post-condition, on the raw text.** `page._strip_keys` reads all three key
    spellings, so this should not be reachable — but "the value a real YAML parser reads out
    matches what the server stamped" is a
    post-condition a DUPLICATE declaration can satisfy (PyYAML takes the LAST occurrence, and the
    server's own line is appended last), which means it is not actually a post-condition on "the
    capture's own declaration was removed." This gate also refuses outright if any server-owned
    key is declared more than once in the raw frontmatter, under any spelling — a check the parsed
    VALUE comparison above cannot make on its own, however good `_strip_keys` gets.

    **That "second, independent post-condition" was once not independent.**
    `page.duplicate_top_level_keys` called the SAME `_match_key` the strip uses, so a spelling
    `_match_key` cannot see (YAML explicit-key syntax, a hex escape inside a quoted key, a BOM as
    the block's first byte) was invisible to both the strip AND its own backstop at once — five
    reproduced bypasses, all committing the capture's own `entity:` declaration beside the
    server's stamped one. Closed three ways:
    - `page.duplicate_top_level_keys` is now PARSER-based (`yaml.compose`, PyYAML's own notion of
      key identity) — independent of `_match_key` by construction, not just in practice.
    - A BOM inside the block, or a top-level YAML explicit-key line, is refused OUTRIGHT below,
      before any parsing — neither has any legitimate reason to appear in this repo's page dialect
      at all, whether or not it happens to collide with a server-owned key.
    - Case and homoglyph spellings (`Entity:`, `еntity:` — Cyrillic е) are not "duplicates" to a
      real parser (they are genuinely different strings), so they are closed separately: both
      `page._strip_keys` and `FORBIDDEN_PAGE_KEYS` below compare on `page.normalize_key`
      (NFKC + casefold), not raw equality.

    **Enumerating confusable spellings does not converge, so the control inverts.** A Greek small
    omicron (`οwner:`), a Greek capital epsilon (`Εntity:`), a Turkish dotless ı (`entıty:`),
    small-caps letterforms (`ᴇɴᴛɪᴛʏ:`), a zero-width joiner inside the word, and a combining accent
    instead of a precomposed character are six more bypasses of the same shape — every one of them
    survives `page.stamp_server_fields`' strip
    (none folds to an ASCII `SERVER_OWNED_KEYS` name under `page.normalize_key`, because
    `page._HOMOGLYPH_FOLD` has never covered Greek, Turkish, small-caps, or combining-mark tricks,
    and never will finish covering the next script either) and produced ZERO findings here before
    this check existed. Rather than add a seventh table entry, this gate now REFUSES any top-level
    frontmatter key that is not a plain lowercase ASCII identifier (`_ALLOWED_KEY_RE`) — a whitelist
    is the only version of this control that does not need to be revisited every time someone finds
    script number four. The confusables table stays for `_strip_keys`' best-effort cleanup (the
    Cyrillic spellings it covers are healed silently rather than refused), but it is no longer what
    stands between a forged field and a filed page — this pattern is.

    A top-level YAML merge key (`<<:`) is refused outright the same way, for the same reason
    `duplicate_top_level_keys` names in its own docstring: a key it contributes never appears in
    `root.value`, so nothing downstream that walks the compose tree can see it.

    **Every check above runs on MODIFIED pages too, not only new ones.** It used to run on
    `ctx.in_lane_new_pages()` alone — the duplicate-key backstop, the key whitelist, the
    BOM/`? key`/merge-key refusals, the stamped-value post-condition — which left all of it absent
    for a page that is MODIFIED rather than created, and `gate_body_rewrite`'s own byte-for-byte
    rule has no opinion about whether a field's VALUE is legitimate, only about whether the SHAPE
    of the change is legal.

    **No caller gets an exception.** A NEW page's stamped values must match `ctx.stamped` exactly;
    a MODIFIED page gets `expected = {}` and the forbidden-key check, so `owner` is categorically
    forbidden and a smuggled `verification:` is caught as a forbidden field with nothing to argue
    about.
    """
    out = []
    for path in ctx.in_lane_new_pages():
        # `ctx.stamped_by_path` carries PER-PAGE stamped values when the caller populated it
        # (the meeting flow, whose decision pages each stamp a different `entity:`); every
        # ordinary capture leaves it empty and every new page falls back to the single
        # `ctx.stamped` dict.
        expected = dict(ctx.stamped_by_path.get(path) or ctx.stamped or {})
        out.extend(_frontmatter_findings(ctx, path, expected=expected))
    for path in ctx.in_lane_modified_pages():
        out.extend(_frontmatter_findings(ctx, path, expected={}))
    return out


def _frontmatter_findings(ctx: GateContext, path: str, *, expected: dict) -> list[Finding]:
    """The structural checks `gate_frontmatter` runs per page, shared by the new-page and
    modified-page loops — the two differ only in `expected`, the stamped values a real value must
    equal. `owner:` is categorically forbidden on both: nothing legitimately adds one."""
    try:
        with open(os.path.join(ctx.worktree, path), encoding="utf-8") as f:
            front, _ = page_policy.split_frontmatter(f.read())
    except (OSError, UnicodeDecodeError):
        return []            # `gate_binary_page` already vetoed it

    out = []
    if _BOM in front:
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: its frontmatter contains a Unicode BOM (U+FEFF), which "
                           f"has no legitimate place inside a frontmatter block — refused "
                           f"outright",
                           locator=path))
    if any(_EXPLICIT_KEY_RE.match(line) for line in front.splitlines()
          if not line[:1].isspace()):
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: its frontmatter uses YAML explicit-key syntax (`? key`), "
                           f"which is not part of this repo's page dialect — refused outright",
                           locator=path))
    if any(_MERGE_KEY_RE.match(line) for line in front.splitlines()
          if not line[:1].isspace()):
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: its frontmatter uses a YAML merge key (`<<:`), which is "
                           f"not part of this repo's page dialect — refused outright",
                           locator=path))

    # The provenance group is in the duplicate-declaration backstop too. `content_hash` is in
    # `page.SERVER_OWNED_KEYS` and so was covered here already; `tier`/`extracted_at` were not, so
    # a duplicate declaration of either (the capture's own forged line beside the server's stamped
    # one) was invisible to this check even though the equality post-condition above would
    # eventually catch the PARSED value — the same duplicate-key hole already closed for
    # `entity:` and for every other server-owned key.
    dup_keys = page_policy.duplicate_top_level_keys(front) & (
        set(page_policy.SERVER_OWNED_KEYS) | set(PROVENANCE_PAGE_KEYS))
    for key in sorted(dup_keys):
        out.append(Finding("frontmatter", "forged-field",
                           f"{path}: it declares {key!r} more than once — a server-owned "
                           f"field must appear exactly once, and a duplicate can hide a "
                           f"capture's own attempt behind whichever occurrence a YAML parser "
                           f"happens to read last",
                           locator=path))
    try:
        parsed = yaml.safe_load(front) if front.strip() else {}
    except yaml.YAMLError:
        out.append(Finding("frontmatter", "unparseable",
                           f"{path}: its frontmatter is not valid YAML, so what the page "
                           f"actually declares cannot be established",
                           locator=path))
        return out
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        out.append(Finding("frontmatter", "unparseable",
                           f"{path}: its frontmatter does not parse to a mapping",
                           locator=path))
        return out

    # THE whitelist, checked against the real parsed keys —
    # whatever a real YAML parser resolved this frontmatter's top-level keys to, every one of
    # them must look like a plain lowercase ASCII identifier. A key that is not even a string
    # (a bare `123:`) is refused the same way: it cannot match the pattern either.
    for key in parsed:
        if not isinstance(key, str) or not _ALLOWED_KEY_RE.match(key):
            out.append(Finding("frontmatter", "forged-field",
                               f"{path}: it declares the top-level key {key!r}, which is not "
                               f"a plain lowercase identifier (`^[a-z_][a-z0-9_.-]*$`) — "
                               f"refused outright, whatever it resembles",
                               locator=path))

    # `normalize_key` on both sides: `Owner:`/`оwner:` (a homoglyph)
    # parse to a DIFFERENT dict key than `owner` to a real YAML parser too, so a raw `in`
    # test against `parsed` would miss them exactly like the old `_strip_keys` did.
    normalized_parsed_keys = {page_policy.normalize_key(k) for k in parsed if isinstance(k, str)}
    # `content_hash`/`tier`/`extracted_at` (`PROVENANCE_PAGE_KEYS`) are legitimate ONLY on
    # the meeting flow's one provenance page. `ctx.provenance_pages` is populated by
    # `processing._stamp_meeting`, which derives it from the diff's own shape (the single new page
    # under `sources/meetings/` — provably exactly one, by the time this runs, or
    # `_cross_check_meeting_outcome`'s `source-page-count` veto has already refused the commit).
    # Note precisely where the control lives: `gate_frontmatter` only ever READS
    # `ctx.provenance_pages` and never recomputes "is this a source page" itself — that is the
    # caller-declares-the-fact posture. The field's PRODUCER (`_stamp_meeting`) does derive the set
    # from the diff's shape, as described above; the two are different questions and this comment
    # exists so a reader does not conflate them.
    provenance_ok = path in ctx.provenance_pages
    for key in FORBIDDEN_PAGE_KEYS:
        if key in PROVENANCE_PAGE_KEYS and provenance_ok:
            continue
        if page_policy.normalize_key(key) not in normalized_parsed_keys:
            continue
        out.append(Finding("frontmatter", "forbidden-field",
                           f"{path}: the filed page still declares {key!r}, which the "
                           f"fast lane never sets and a capture may never assert",
                           locator=path))
    for key, expected_value in expected.items():
        if expected_value is None:
            if key in parsed:
                out.append(Finding("frontmatter", "forged-field",
                                   f"{path}: it declares {key!r}, which the server resolved "
                                   f"to nothing and the page must therefore omit",
                                   locator=path))
            continue
        # `str()` on both sides on purpose: `as_of: 2026-07-26` parses to a `date`, and the
        # question is whether the page says what the server said, not which Python type
        # PyYAML chose to say it with.
        if _as_text(parsed.get(key)) != _as_text(expected_value):
            out.append(Finding("frontmatter", "forged-field",
                               f"{path}: its {key!r} is not the value the server stamped — "
                               f"that field is computed by the server and a capture's own "
                               f"declaration of it is ignored",
                               locator=path))
    return out


def _as_text(value) -> str:
    """One comparable spelling for a frontmatter value, whatever YAML made of it."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


# ── orchestration ─────────────────────────────────────────────────────────────────────────────
# Order matters only for readability of the findings list; every gate runs every time, so one
# corrective retry carries EVERY problem back to the agent rather than revealing them one per
# attempt (there is only one retry — a gate that hid a second problem would waste it).
#
# `gate_binary_page` is the one ordering that IS load-bearing: it establishes that the pages are
# text at all, which is the precondition the secrets, PII, body-rewrite and trace gates were
# silently assuming when a single NUL byte turned all four of them off.
ALL_GATES = (gate_zone, gate_binary_page, gate_body_rewrite, gate_secrets, gate_pii,
             gate_frontmatter, gate_contract, gate_anchoring)


def run_gates(ctx: GateContext, gates=ALL_GATES) -> list[Finding]:
    """Run every gate over one attempt's diff and return every finding, vetoes first."""
    findings = []
    for gate in gates:
        findings.extend(gate(ctx))
    findings.sort(key=lambda f: (f.severity != SEVERITY_VETO, f.gate, f.code))
    return findings


def vetoes(findings) -> list[Finding]:
    return [f for f in findings if f.severity == SEVERITY_VETO]


def unrepairable(findings) -> list[Finding]:
    """The vetoes that name no repair the agent can perform — empty when the retry is worth taking.

    **The general form of "a message is not a brief", applied where there is no brief to write.**
    A veto the agent cannot act on makes the second pass a certainty rather than a chance: it costs
    the run, delays the honest answer by an agent pass, and — for `zone/body-rewrite` — hands back
    an instruction to repair something the agent did not do and cannot do.

    **Any unrepairable veto stops the retry, not only an all-unrepairable set.** The retry exists to
    reach a pass with NO vetoes; one that cannot clear makes that unreachable, so the second pass
    could at best swap which refusal is reported. Refusing after one pass is the same terminal state
    sooner, and the report's agent-attempt counter then says `1` — which is true, and is how an
    operator can tell this branch was taken.

    The six are the ones whose subject is a page the agent cannot write, or cannot have written:
    `zone/body-rewrite`, `zone/unparseable` and `zone/unreadable-edit` judge a MODIFIED page, which
    only `edits.apply_declared` produces; `secrets/unscanned-diff` and `pii/unscanned-diff` report
    that a scanner could not run over one; and `zone/meeting-edit-refused` fires only when
    `ctx.edits_allowed` is `False` — a caller-level fact, not a per-diff judgment — where the agent
    holds no tool that could have produced the modification.

    **Not because the retry could not clear it**: `processing._reset_for_retry`'s
    `reset --hard` + `clean -fdq` would, for a transient external write. The real reason is that
    `processing.preserve_refused_diff` runs only on this terminal path, never before a retry, so a
    repairable finding here would let that reset erase the only evidence of an unexplained write
    into the worktree before an operator ever saw it. Fail-closed while keeping the evidence is the
    right posture for a modification nothing here can explain; a silent second pass that might
    destroy that evidence is not.

    Everything else stays repairable, including the zone gate's `deletion` / `unsupported-change` /
    `not-a-regular-file` — the agent has no tool that can produce any of them either, and they have
    no known producer at all, same as `zone/meeting-edit-refused` above. **Whether their own
    reset-before-retry destroys the same kind of evidence, and so whether they belong on this list
    too, is an open question** — flagged here rather than settled, so the next person extending
    this list does not read the silence as an answer.
    """
    return [f for f in vetoes(findings) if not f.repairable]


def corrective_brief(findings, *, reset: bool = True) -> str:
    """What the agent is told on its one corrective retry: the vetoes, as instructions to REPAIR.

    **A gate's message is not a brief.** This forwarded `message` and nothing else, and the
    anchoring case measured what that costs: three forced vetoes, three
    retries, zero recoveries — with the brief provably delivered and provably read (the agent
    rewrote the page, renamed it and changed its declared entity list, 11–16 turns each time),
    including a variant where nothing in the repo contradicted it. The retry did not fail; the
    brief did. *"No wikilink on the page resolves through the entity registry"* is a diagnosis
    written for a human reading a report: no locator, no candidate names, no menu of the outcomes
    that would satisfy the gate. Contrast the contract linter's dead-link finding — same
    machinery, and its message happens to name a path, a target and a repair the agent can
    execute.

    So a finding may carry its own `brief`, and one that does not falls back to `message` — which
    is right where the message already reads as an instruction, and is the standing debt where it
    does not. A brief owes three things a message does not: what the gate actually EXAMINED, which
    of the acceptable outcomes are AVAILABLE here, and the SMALLEST edit that reaches one.

    The preamble states the worktree reset as a fact (it happens immediately after this text is
    composed) and deliberately no longer says "write the page again": parking the capture is a
    valid repair for at least one gate, and a preamble that presupposes re-filing argues against
    the brief underneath it.

    Every veto reaching here is repairable, because `processing._run_in_worktree` does not compose a
    brief at all when one is not (`unrepairable`) — so the preamble's promise that each point says
    what would satisfy it is one this function can keep.

    **`reset`: the preamble is a CLAIM about what just happened, so it has to be true.** The fast
    lane's retry really does reset the worktree (`processing._reset_for_retry`) immediately after
    this text is composed, so the default, `True`, states a fact there — and every caller today is
    that one. `reset=False` exists for a caller with no worktree to reset and no retry ceremony:
    telling such a caller "nothing you wrote is still on disk" would be lying about a reset that
    never happened, which is the same class of defect as handing back a message in place of a
    brief.
    """
    lines = [_brief_item(f) for f in vetoes(findings)]
    preamble = ("The gates refused this draft, and the worktree has been reset to the commit it "
               "branched from — nothing you wrote is still on disk. " if reset else
               "The gates refused this proposal; nothing was committed or pushed. ")
    return (preamble
           + "Every point below has to be resolved. Each one says what would satisfy it; where a "
             "point names more than one acceptable outcome, pick the one that is true of the "
             "material.\n"
           + "\n".join(lines))


def _brief_item(finding: Finding) -> str:
    """One veto as a list item, with any continuation lines indented under it so a multi-line
    brief stays one point rather than reading as several."""
    first, *rest = (finding.brief or finding.message).splitlines() or [""]
    return "\n".join([f"- [{finding.gate}] {first}",
                      *(f"  {line}" if line else "" for line in rest)])
