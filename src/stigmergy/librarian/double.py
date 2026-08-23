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
    DOUBLE:overlap=<path>    file, and DECLARE that an existing page covers the same ground
    DOUBLE:refresh=<path>    file, and DECLARE that an existing page is brought up to date — the
                             account's `rewrites`, which the worker performs and the gates judge
    DOUBLE:no-outcome        write no outcome file at all
    DOUBLE:bad-shape         write an outcome whose SHAPE the boundary refuses, on EVERY attempt
    DOUBLE:bad-shape-once    write it on the first attempt and a good one on the corrective retry
    DOUBLE:long-summary      a summary past the prose ceiling — the benign twin: TRUNCATED and
                             filed, never refused

The declaring directives write NOTHING to another page, and the proposing directives write
NOTHING to `wiki/entities/`: the account declares, `librarian.identity` creates, which is the whole
production path the offline suite exists to exercise. Well-behaved
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

        # ── the overlap case: a JUDGMENT the account carries, never a write ─────────────
        # Only new files, like the real agent: the double never touches a page that already exists.
        overlaps = []
        if "overlap" in directives and directives["overlap"]:
            overlaps.append({
                "path": directives["overlap"],
                "note": "covers the same ground; the newer page adds what the capture carried"})
        # ── the rewrite case: DECLARED, never performed here ────────────────────────────
        # The one declaration that names somebody else's page. The double writes nothing to it:
        # `processing._apply_declared_rewrites` does, and the gates judge the diff.
        rewrites = []
        if "refresh" in directives and directives["refresh"]:
            target = directives["refresh"]
            rewrites.append({
                "path": target,
                "body": f"# {target.rsplit('/', 1)[-1].removesuffix('.md')}\n\n"
                        f"Brought up to date by a later capture; what it recorded before is in "
                        f"the history of this page.\n",
                "why": "a later capture established what this page had wrong"})

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
            "rewrites": rewrites,
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


    @staticmethod
    def _slug(text: str) -> str:
        """`processing._meeting_stem`'s slug, by calling the same function it calls: a double that
        predicts production's filename must not own its own idea of it."""
        return normalize.slugify(text or "")


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
