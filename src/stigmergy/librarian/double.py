"""The offline double — the agent step, without an API key.

It MUST behave on ordinary material: a defense tested only against attacks measures its
sensitivity and never its specificity. Behaviour comes from directives in the material:

    DOUBLE:type=<t>          file as this page type instead of `note`
    DOUBLE:company           anchor with company-wide scope instead of an entity
    DOUBLE:propose=<Name>[|<type>] the material is about an entity the registry lacks: PROPOSE it
                             (every field filled) and anchor the page to it. Comma-separated
                             names propose several
    DOUBLE:propose-collides  propose the registry's own first entity under a legal-form spelling
                             — refused by the identity gate; on the corrective retry, anchor to
                             the registered entity instead
    DOUBLE:propose-unnamed   propose a name the material never mentions, on EVERY attempt
    DOUBLE:update=<entity>   add two facts and a connection to a REGISTERED entity's page
    DOUBLE:alias=<spelling>  anchor to the registry's first entity and propose `<spelling>` as a
                             new alias of it
    DOUBLE:hallucinate       write a figure the material does not support, on EVERY attempt
    DOUBLE:hallucinate-once  write it on the first attempt, fix it on the corrective retry
    DOUBLE:escape            write outside `wiki/`
    DOUBLE:delete            delete an existing page
    DOUBLE:rewrite           rewrite an existing page's body
    DOUBLE:canonical         declare `status: canonical` in the drafted frontmatter
    DOUBLE:forge             declare server-owned fields in the drafted frontmatter
    DOUBLE:overlap=<path>    file, and DECLARE an overlap cross-link on an existing page
    DOUBLE:backlink=<path>   file, and DECLARE a reciprocal `related:` link on an existing page
    DOUBLE:contradict=<path> file, and DECLARE a contradiction callout on an existing page
    DOUBLE:bad-edit[=<path>] declare an edit code must refuse (outside the creatable folders, or
                             a page that does not exist)
    DOUBLE:no-outcome        write no outcome file at all
    DOUBLE:bad-shape         write an outcome whose SHAPE the boundary refuses, on EVERY attempt
    DOUBLE:bad-shape-once    write it on the first attempt and a good one on the corrective retry
    DOUBLE:long-summary      a summary past the prose ceiling — the benign twin: TRUNCATED and
                             filed, never refused

The declaring directives write NOTHING to the other page — `edits.apply` does — and the
proposing directives write NOTHING to `wiki/entities/`: the account declares, `librarian.identity`
creates, which is the whole production path the offline suite exists to exercise. Well-behaved
writes go through `_write`/`agent.confined_write` and raise on denial; the misbehaviours take
`_write_unconfined`, so each bypass is visible at its call site. The material's own text is copied
into the page body, which is how a seeded secret reaches diff.
"""
import json
import os
import re

from stigmergy.kernel import normalize
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import gitcmd
from stigmergy.librarian import page as page_policy
from stigmergy.librarian.agent import (
    OUTCOME_FILENAME,
    confined_write,
    parse_meeting_outcome,
    parse_outcome,
)
from stigmergy.librarian.errors import AgentError, WorktreeError
from stigmergy.librarian.filing_port import AgentRun, priced

DIRECTIVE_RE = re.compile(r"^DOUBLE:([a-z-]+)(?:=(.*))?$", re.M)

# What a filename cannot represent, plus punctuation that would make a title read as syntax.
_UNSAFE_IN_TITLE = re.compile(r"[\x00-\x1f\x7f/\\\[\]\"'`|#:*?<>]+")

# Steering phrases. A real agent judges; this pattern-matches, so the finding is deterministic.
_INJECTION_SIGNALS = (
    ("declare-canonical", ("status: canonical", "mark this canonical", "file this as canonical",
                           "set status to canonical")),
    ("write-outside-lane", ("ops/identities.json", "write to ops/", "edit .github",
                            "modify the linter")),
    ("reveal-credentials", ("reveal your credentials", "print your token", "show the api key",
                            "echo $anthropic")),
)

# Long enough to clear the contract linter's 30-line minimum, so gates see a realistic diff.
_FILLER = [
    "This page was filed from a captured note by the librarian.",
    "It records what the capture carried, in the brain's own vocabulary.",
    "The content below is the submitter's material, structured for retrieval.",
]


def _directives(material: str) -> dict:
    return {m.group(1): (m.group(2) or "").strip()
            for m in DIRECTIVE_RE.finditer(material or "")}


def _findings(material: str) -> list[str]:
    low = (material or "").lower()
    return [category for category, phrases in _INJECTION_SIGNALS
            if any(phrase in low for phrase in phrases)]


class DoubleAgent:
    """`filing_port.FilingAgent`'s surface, satisfied structurally, with no network, key or
    framework import. `cost_usd` stays `0.0`: an offline pass really does spend nothing."""

    # The EXPLORING shape: the double WRITES the page through `agent.confined_write`, which is what
    # makes the offline suite prove something about the production write path.
    structured_ordinary = False

    # No gathered context. A real declaration, so `processing`'s branch runs in BOTH states.
    wants_gathered = False

    def __init__(self, settings):
        self.settings = settings

    def run(self, *, worktree: str, material: str, hints: dict, submitted_by: str,
            corrective: str = "", flow_note: str = "", gathered: str = "",
            acl: list[str] | None = None) -> AgentRun:
        # `flow_note`/`gathered`/`acl` are accepted and unused: the signature answers the PORT,
        # and this backend holds no read tool for `acl` to scope.
        directives = _directives(material)
        findings = _findings(material)
        run = AgentRun(turns=1, tool_calls=3)

        # ── the filing path ──────────────────────────────────────────────────────────────
        page_type = directives.get("type") or "note"
        # Any type the placement table lacks lands in the default folder and is judged by the
        # gates from there — the double carries no second vocabulary of its own.
        folder = page_policy.FOLDER_BY_TYPE.get(page_type, "wiki/notes")
        title = self._title(material)
        page_path = f"{folder}/{title}.md"

        # ── the proposing paths: the account DECLARES, `librarian.identity` creates ─────────
        new_entities, new_aliases = [], []
        anchor_entity = self._registry_entity(worktree)
        if "propose" in directives:
            names = [n.strip() for n in directives["propose"].split(",") if n.strip()]
            for spec in names:
                name, _, entity_type = spec.partition("|")
                new_entities.append(self._proposed_entity(name.strip(), entity_type.strip(),
                                                          note_title=title))
            if new_entities:
                anchor_entity = new_entities[0]["name"]
        if "propose-collides" in directives:
            # The registered entity under a legal form the collision fold catches. On the retry the
            # brief says to anchor to the registered id, which is what a real agent would do.
            registered = self._registry_entity(worktree)
            if not corrective:
                new_entities.append(self._proposed_entity(f"{registered} S.L.", "organization",
                                                          note_title=title))
                anchor_entity = f"{registered} S.L."
            else:
                anchor_entity = registered
        if "propose-unnamed" in directives:
            new_entities.append(self._proposed_entity("Nobody Mentioned This", "organization",
                                                      note_title=title))
        if directives.get("alias"):
            new_aliases.append({"entity": anchor_entity, "alias": directives["alias"]})
        entity_updates = []
        if directives.get("update"):
            # Facts the note establishes about a REGISTERED entity, appended to its page.
            entity_updates.append({
                "entity": directives["update"],
                "facts": [f"Established by the capture filed as {title}",
                          "Appended by the offline double, never a rewrite"],
                "connections": [f"[[{title}]] — the note that established it"]})
        company = "company" in directives
        anchoring = ({"kind": "company", "entities": [],
                      "reason": "a practice that applies across the whole company, "
                                "not to one client or product"}
                     if company else
                     {"kind": "entity", "entities": [anchor_entity], "reason": ""})

        hallucinate = ("hallucinate" in directives
                       or ("hallucinate-once" in directives and not corrective))
        self._write_page(worktree, page_path, title=title, page_type=page_type,
                         material=material, anchor=None if company else anchor_entity,
                         hallucinate=hallucinate,
                         canonical="canonical" in directives, forge="forge" in directives)

        # ── the overlap case: DECLARED, never performed ──────────────────────────────────
        # Only new files, like the real agent; a double that edited leaves `edits.apply` untested.
        overlaps, declared_edits = [], []
        if "overlap" in directives and directives["overlap"]:
            other = directives["overlap"]
            note = "covers the same ground; the newer page adds what the capture carried"
            overlaps.append({"path": other, "note": note})
            declared_edits.append({"path": other, "kind": "overlap", "link": title, "note": note})
        if "backlink" in directives and directives["backlink"]:
            declared_edits.append({"path": directives["backlink"], "kind": "backlink",
                                   "link": title, "note": ""})
        if "contradict" in directives and directives["contradict"]:
            declared_edits.append({
                "path": directives["contradict"], "kind": "contradiction", "link": title,
                "note": "the capture states the opposite of what this page records"})
        # An edit to a page that is not there, so `edits.validate` is exercised.
        if "bad-edit" in directives:
            declared_edits.append({"path": directives["bad-edit"] or "ops/identities.json",
                                   "kind": "backlink", "link": title, "note": ""})

        if "escape" in directives:
            # Deliberately unconfined: writing outside the lane is what the zone gate refuses.
            # The identity ROSTER — a real ops file, and the sharpest thing an out-of-lane
            # write could name: an agent that could edit it could grant itself an
            # audience. `ops/acl.json` used to stand here and no longer exists, so the
            # sabotage was naming a path nothing could ever have written to.
            self._write_unconfined(worktree, "ops/identities.json", '{}\n')
        if "delete" in directives:
            self._delete_some_page(worktree, keep=page_path)
        if "rewrite" in directives:
            self._rewrite_some_page(worktree, keep=page_path)

        outcome = {
            "decision": "file",
            "page_path": page_path,
            "page_type": page_type,
            "title": title,
            "anchoring": anchoring,
            "links_created": [] if company else [anchor_entity],
            "overlaps": overlaps,
            "edits": declared_edits,
            "findings": [{"category": c} for c in findings],
            "new_entities": new_entities,
            "new_aliases": new_aliases,
            "entity_updates": entity_updates,
            "summary": f"filed the capture as a {page_type}",
        }
        if "long-summary" in directives:
            # Digit-free on purpose, like `_FILLER`: a numeral reads as an asserted figure.
            outcome["summary"] = ("a very long account of what was filed and why it went there, "
                                 * 200)
        if "bad-shape" in directives or ("bad-shape-once" in directives and not corrective):
            # `_park` parses through the REAL boundary, so this raises where a backend would.
            outcome["decision"] = "publish"
        if "no-outcome" in directives:
            return run
        return self._account(worktree, run, outcome)

    @staticmethod
    def _proposed_entity(name: str, entity_type: str, *, note_title: str) -> dict:
        """A complete proposal, every field filled, the way the brief asks a real agent to write
        one. Digit-free like `_FILLER`: a numeral in a fact reads as an asserted figure."""
        return {
            "name": name,
            "entity_type": entity_type or "organization",
            "role": "the thing the captured note is about, proposed by the offline double",
            "aliases": [f"{name} (double alias)"],
            "summary": (f"{name} is an entity the captured material names and the registry did "
                        f"not know; the offline double proposes it with every field filled."),
            "facts": [f"Named in the capture filed as {note_title}",
                      "Introduced by the capture that named it"],
            "connections": [f"[[{note_title}]] — the note that introduced it"],
        }

    # ── the meeting flow's own directives — CONTENT only, never a page write ─────────────────
    # No declared-vs-written mismatch directives: the agent's one legal write here is its outcome
    # file, and code authors every page.
    #
    #   DOUBLE:decisions=<n>              how many decisions to distil (default 2)
    #   DOUBLE:meeting-hallucinate        unsupported figure in the first decision BODY, every pass
    #   DOUBLE:meeting-hallucinate-once   the same, first pass only; an honest body on the retry
    #   DOUBLE:meeting-hallucinate-last   the figure in the LAST decision, so a check that reads
    #                                     only the first page is distinguishable
    #   DOUBLE:meeting-hallucinate-meeting-page  the figure in `meeting_notes` instead
    #   DOUBLE:meeting-propose=a,b        the decisions anchor to entities the registry lacks:
    #                                     PROPOSE them, one per decision, cycling
    #   DOUBLE:meeting-anchor=<name>      declare this entity on every decision — a complete
    #                                     outcome the registry cannot resolve, so it is vetoed
    #   DOUBLE:meeting-company[=n]        the nth decision (1-indexed) anchors company-wide, so a
    #                                     set carries both scopes at once
    #   DOUBLE:meeting-body-date-link     a decision BODY links a date-bearing stem, against the
    #                                     brief's body-stays-digit-free convention
    #   DOUBLE:meeting-collide            the first decision's title slugifies onto an EXISTING
    #                                     fixture page, exercising the collision precheck
    #   DOUBLE:meeting-backlink=<path>    file, and DECLARE a reciprocal `related:` link on an
    #                                     existing page — this flow's own edit mechanism
    #   DOUBLE:meeting-overlap=<path>     the same, plus an overlap callout carrying a note
    #   DOUBLE:meeting-bad-edit[=<path>]  declare an edit code must refuse (outside the editable
    #                                     folders by default, or the named page)
    #
    # A planted secret needs no directive: the transcript reaches the SOURCE page verbatim, so the
    # pre-agent scan catches it before any agent runs.
    def run_meeting(self, *, worktree: str, material: str, meeting_meta: dict, registry,
                    source_page_path: str, corrective: str = "", gathered: str = "") -> AgentRun:
        # `gathered` is accepted and unused: the signature answers the PORT, and what the worker
        # gathered is a real model's input, not a scripted double's.
        directives = _directives(material)
        findings = _findings(material)
        run = AgentRun(turns=1, tool_calls=1)   # one Write call: its own outcome file

        title = meeting_meta.get("title") or self._title(material)

        entities_for_decisions = None    # None -> cycle the registry; set -> proposed names
        new_entities = []
        if "meeting-propose" in directives:
            entities_for_decisions = [n.strip() for n in directives["meeting-propose"].split(",")
                                      if n.strip()] or ["An Unregistered Thing"]

        if entities_for_decisions is None:
            registry_entities = self._registry_entities(worktree)
            n_decisions = int(directives.get("decisions") or "2")
            entities_for_decisions = [registry_entities[i % len(registry_entities)]
                                      if registry_entities else "Stigmergy"
                                      for i in range(n_decisions)]
        # Every decision DECLARES a name the registry lacks — a complete `file` outcome
        # `gate_anchoring` vetoes. The BODY still wikilinks a REGISTERED entity, or a second
        # `dead_links` veto stops `processing._refuse_meeting` from parking.
        anchor_link = None
        if "meeting-anchor" in directives:
            declared = (directives.get("meeting-anchor") or "").strip() or "An Unregistered Thing"
            anchor_link = entities_for_decisions[0] if entities_for_decisions else None
            entities_for_decisions = [declared] * len(entities_for_decisions)
        hallucinate_first = "meeting-hallucinate" in directives or (
            "meeting-hallucinate-once" in directives and not corrective)
        # Every pass: the point is a veto that SURVIVES the retry, on a late page.
        hallucinate_last = "meeting-hallucinate-last" in directives
        # 1-indexed. `gate_anchoring` judges each decision page independently, so a company-wide
        # one coexists with entity-anchored siblings on purpose.
        company_at = None
        if "meeting-company" in directives:
            raw_index = directives.get("meeting-company") or "1"
            company_at = int(raw_index) if raw_index.strip() else 1

        # The SAME stem `processing._meeting_stem` computes, or `meeting-body-date-link` is a dead
        # link the linter refuses for an unrelated reason.
        date = meeting_meta.get("meeting_date") or "2026-07-29"
        meeting_stem = self._slug(f"{date}-{title}")
        if "meeting-propose" in directives:
            # The proposed page links the MEETING page by stem — the one page in this set whose
            # name the double can know before code slugifies the decisions' titles.
            new_entities = [self._proposed_entity(name, "organization", note_title=meeting_stem)
                            for name in entities_for_decisions]

        decisions = []
        n = len(entities_for_decisions)
        for i, entity in enumerate(entities_for_decisions):
            page_hallucinate = (hallucinate_first and i == 0) or (hallucinate_last and i == n - 1)
            company_here = company_at is not None and (i + 1) == company_at
            if i == 0 and "meeting-collide" in directives:
                # Slugifies onto an EXISTING fixture page: the COLLISION is the point.
                d_title = "A decision from a previous meeting"
            else:
                d_title = f"{title} — decision {i + 1}"
            body = self._decision_body(
                entity=anchor_link or entity, company=company_here, hallucinate=page_hallucinate,
                body_date_link="meeting-body-date-link" in directives and i == 0,
                meeting_stem=meeting_stem)
            anchoring = (
                {"kind": "company", "entities": [],
                 "reason": "applies to every customer this meeting touched, not one of them"}
                if company_here else
                {"kind": "entity", "entities": [entity], "reason": ""})
            decisions.append({"title": d_title, "body": body, "anchoring": anchoring})

        # ── the declared-edit case: DECLARED, never performed ─────────────────────────────
        # Only the outcome file is written here, like every other meeting directive — the WORKER
        # performs the edit (`edits.apply_declared`), so a double that edited a page itself would
        # leave that call untested.
        #
        # The link is the first decision's own TITLE, spelled exactly as the brief tells a real
        # agent to spell it — NOT the stem `_decision_stems` will slugify it into. A double that
        # named the stem would be predicting a filename the agent is never shown, and the
        # title→stem resolution in `processing._edits_with_resolved_links` (the thing that makes
        # a correct declaration land) would go unexercised by every offline test.
        declared_edits = []
        edit_link = decisions[0]["title"] if decisions else meeting_stem
        if directives.get("meeting-backlink"):
            declared_edits.append({"path": directives["meeting-backlink"], "kind": "backlink",
                                  "link": edit_link, "note": ""})
        if directives.get("meeting-overlap"):
            declared_edits.append({
                "path": directives["meeting-overlap"], "kind": "overlap", "link": edit_link,
                "note": "the same ground this meeting revisited, from the other side"})
        # An edit code must refuse, so `edits.validate` is exercised on this flow too.
        if "meeting-bad-edit" in directives:
            declared_edits.append({"path": directives["meeting-bad-edit"] or "ops/identities.json",
                                  "kind": "backlink", "link": edit_link, "note": ""})

        attendees = [a.strip() for a in (meeting_meta.get("attendees") or "").split(",")
                    if a.strip()]
        outcome = {
            "decision": "file",
            "meeting_title": title,
            "attendees": attendees,
            "meeting_notes": self._meeting_notes(
                hallucinate="meeting-hallucinate-meeting-page" in directives),
            "action_items": ([{"owner": attendees[0], "action": "follow up", "done": False}]
                            if attendees else []),
            "decisions": decisions,
            "edits": declared_edits,
            "findings": [{"category": c} for c in findings],
            "new_entities": new_entities,
            "summary": f"distilled {len(decisions)} decision(s) from the meeting",
        }
        return self._account_meeting(worktree, run, outcome)

    def _account_meeting(self, worktree, run, outcome):
        """`_account`'s meeting sibling — writes ONLY the outcome file, the agent's one legal write."""
        self._write(worktree, OUTCOME_FILENAME, json.dumps(outcome, indent=2) + "\n")
        run.outcome = self._priced_parse(run, parse_meeting_outcome, outcome)
        return run

    @staticmethod
    def _slug(text: str) -> str:
        """`processing._meeting_stem`'s slug, by calling the same function it calls: a double that
        predicts production's filename must not own its own idea of it."""
        return normalize.slugify(text or "")

    @classmethod
    def _registry_entities(cls, worktree: str) -> list[str]:
        return [e["name"] for e in cls._load_registry(worktree).entities.values()]

    @staticmethod
    def _decision_body(*, entity, company, hallucinate, body_date_link, meeting_stem):
        """CONTENT only — code builds frontmatter, filename and source wikilink. Padding is
        digit-free: any numeral reads as a figure the transcript asserted."""
        opening = ("This decision applies company-wide, not to one customer in this meeting."
                  if company else f"Decided about [[{entity}]] in this meeting.")
        lines = ["## Context", "", opening, "", "## Decision", ""]
        lines += _FILLER
        if hallucinate:
            lines += ["", "The meeting implies a figure of 9142 units, worth recording."]
        if body_date_link:
            # DELIBERATE violation: `gates.prose_written`'s numeric matcher ignores `[[...]]`.
            # `meeting_stem` must be THIS capture's meeting page or `dead_links` fires first.
            lines += ["", f"See the minutes at [[{meeting_stem}]] for the full discussion."]
        # `len(lines)`: the linter trims only leading/trailing blanks, so mid-body blanks count.
        while len(lines) < 32:
            lines.append("Additional context recorded from the meeting for future readers.")
        return "\n".join(lines)

    @staticmethod
    def _meeting_notes(*, hallucinate):
        """The meeting page's "## Notes" content — the only free text on it the agent drafts."""
        lines = list(_FILLER)
        if hallucinate:
            lines += ["", "The minutes record a figure of 4173 units nobody in the transcript "
                          "said."]
        while len(lines) < 20:
            lines.append("Additional minutes recorded from the meeting for future readers.")
        return "\n".join(lines)

    # ── helpers ──────────────────────────────────────────────────────────────────────────
    def _account(self, worktree, run, outcome):
        """Write the outcome file AND hand back the parsed object, through the same `parse_outcome`
        a real backend uses, so the double cannot produce a shape it would have refused."""
        self._write(worktree, OUTCOME_FILENAME, json.dumps(outcome, indent=2) + "\n")
        run.outcome = self._priced_parse(run, parse_outcome, outcome)
        return run

    @staticmethod
    def _priced_parse(run, parse, outcome):
        """The parse, priced like every backend's: a refusal must leave `run_cost_usd` attached, or
        `processing` cannot tell "nothing was spent" from "nobody attached it"."""
        try:
            return parse(outcome)
        except AgentError as ex:
            priced(run, ex)
            raise

    def _write(self, worktree: str, rel: str, text: str) -> None:
        """A write the REAL agent would be permitted, through the very rule that permits it. Loud
        on denial: a silent skip would surface three gates later as "the agent wrote nothing"."""
        if not confined_write(worktree, rel, existing=gitcmd.tracked_paths(worktree)):
            raise WorktreeError(
                f"the offline double tried to write {rel}, which agent.confined_write denies — the "
                f"real agent's `write_page` tool would have refused it too. Either the fixture "
                f"names a page that already exists (case- and normalization-insensitively) or the "
                f"confinement rule changed shape")
        self._write_unconfined(worktree, rel, text)

    @staticmethod
    def _write_unconfined(worktree: str, rel: str, text: str) -> None:
        """A write the confinement rule DENIES. Named so the bypass is visible at its call site."""
        path = os.path.join(worktree, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def _title(material: str) -> str:
        """A filename-safe title from the material's first real line, accents intact. If nothing
        survives, a generic name rather than an approximation of somebody's words."""
        for line in (material or "").splitlines():
            text = line.strip().lstrip("#").strip()
            if text and not text.startswith("DOUBLE:") and not text.startswith("---"):
                cleaned = _UNSAFE_IN_TITLE.sub(" ", text).strip()
                if cleaned:
                    return " ".join(cleaned.split()[:6])[:60] or "Captured Note"
        return "Captured Note"

    @staticmethod
    def _load_registry(worktree: str):
        return registry_module.load_registry(
            os.path.join(worktree, "ops", registry_module.REGISTRY_FILE))

    @classmethod
    def _registry_entity(cls, worktree: str) -> str:
        """A name the registry resolves, read from the worktree rather than hardcoded."""
        for entity in cls._load_registry(worktree).entities.values():
            return entity["name"]
        return "Stigmergy"

    def _write_page(self, worktree, page_path, *, title, page_type, material, anchor,
                    hallucinate, canonical, forge):
        today = "2026-07-26"
        front = [
            f"type: {page_type}",
            f'title: "{title}"',
            f"status: {'canonical' if canonical else 'developing'}",
            f"created: {today}",
            f"updated: {today}",
            f"tags: [{page_type}]",
            f'related: ["[[{anchor}]]"]' if anchor else "related: []",
            "sources: []",
        ]
        if forge:
            # Asserts what only the server may compute; every one must be gone on the filed page.
            front += ['submitted_by: someone.else@example.com', "verification: verified",
                      'acl: ["leadership"]', 'content_hash: "sha256:deadbeef"',
                      "owner: someone.else"]
        body = [f"# {title}", ""]
        body += _FILLER
        body.append("")
        if anchor:
            body.append(f"This material is about [[{anchor}]].")
        else:
            body.append("This material applies company-wide rather than to one entity.")
        body.append("")
        body.append("## What the capture said")
        body.append("")
        # The material verbatim — this is how a seeded secret reaches the diff.
        body += [line for line in (material or "").splitlines()
                 if not line.startswith("DOUBLE:")]
        body.append("")
        if hallucinate:
            body.append("The capture implies a figure of 4823 units, which is worth recording.")
            body.append("")
        body.append("## Why it is here")
        body.append("")
        body += _FILLER
        body.append("")
        body.append("## Connections")
        body.append("")
        body.append(f"- [[{anchor}]] — the entity this material belongs to" if anchor
                    else "- applies across the company; no single entity owns it")
        # Pads to the linter's 30-line minimum, DIGIT-FREE: a numeral would be an unsupported
        # figure, making the double misbehave on ordinary content.
        while len([line for line in body if line.strip()]) < 32:
            body.append("Additional context recorded from the capture for future readers.")
        self._write(worktree, page_path,
                    "---\n" + "\n".join(front) + "\n---\n\n" + "\n".join(body) + "\n")

    @staticmethod
    def _some_existing_page(worktree, keep) -> str:
        for root, _, files in os.walk(os.path.join(worktree, "wiki")):
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(root, name), worktree)
                if name.endswith(".md") and rel != keep:
                    return rel
        return ""

    def _delete_some_page(self, worktree, keep):
        rel = self._some_existing_page(worktree, keep)
        if rel:
            os.remove(os.path.join(worktree, rel))

    def _rewrite_some_page(self, worktree, keep):
        rel = self._some_existing_page(worktree, keep)
        if not rel:
            return
        path = os.path.join(worktree, rel)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        # Remove a real body line: an additive edit only adds, so this is what the gate catches.
        lines = [line for line in text.splitlines()]
        for index, line in enumerate(lines):
            if line.strip() and not line.startswith(("---", "#", "type:", "title:")):
                del lines[index]
                break
        # Unconfined by design: `confined_write` denies this and `gate_body_rewrite` refuses it.
        self._write_unconfined(worktree, rel, "\n".join(lines) + "\n")
