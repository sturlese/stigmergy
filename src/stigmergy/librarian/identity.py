"""Births: the entities and spellings a filing CREATES so the page can land, confirmed by the
person whose capture it was.

A capture about a name the registry does not know used to stop here — parked on a question to a
person who had already left, and then filed with the identity waiting on somebody else. It does
neither any more (ADR 044): **the capture is the approval.** The agent declares the identity it
read out of the material (`Outcome.new_entities`: name, type, role, aliases, the "What / Who"
paragraph, the facts, the connections) and CODE does the rest in the same commit as the page:
validates it through the birth gate every entity passes (`entities.birth.prepare` — forbidden
characters, the collision fold, a page already on disk), renders it through the knowledge repo's
own template with `approved_by` naming the SUBMITTER, writes it into `wiki/entities/`, and
regenerates `ops/entity-registry.json` so the new id resolves for the anchoring gate and for every
later capture. A spelling the material uses for a REGISTERED entity (`Outcome.new_aliases`) is the
same idea one size down: appended to that entity's `aliases:` and regenerated into the registry.

**The agent judges; code writes and vetoes.** Nothing the account says becomes a file without
passing `birth`'s gate, and nothing it writes escapes the diff the gates judge: the lane is widened
by exactly `wiki/entities/` and the registry file for this run, `gates.gate_identity` proves every
entity-zone entry is one this module wrote and that it says who introduced it, and the registry and
every edited page are byte-proven (`GateContext.expected_bytes`). Two honesty checks keep a model
from inventing an identity: a declared name must be NAMED in the material (or in the submitter's
hints), and it must not collide with a registered spelling (then the material is ABOUT that entity
— anchor there). Each refusal is a `Finding` the corrective retry can act on, never a park.

What a wrong identity costs, and what pays it: nothing here is reviewed before it lands, so a
second identity for one thing is found afterwards — the gardener's identity pass judges every
registered pair and the repair that follows merges them, re-anchoring every page.
"""
import json
import os
from dataclasses import dataclass, field

from stigmergy.entities import birth, generator
from stigmergy.entities.errors import CollisionError, EntityError
from stigmergy.kernel import registry as registry_module
from stigmergy.kernel.registry import Registry
from stigmergy.librarian import gates, gather
from stigmergy.librarian import page as page_policy

GATE = "identity"

# What a drift refusal says instead of a command: the two sides are both derived, and a person
# hand-editing either is what put them out of step.
FIX_HINT = ("the entity pages and the registry are put back in step by an operator, in the "
            "knowledge repo, before this capture can file")

# Repo-relative, slash-separated. `entities.generator` owns the knowledge repo's identity layout;
# this module reads it from there rather than spelling a second copy.
ENTITY_ZONE_PREFIX = f"{generator.ENTITIES_RELDIR}/"
REGISTRY_RELPATH = generator.REGISTRY_RELPATH
# The entity page's own list of spellings. A spelling this run adds goes straight onto it.
ALIASES_KEY = "aliases"
TEMPLATE_RELPATH = generator.TEMPLATE_RELPATH


@dataclass
class Births:
    """What this run created, and the facts the gates and the report need about it.

    `registry` is the registry the commit will PUBLISH — the base one plus every entity this run
    created — and it is what `gate_anchoring` resolves against, so a page anchored to an entity born
    in the same commit resolves exactly as one anchored to an old entity does.
    """
    registry: Registry
    entity_pages: dict = field(default_factory=dict)    # created path -> canonical id
    alias_pages: dict = field(default_factory=dict)     # modified path -> [(canonical id, alias)]
    expected_bytes: dict = field(default_factory=dict)  # path -> the whole file, byte-proven
    entities: list = field(default_factory=list)        # [{"id", "name", "type", "confirmed_by"}] for the report
    aliases: list = field(default_factory=list)         # [{"entity", "alias"}] for the report
    # created path -> the person who introduced the identity (`approved_by`), told to the gates so
    # gate_identity can prove the page names exactly them. Every created page is in here: an
    # identity is born confirmed by whoever captured (ADR 044).
    confirmed: dict = field(default_factory=dict)
    # ADR 042: the registered entity pages this filing ADDED facts or connections to — modified
    # path -> canonical id — and the counts the report reads. Appended lines only, byte-proven.
    updated_pages: dict = field(default_factory=dict)
    updates: list = field(default_factory=list)         # [{"entity", "facts", "connections"}]
    # ADR 045 D6: what a RESTRICTED capture was not allowed to put on the open entity zone —
    # `[{"entity", "facts", "connections"}]` counted, never quoted. An entity page is the brain's
    # shared vocabulary and carries no audience, so a capture filed at a group may create an
    # identity and say what it IS, and may not publish what its own material established. Reported
    # rather than silently dropped: the person who captured is the one who can decide whether the
    # fact belongs on an open page, and they can only decide it if they are told.
    withheld: list = field(default_factory=list)

    @property
    def confirmed_ids(self) -> list:
        return [self.entity_pages[path] for path in self.confirmed if path in self.entity_pages]

    def touched(self) -> bool:
        return bool(self.entity_pages or self.alias_pages or self.updated_pages)

    @property
    def lane(self) -> tuple[str, ...]:
        """The write prefixes this run's births need beyond the fast lane's own — nothing when
        nothing was created, so a capture that introduces no identity is judged exactly as before."""
        return (ENTITY_ZONE_PREFIX, REGISTRY_RELPATH) if self.touched() else ()

    @property
    def derived_files(self) -> frozenset:
        return frozenset({REGISTRY_RELPATH}) if self.touched() else frozenset()


def write_births(worktree: str, *, outcome, base_registry: Registry, material: str,
                 hints: dict | None, today: str, related=(),
                 registration=None, approver: str = "",
                 acl: list[str] | None) -> "Births | list[gates.Finding]":
    """Create every entity and every alias the account declares, in `worktree`, and return the
    facts about them — or the findings that refuse the account, having written nothing.

    `approver` is the capture's submitter, and every entity created here is born confirmed by
    them (ADR 044). `registration` is what that person asked this capture to register, read off its
    hints by the caller: the entity under the registered name is created under exactly that name
    and type, and an account that declares no such entity while the registry lacks it is refused
    with a brief, so the corrective retry does what they asked.

    **`acl` is the capture's audience, and a non-`None` one narrows what may be WRITTEN here
    rather than labelling anything** ([ADR 045](../../../docs/decisions/045-audience-from-the-door.md)
    D6). An entity page never carries an audience: the registry is the brain's shared vocabulary,
    and a corpus where the same customer exists under three spellings because one of them was
    hidden is the failure this system was built to prevent. So a restricted capture may introduce
    an identity — its name, its type, its aliases and one sentence of What / Who — and its `facts`
    and `connections` are dropped, because those are the restricted material's and belong on the
    restricted page anchored to the entity. `entity_updates` are dropped whole for the same
    reason: the spine is only ever written from open material. Both are counted into
    `Births.withheld` and reach the submitter's report.

    The one sentence that DOES cross is deliberate and is the smallest thing that can: ADR 042 D3
    refuses to write an entity page with no What / Who at all, so the alternative to a model
    sentence about what the entity is would be no identity — and then the same name introduced
    again next week as a second entity. Structure is code's, prose is the model's, and the proof
    is the human's: the report shows the submitter the sentence that was born open.

    All-or-nothing like `edits.apply_declared`: the findings are collected over every declaration
    so the single corrective brief names all of them, and the worktree is left untouched when any
    one is refused (the caller's retry resets the tree anyway; this keeps a half-written set out of
    the diff the final refusal preserves).
    """
    new_entities = list(getattr(outcome, "new_entities", ()) or ())
    new_aliases = list(getattr(outcome, "new_aliases", ()) or ())
    entity_updates = list(getattr(outcome, "entity_updates", ()) or ())
    registered_id = (generator.canonical_id_for(registration.name)
                     if registration is not None and registration.name else "")
    if not new_entities and not new_aliases and not entity_updates:
        if registered_id and not base_registry.canonical_id(registration.name):
            return [_registration_missing(registration)]
        return Births(registry=base_registry)

    drift = generator.check(worktree).divergences
    if drift:
        # Each divergence ends in `generator.FIX_COMMAND`; the finding says the fix once, in its
        # own words, so the trim is off that constant rather than off a phrase copied from it.
        listed = "; ".join(d.message.split(f" — {generator.FIX_COMMAND}")[0] for d in drift[:3])
        return [gates.Finding(
            GATE, "drift",
            f"the knowledge repo's {REGISTRY_RELPATH} and {ENTITY_ZONE_PREFIX} already disagree "
            f"at this capture's base commit ({listed}), so an identity born here would be "
            f"regenerated into a registry this commit was not meant to rewrite — {FIX_HINT}",
            locator=REGISTRY_RELPATH, repairable=False)]

    existing = generator.read_entity_pages(worktree)
    existing_paths = [e.relpath for e in existing]
    page_by_id = {e.canonical_id: e.relpath for e in existing}
    # The working registry grows as entities are accepted, so the second declaration of one name
    # collides with the first, and an alias may name an entity born three lines up.
    working = generator.registry_of(existing)
    haystack = _haystack(material, hints)
    template = _template(worktree)
    if template is None:
        return [gates.Finding(
            GATE, "no-template",
            f"the knowledge repo carries no {TEMPLATE_RELPATH} at this capture's base commit, and "
            f"a new entity's page is that template with its identity filled in — commit the "
            f"template to the knowledge repo",
            locator=TEMPLATE_RELPATH, repairable=False)]

    findings: list[gates.Finding] = []
    planned_pages: dict[str, str] = {}          # path -> text, written only if nothing refused
    births = Births(registry=working)

    for declared in new_entities:
        name = str(declared.get("name") or "").strip()
        aliases = [str(a) for a in (declared.get("aliases") or ()) if str(a).strip()]
        cid = generator.canonical_id_for(name)
        if not _named(haystack, name) and not any(_named(haystack, a) for a in aliases):
            findings.append(gates.Finding(
                GATE, "unnamed-in-material",
                f"declared the entity {name!r}, which the captured material never names (nor do "
                f"the submitter's hints): an entity is introduced because the material is ABOUT "
                f"it, under a spelling that appears in it",
                locator=name,
                brief=f"`new_entities` declares {name!r}, but neither the material nor the hints "
                      f"contain that spelling. Introduce only what the material names, spelled as "
                      f"it names it; if the material is about a registered entity, anchor to that "
                      f"id instead."))
            continue
        try:
            proposal = birth.prepare(
                canonical_id=cid, name=name, entity_type=declared.get("entity_type", ""),
                aliases=aliases, role=declared.get("role", ""), registry=working,
                existing_pages=existing_paths + list(planned_pages))
            declared_facts = tuple(declared.get("facts", ()) or ())
            declared_connections = tuple(declared.get("connections", ()) or ())
            # ONE local, governing BOTH halves of what this restricted birth may carry — the
            # body's Connections fallback and the frontmatter's `related:` — because they are the
            # same fact written twice and a second `if` is how one of them gets missed. It was:
            # the fallback was dropped and `related` went to the renderer anyway, so the OPEN
            # entity page published the restricted page's title in its frontmatter. A title is
            # the whole of what a link leaks; a meeting would have published every decision's.
            page_related = related if acl is None else ()
            if acl is None:
                body = birth.prepare_body(
                    summary=declared.get("summary", ""), facts=declared_facts,
                    # The agent's own connections, or the page this entity was introduced from: a
                    # template stub is a placeholder, and a newborn page is a finished page.
                    connections=(declared_connections
                                 or [f"[[{name_of}]] — the page this entity was introduced from"
                                     for name_of in related]))
            else:
                # Identity and What / Who, and nothing else (D6). The body's Connections
                # fallback and the frontmatter `related:` both go with it (`page_related` above):
                # either would link the OPEN entity page to the restricted page this capture
                # filed — an upward link, written by the system itself, on the one page D6 keeps
                # open on purpose. An entity page is `ORPHAN_EXEMPT_TYPES` in the gardener, so an
                # empty `related:` costs no orphan finding; there is nothing to trade here.
                body = birth.prepare_body(summary=declared.get("summary", ""))
                if declared_facts or declared_connections:
                    births.withheld.append({"entity": cid, "facts": len(declared_facts),
                                            "connections": len(declared_connections)})
        except CollisionError as ex:
            hit = working.collision_id(name) or next(
                (working.collision_id(a) for a in aliases if working.collision_id(a)), "")
            findings.append(gates.Finding(
                GATE, "collides", f"declared {name!r}, but {ex}", locator=name,
                brief=f"`new_entities` declares {name!r}, and the registry already resolves that "
                      f"spelling to {hit!r}. Do not introduce it: the material is about that entity, "
                      f"so anchor to the id {hit!r} — and if the material spells it differently "
                      f"from the registry, put that spelling in `new_aliases` for {hit!r}."))
            continue
        except EntityError as ex:
            findings.append(gates.Finding(
                GATE, "invalid", f"declared {name!r}, but {ex}", locator=name,
                brief=f"`new_entities` entry {name!r} is not a valid identity: {ex}"))
            continue
        # EVERY identity is born confirmed by the person whose capture introduced it — a
        # registration and an ordinary capture alike (ADR 044 D1). The approver is the submitter,
        # resolved by the server, never anything the account said.
        text = birth.render_page(template, proposal, today=today, approved_by=approver,
                                 body=body, related=page_related)
        planned_pages[proposal.relpath] = text
        entry = registry_module.entry(proposal.name, proposal.entity_type, proposal.aliases,
                                      approved_by=approver)
        working.entities[proposal.canonical_id] = entry
        registry_module.index_entity(working, proposal.canonical_id, entry)
        page_by_id[proposal.canonical_id] = proposal.relpath
        births.entity_pages[proposal.relpath] = proposal.canonical_id
        births.confirmed[proposal.relpath] = approver
        births.entities.append({"id": proposal.canonical_id, "name": proposal.name,
                                "type": proposal.entity_type, "confirmed_by": approver})

    edited_texts: dict[str, str] = {}
    for declared in new_aliases:
        target, alias = str(declared.get("entity") or "").strip(), str(declared.get("alias") or "")
        cid = working.canonical_id(target)
        if not cid:
            findings.append(gates.Finding(
                GATE, "unknown-entity",
                f"declared {alias!r} as an alias of {target!r}, which the registry does not "
                f"resolve to any entity",
                locator=alias,
                brief=f"`new_aliases` names {target!r} as the entity {alias!r} spells, but nothing "
                      f"registered resolves to {target!r}. Name a registered entity's id — or, if "
                      f"the thing is new, introduce it in `new_entities` with this spelling among "
                      f"its `aliases`."))
            continue
        if cid in births.entity_pages.values():
            findings.append(gates.Finding(
                GATE, "alias-of-new-entity",
                f"declared {alias!r} as an alias of {cid!r}, an entity this same account "
                f"introduces — its spellings belong in that entity's own `aliases`",
                locator=alias,
                brief=f"`new_aliases` adds {alias!r} to {cid!r}, which `new_entities` creates in "
                      f"this same account. Put the spelling in that entry's `aliases` instead."))
            continue
        try:
            cleaned = birth.clean_aliases([alias], name=working.title(cid) or cid)
        except EntityError as ex:
            findings.append(gates.Finding(GATE, "invalid", f"declared an alias, but {ex}",
                                          locator=alias, brief=f"`new_aliases`: {ex}"))
            continue
        if not cleaned:
            continue                   # the alias IS the entity's name — nothing to learn
        alias = cleaned[0]
        if not _named(haystack, alias):
            findings.append(gates.Finding(
                GATE, "unnamed-in-material",
                f"declared the alias {alias!r} for {cid!r}, which the captured material never "
                f"uses: a spelling is added because the material uses it",
                locator=alias,
                brief=f"`new_aliases` declares {alias!r}, a spelling the material does not "
                      f"contain. Add only the spellings the material actually uses."))
            continue
        other = working.collision_id(alias)
        if other and other != cid:
            findings.append(gates.Finding(
                GATE, "alias-collides",
                f"declared {alias!r} as an alias of {cid!r}, but that spelling already resolves "
                f"to {other!r}",
                locator=alias,
                brief=f"`new_aliases` declares {alias!r} for {cid!r}, and the registry already "
                      f"resolves that spelling to {other!r}. A spelling names one entity: drop the "
                      f"alias, and check which of the two the material is really about."))
            continue
        if working.canonical_id(alias) == cid:
            continue                   # already one of its spellings
        path = page_by_id.get(cid)
        text = edited_texts.get(path)
        if text is None:
            text = _read(worktree, path)
        if text is None:
            findings.append(gates.Finding(
                GATE, "unreadable-entity-page",
                f"the page of {cid!r} ({path}) could not be read at this capture's base commit, "
                f"so a spelling cannot be added to it",
                locator=path or cid, repairable=False))
            continue
        try:
            front, tail = page_policy.front_and_tail(text)
        except ValueError:
            findings.append(gates.Finding(
                GATE, "unreadable-entity-page",
                f"the page of {cid!r} ({path}) has no frontmatter block, so a spelling cannot be "
                f"added to it", locator=path, repairable=False))
            continue
        # Straight onto the entity's own `aliases:` — a spelling the material uses IS one of its
        # names (ADR 044 D1); there is no waiting list for it to sit on.
        values = page_policy.list_field_values(front, ALIASES_KEY)
        edited_texts[path] = page_policy.rebuild(
            page_policy.with_list_field(front, ALIASES_KEY, [*values, alias]), tail)
        entry = working.entities[cid]
        entry["aliases"] = [*entry.get("aliases", []), alias]
        registry_module.index_entity(working, cid, entry)
        births.alias_pages.setdefault(path, []).append((cid, alias))
        births.aliases.append({"entity": cid, "alias": alias})

    for declared in entity_updates:
        target = str(declared.get("entity") or "").strip()
        cid = working.canonical_id(target)
        facts = [str(line) for line in (declared.get("facts") or ()) if str(line).strip()]
        connections = [str(line) for line in (declared.get("connections") or ())
                       if str(line).strip()]
        if acl is not None:
            # The spine is only ever written from OPEN material (D6). This is not a refusal of the
            # account — the capture files, its own page carries the facts, and the entity keeps
            # its anchor — so it is counted and reported rather than turned into a finding the
            # corrective retry would spend an attempt on.
            births.withheld.append({"entity": cid or target, "facts": len(facts),
                                    "connections": len(connections)})
            continue
        if not cid:
            findings.append(gates.Finding(
                GATE, "update-unknown-entity",
                f"adds facts to {target!r}, which the registry does not resolve to any entity",
                locator=target,
                brief=f"`entity_updates` names {target!r}, and nothing registered resolves to it. "
                      f"Name a registered entity's id — or, if the thing is new, introduce it in "
                      f"`new_entities` with these facts as its own."))
            continue
        if cid in births.entity_pages.values():
            findings.append(gates.Finding(
                GATE, "update-of-new-entity",
                f"adds facts to {cid!r}, an entity this same account introduces — they belong in "
                f"that entity's own `facts` and `connections`",
                locator=target,
                brief=f"`entity_updates` adds to {cid!r}, which `new_entities` creates in this same "
                      f"account. Put the facts in that entry instead."))
            continue
        path = page_by_id.get(cid)
        text = edited_texts.get(path)
        if text is None:
            text = _read(worktree, path)
        if text is None:
            findings.append(gates.Finding(
                GATE, "unreadable-entity-page",
                f"the page of {cid!r} ({path}) could not be read at this capture's base commit, "
                f"so nothing can be added to it",
                locator=path or cid, repairable=False))
            continue
        try:
            body = birth.prepare_body(summary="-", facts=facts, connections=connections)
        except EntityError as ex:
            findings.append(gates.Finding(GATE, "invalid", f"an update, but {ex}", locator=target,
                                          brief=f"`entity_updates`: {ex}"))
            continue
        appended = _append_to_sections(text, {
            birth.FACTS_SECTION: [f"- {fact}" for fact in body.facts],
            birth.CONNECTIONS_SECTION: [f"- {c}" for c in body.connections],
        }, today=today)
        if appended is None:
            continue                   # every line was already on the page — nothing to learn
        new_text, counts = appended
        edited_texts[path] = new_text
        births.updated_pages[path] = cid
        births.updates.append({"entity": cid, "facts": counts[birth.FACTS_SECTION],
                                  "connections": counts[birth.CONNECTIONS_SECTION]})

    if (registered_id and registered_id not in births.entity_pages.values()
            and not working.canonical_id(registration.name)):
        findings.append(_registration_missing(registration))
    if findings:
        return findings
    if not births.touched():
        # Nothing was WRITTEN — but something may have been WITHHELD (ADR 045 D6), and that is
        # exactly the case where the submitter has to be told: the page they got carries no sign
        # of it, so a report that dropped this would be a silent subtraction from their capture.
        return Births(registry=base_registry, withheld=births.withheld)

    for relpath, text in planned_pages.items():
        if not _write_new(worktree, relpath, text):
            return [gates.Finding(
                GATE, "outside-worktree",
                f"{relpath} does not resolve inside this capture's own checkout, or already "
                f"exists there — nothing was written",
                locator=relpath, repairable=False)]
    for relpath, text in edited_texts.items():
        full = os.path.join(worktree, *relpath.split("/"))
        with page_policy.open_for_rewrite(full) as f:
            f.write(text)
        births.expected_bytes[relpath] = text
    generator.regenerate(worktree)
    births.expected_bytes[REGISTRY_RELPATH] = _read(worktree, REGISTRY_RELPATH) or ""
    # Re-derived from the files as written, never trusted from the working copy above: this is
    # the registry the commit PUBLISHES, and the gates must resolve against exactly that.
    births.registry = generator.derive_registry(worktree)
    return births


def _append_to_sections(text: str, sections: dict, *, today: str):
    """Append each section's NEW lines to the page — under its `## Heading` when the page has it,
    as a new section at the end when it does not (a page born with nothing to say there has no
    heading) — and move `updated:` to today. A line the page already carries, whitespace folded,
    is not appended again. Returns `(text, {heading: appended})`, or `None` when nothing is new."""
    try:
        front, tail = page_policy.front_and_tail(text)
    except ValueError:
        return None
    present = {" ".join(line.split()) for line in tail.split("\n")}
    counts, lines = {}, tail.rstrip("\n").split("\n")
    for heading, candidates in sections.items():
        fresh = [c for c in candidates if " ".join(c.split()) not in present]
        counts[heading] = len(fresh)
        if not fresh:
            continue
        marker = f"## {heading}"
        if marker in lines:
            start = lines.index(marker) + 1
            end = start
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            while end > start and not lines[end - 1].strip():
                end -= 1
            lines[end:end] = fresh
        else:
            lines.extend(["", marker, "", *fresh])
        present.update(" ".join(c.split()) for c in fresh)
    if not any(counts.values()):
        return None
    new_tail = "\n".join(lines).rstrip("\n") + "\n"
    return page_policy.rebuild(page_policy.with_scalar_field(front, "updated", today), new_tail), counts


def _registration_missing(registration) -> "gates.Finding":
    """The capture asked for an entity to be registered and the account did neither of the two
    honest things: introduce it, or anchor to the registered entity it already is."""
    name, kind = registration.name, registration.entity_type or "its type"
    return gates.Finding(
        GATE, "registration-missing",
        f"this capture registers {name!r} ({kind}), and the account neither introduces it nor "
        f"resolves it to a registered entity",
        locator=name,
        brief=f"This capture REGISTERS {name!r} ({kind}). Introduce it in `new_entities` under "
              f"exactly that name and type, with `summary`, `facts` and `connections` written from "
              f"the material and from what the brain already holds about it — or, if the registry "
              f"already resolves that name, anchor to that entity and put their spelling in "
              f"`new_aliases`.")


def _haystack(material: str, hints: dict | None) -> str:
    """What a declared name has to appear in: the material, plus the submitter's hints — a hint
    naming the entity is the submitter saying what the capture is about, in their own words."""
    try:
        hint_text = json.dumps(hints or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        hint_text = ""
    return f"{material or ''}\n{hint_text}"


def _named(haystack: str, spelling: str) -> bool:
    return gather.mentions(haystack, spelling)


def _template(worktree: str) -> str | None:
    return _read(worktree, TEMPLATE_RELPATH)


def _read(worktree: str, relpath: str | None) -> str | None:
    if not relpath:
        return None
    try:
        with open(os.path.join(worktree, *relpath.split("/")), encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _write_new(worktree: str, relpath: str, text: str) -> bool:
    """One new page inside the checkout, through the hardened opener (`O_EXCL`, `O_NOFOLLOW`):
    the path was checked against the pages on disk by `birth.prepare`, and the opener makes that
    hold at the moment of writing."""
    if not page_policy.is_inside(worktree, relpath):
        return False
    full = os.path.join(worktree, *relpath.split("/"))
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with page_policy.open_for_new(full) as f:
            f.write(text)
    except OSError:
        return False
    return True
