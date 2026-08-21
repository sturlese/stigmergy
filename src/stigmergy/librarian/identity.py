"""Proposals: the entities and spellings a filing CREATES so the page can land, for a steward to
confirm afterwards.

A capture about a name the registry does not know used to stop here — parked on a question to a
person who had already left. It does not stop any more. The agent declares the identity it read
out of the material (`Outcome.new_entities`: name, type, role, aliases, the "What / Who"
paragraph, the facts, the connections) and CODE does the rest in the same commit as the page:
validates it through the birth gate every entity passes (`entities.birth.prepare` — forbidden
characters, the collision fold, a page already on disk), renders it through the knowledge repo's
own template with `approved_by` EMPTY, writes it into `wiki/entities/`, and regenerates
`ops/entity-registry.json` so the new id resolves for the anchoring gate and for every later
capture. A spelling the material uses for a REGISTERED entity (`Outcome.new_aliases`) is the same
idea one size down: appended to that entity's `proposed_aliases:`, regenerated into the registry,
confirmed or declined later.

**The agent judges; code writes and vetoes.** Nothing the account says becomes a file without
passing `birth`'s gate, and nothing it writes escapes the diff the gates judge: the lane is widened
by exactly `wiki/entities/` and the registry file for this run, `gates.gate_identity` proves every
entity-zone entry is one this module wrote, and the registry and every edited page are byte-proven
(`GateContext.expected_bytes`). Three honesty checks keep a model from inventing an identity: a
proposed name must be NAMED in the material (or in the submitter's hints), it must not collide
with a registered spelling (then the material is ABOUT that entity — anchor there), and it must
not be an identity a steward already declined (the ledger remembers; the same name proposed again
would loop forever). Each refusal is a `Finding` the corrective retry can act on, never a park.
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

# Repo-relative, slash-separated. `entities.generator` owns the knowledge repo's identity layout;
# this module reads it from there rather than spelling a second copy.
ENTITY_ZONE_PREFIX = f"{generator.ENTITIES_RELDIR}/"
REGISTRY_RELPATH = generator.REGISTRY_RELPATH
TEMPLATE_RELPATH = generator.TEMPLATE_RELPATH


@dataclass
class Proposals:
    """What this run proposed, and the facts the gates and the report need about it.

    `registry` is the registry the commit will PUBLISH — the base one plus every proposal this run
    made — and it is what `gate_anchoring` resolves against, so a page anchored to an entity born
    in the same commit resolves exactly as one anchored to an old entity does.
    """
    registry: Registry
    entity_pages: dict = field(default_factory=dict)    # created path -> canonical id
    alias_pages: dict = field(default_factory=dict)     # modified path -> [(canonical id, alias)]
    expected_bytes: dict = field(default_factory=dict)  # path -> the whole file, byte-proven
    entities: list = field(default_factory=list)        # [{"id", "name", "type"}] for the report
    aliases: list = field(default_factory=list)         # [{"entity", "alias"}] for the report

    def touched(self) -> bool:
        return bool(self.entity_pages or self.alias_pages)

    @property
    def lane(self) -> tuple[str, ...]:
        """The write prefixes this run's proposals need beyond the fast lane's own — nothing when
        nothing was proposed, so a capture that proposes no identity is judged exactly as before."""
        return (ENTITY_ZONE_PREFIX, REGISTRY_RELPATH) if self.touched() else ()

    @property
    def derived_files(self) -> frozenset:
        return frozenset({REGISTRY_RELPATH}) if self.touched() else frozenset()


def write_proposals(worktree: str, *, outcome, base_registry: Registry, material: str,
                    hints: dict | None, declined_ids, today: str,
                    related=()) -> "Proposals | list[gates.Finding]":
    """Create every entity and every alias the account proposes, in `worktree`, and return the
    facts about them — or the findings that refuse the account, having written nothing.

    All-or-nothing like `edits.apply_declared`: the findings are collected over every declaration
    so the single corrective brief names all of them, and the worktree is left untouched when any
    one is refused (the caller's retry resets the tree anyway; this keeps a half-written set out of
    the diff the final refusal preserves).
    """
    new_entities = list(getattr(outcome, "new_entities", ()) or ())
    new_aliases = list(getattr(outcome, "new_aliases", ()) or ())
    if not new_entities and not new_aliases:
        return Proposals(registry=base_registry)

    drift = generator.check(worktree).divergences
    if drift:
        listed = "; ".join(d.message.split(" — run ")[0] for d in drift[:3])
        return [gates.Finding(
            GATE, "drift",
            f"the knowledge repo's {REGISTRY_RELPATH} and {ENTITY_ZONE_PREFIX} already disagree "
            f"at this capture's base commit ({listed}), so an identity proposed here would be "
            f"regenerated into a registry this commit was not meant to rewrite — a steward runs "
            f"`{generator.FIX_COMMAND}` in the knowledge repo first",
            locator=REGISTRY_RELPATH, repairable=False)]

    existing = generator.read_entity_pages(worktree)
    existing_paths = [e.relpath for e in existing]
    page_by_id = {e.canonical_id: e.relpath for e in existing}
    # The working registry grows as proposals are accepted, so the second proposal of one name
    # collides with the first, and an alias may name an entity born three lines up.
    working = generator.registry_of(existing)
    haystack = _haystack(material, hints)
    declined = {str(d) for d in (declined_ids or ())}
    template = _template(worktree)
    if template is None:
        return [gates.Finding(
            GATE, "no-template",
            f"the knowledge repo carries no {TEMPLATE_RELPATH} at this capture's base commit, and "
            f"a proposed entity's page is that template with its identity filled in — commit the "
            f"template to the knowledge repo",
            locator=TEMPLATE_RELPATH, repairable=False)]

    findings: list[gates.Finding] = []
    planned_pages: dict[str, str] = {}          # path -> text, written only if nothing refused
    proposals = Proposals(registry=working)

    for declared in new_entities:
        name = str(declared.get("name") or "").strip()
        aliases = [str(a) for a in (declared.get("aliases") or ()) if str(a).strip()]
        cid = generator.canonical_id_for(name)
        if not _named(haystack, name) and not any(_named(haystack, a) for a in aliases):
            findings.append(gates.Finding(
                GATE, "unnamed-in-material",
                f"proposed the entity {name!r}, which the captured material never names (nor do "
                f"the submitter's hints): an entity is proposed because the material is ABOUT it, "
                f"under a spelling that appears in it",
                locator=name,
                brief=f"`new_entities` proposes {name!r}, but neither the material nor the hints "
                      f"contain that spelling. Propose only what the material names, spelled as it "
                      f"names it; if the material is about a registered entity, anchor to that id "
                      f"instead."))
            continue
        if cid in declined:
            findings.append(gates.Finding(
                GATE, "declined",
                f"proposed the entity {name!r} ({cid}), which a steward already declined as an "
                f"identity — it is not to be proposed again",
                locator=name,
                brief=f"{name!r} was proposed before and a steward declined it. Do not propose it "
                      f"again: anchor the page to a registered entity it belongs to, or declare "
                      f"company-wide scope with a reason."))
            continue
        try:
            proposal = birth.prepare(
                canonical_id=cid, name=name, entity_type=declared.get("entity_type", ""),
                aliases=aliases, role=declared.get("role", ""), registry=working,
                existing_pages=existing_paths + list(planned_pages))
            body = birth.prepare_body(
                summary=declared.get("summary", ""), facts=declared.get("facts", ()),
                # The agent's own connections, or the page this entity was proposed from: a
                # template stub is a placeholder, and a proposed page is a finished page.
                connections=(declared.get("connections", ())
                             or [f"[[{name_of}]] — the page this entity was proposed from"
                                 for name_of in related]))
        except CollisionError as ex:
            hit = working.collision_id(name) or next(
                (working.collision_id(a) for a in aliases if working.collision_id(a)), "")
            findings.append(gates.Finding(
                GATE, "collides", f"proposed {name!r}, but {ex}", locator=name,
                brief=f"`new_entities` proposes {name!r}, and the registry already resolves that "
                      f"spelling to {hit!r}. Do not propose it: the material is about that entity, "
                      f"so anchor to the id {hit!r} — and if the material spells it differently "
                      f"from the registry, put that spelling in `new_aliases` for {hit!r}."))
            continue
        except EntityError as ex:
            findings.append(gates.Finding(
                GATE, "invalid", f"proposed {name!r}, but {ex}", locator=name,
                brief=f"`new_entities` entry {name!r} is not a valid identity: {ex}"))
            continue
        text = birth.render_page(template, proposal, today=today, approved_by="", body=body,
                                 related=related)
        planned_pages[proposal.relpath] = text
        entry = registry_module.entry(proposal.name, proposal.entity_type, proposal.aliases,
                                      proposed=True)
        working.entities[proposal.canonical_id] = entry
        registry_module.index_entity(working, proposal.canonical_id, entry)
        page_by_id[proposal.canonical_id] = proposal.relpath
        proposals.entity_pages[proposal.relpath] = proposal.canonical_id
        proposals.entities.append({"id": proposal.canonical_id, "name": proposal.name,
                                   "type": proposal.entity_type})

    edited_texts: dict[str, str] = {}
    for declared in new_aliases:
        target, alias = str(declared.get("entity") or "").strip(), str(declared.get("alias") or "")
        cid = working.canonical_id(target)
        if not cid:
            findings.append(gates.Finding(
                GATE, "unknown-entity",
                f"proposed {alias!r} as an alias of {target!r}, which the registry does not "
                f"resolve to any entity",
                locator=alias,
                brief=f"`new_aliases` names {target!r} as the entity {alias!r} spells, but nothing "
                      f"registered resolves to {target!r}. Name a registered entity's id — or, if "
                      f"the thing is new, propose it in `new_entities` with this spelling among its "
                      f"`aliases`."))
            continue
        if cid in proposals.entity_pages.values():
            findings.append(gates.Finding(
                GATE, "alias-of-new-entity",
                f"proposed {alias!r} as an alias of {cid!r}, an entity this same account proposes "
                f"— its spellings belong in that entity's own `aliases`",
                locator=alias,
                brief=f"`new_aliases` adds {alias!r} to {cid!r}, which `new_entities` creates in "
                      f"this same account. Put the spelling in that entry's `aliases` instead."))
            continue
        try:
            cleaned = birth.clean_aliases([alias], name=working.title(cid) or cid)
        except EntityError as ex:
            findings.append(gates.Finding(GATE, "invalid", f"proposed an alias, but {ex}",
                                          locator=alias, brief=f"`new_aliases`: {ex}"))
            continue
        if not cleaned:
            continue                   # the alias IS the entity's name — nothing to learn
        alias = cleaned[0]
        if not _named(haystack, alias):
            findings.append(gates.Finding(
                GATE, "unnamed-in-material",
                f"proposed the alias {alias!r} for {cid!r}, which the captured material never "
                f"uses: a spelling is proposed because the material uses it",
                locator=alias,
                brief=f"`new_aliases` proposes {alias!r}, a spelling the material does not "
                      f"contain. Propose only the spellings the material actually uses."))
            continue
        other = working.collision_id(alias)
        if other and other != cid:
            findings.append(gates.Finding(
                GATE, "alias-collides",
                f"proposed {alias!r} as an alias of {cid!r}, but that spelling already resolves to "
                f"{other!r}",
                locator=alias,
                brief=f"`new_aliases` proposes {alias!r} for {cid!r}, and the registry already "
                      f"resolves that spelling to {other!r}. A spelling names one entity: drop the "
                      f"alias, and check which of the two the material is really about."))
            continue
        if working.canonical_id(alias) == cid:
            continue                   # already one of its spellings, approved or proposed
        path = page_by_id.get(cid)
        text = edited_texts.get(path)
        if text is None:
            text = _read(worktree, path)
        if text is None:
            findings.append(gates.Finding(
                GATE, "unreadable-entity-page",
                f"the page of {cid!r} ({path}) could not be read at this capture's base commit, "
                f"so a spelling cannot be proposed on it",
                locator=path or cid, repairable=False))
            continue
        try:
            front, tail = page_policy.front_and_tail(text)
        except ValueError:
            findings.append(gates.Finding(
                GATE, "unreadable-entity-page",
                f"the page of {cid!r} ({path}) has no frontmatter block, so a spelling cannot be "
                f"proposed on it", locator=path, repairable=False))
            continue
        values = page_policy.list_field_values(front, generator.PROPOSED_ALIASES_KEY)
        edited_texts[path] = page_policy.rebuild(
            page_policy.with_list_field(front, generator.PROPOSED_ALIASES_KEY, [*values, alias]),
            tail)
        entry = working.entities[cid]
        entry[registry_module.PROPOSED_ALIASES_KEY] = [
            *entry.get(registry_module.PROPOSED_ALIASES_KEY, []), alias]
        registry_module.index_entity(working, cid, entry)
        proposals.alias_pages.setdefault(path, []).append((cid, alias))
        proposals.aliases.append({"entity": cid, "alias": alias})

    if findings:
        return findings
    if not proposals.touched():
        return Proposals(registry=base_registry)

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
        proposals.expected_bytes[relpath] = text
    generator.regenerate(worktree)
    proposals.expected_bytes[REGISTRY_RELPATH] = _read(worktree, REGISTRY_RELPATH) or ""
    # Re-derived from the files as written, never trusted from the working copy above: this is
    # the registry the commit PUBLISHES, and the gates must resolve against exactly that.
    proposals.registry = generator.derive_registry(worktree)
    return proposals


def _haystack(material: str, hints: dict | None) -> str:
    """What a proposed name has to appear in: the material, plus the submitter's hints — a hint
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
