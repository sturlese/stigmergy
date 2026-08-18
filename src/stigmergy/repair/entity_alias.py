"""The `entity-alias` kind: two registry entries turn out to be one entity, and one absorbs the
other.

A merge is a SUPERSESSION, never a removal. `wiki/entities/` is absent from every deletable set by
construction (ADR 016, ADR 039's second amendment): the absorbed page STAYS, marked
`superseded_by:` the survivor, demoted by `index.rank` exactly as any superseded page is, and still
readable — knowing that these two names were once two entities is the whole record of the decision.

**The model picks the survivor; code computes the sweep.** Which of two names is canonical is a
judgment — the legal name is often the less-used one, and a backlink count answers a different
question — so it belongs to an agent reading both pages, and its rationale is what a steward reads
beside Approve. Which pages change, and what bytes each ends up with, is authority: a model never
computes a file list (#72's deletion lesson, where an error is a wrong write).

One approval, one commit, four kinds of change:

  · the SURVIVOR's page gains the absorbed entity's spellings in its `aliases:` list;
  · the ABSORBED page loses those spellings (they are the survivor's now) and gains
    `superseded_by:` pointing at the survivor;
  · every page whose `entity:` list names the absorbed id is RE-ANCHORED to the survivor;
  · `ops/entity-registry.json` is regenerated — by `entities.generator`, never by this module.

**The one spelling a merge cannot move, and it is not a choice.** The knowledge repo's own contract
linter refuses an alias that names an existing page — `alias 'X' collides with page
wiki/entities/X.md` — because the wikilink namespace is keyed on page STEMS, and the absorbed
page's file is still there by governance. So the survivor claims the absorbed entity's ALIASES and
never its own name, and `_unclaimable` refuses such an alias at PLAN time with a sentence rather
than letting `gate_contract` veto it at apply time. The consequence is stated plainly in ADR 039's
third amendment and is not hidden here: the absorbed name keeps resolving to the absorbed page's
retired identity, which now says what it was merged into.

Three properties buy this kind its safety, and each is asked of a different thing — `deletion.py`'s
three, because this is the same shape of change:

  · **The zone is narrow and derived.** The lane covers the entity zone, the zones of the pages
    actually being re-anchored, and `ops/` — nothing else, and `validate` is what confines the ops
    inside it.
  · **The plan is a pure function of the bytes on disk.** Computed at propose time against the
    operator's checkout, RECOMPUTED at apply time against a fresh clone, and refused unless the two
    agree byte for byte. A corpus that moved under a proposal is a re-proposal.
  · **The registry is PREDICTED here and WRITTEN by the generator.** `plan` derives what
    `stigmergy-entities regenerate` will produce (through the generator's own reader and
    `kernel.registry.registry_text`, the one serializer); `apply_declared` writes the pages, runs
    the real `generator.regenerate`, and refuses unless the file it produced is byte-identical to
    the prediction. One writer of the registry, still.
"""
import dataclasses
import hashlib
import os

from stigmergy.entities import generator
from stigmergy.entities.errors import EntityError
from stigmergy.kernel.registry import Registry, registry_text
from stigmergy.librarian import gates
from stigmergy.librarian import page as page_policy
from stigmergy.repair import deletion, schema
from stigmergy.repair.errors import RepairError

OP_ALIAS = schema.ALIAS_OP_NAME
OP_RETIRE = schema.RETIRE_OP_NAME
OP_REANCHOR = schema.REANCHOR_OP_NAME
OP_REGISTRY = schema.REGISTRY_OP_NAME
OP_NAMES = (OP_ALIAS, OP_RETIRE, OP_REANCHOR, OP_REGISTRY)

# The zone an identity lives in and the file derived from it. Both spelled here rather than
# imported from `entities.generator`, the posture `entity_body` states one module over: this
# package talks to the knowledge repo through FILES, and the two constants are the shape of a
# checkout rather than that package's API. `generator.regenerate` IS imported, because it is the
# behaviour there must be only one of.
ENTITY_ZONE_PREFIX = "wiki/entities/"
REGISTRY_RELPATH = "ops/entity-registry.json"

# The frontmatter keys this kind writes, and the ONLY three.
ALIASES_KEY = "aliases"
SUPERSEDED_BY_KEY = "superseded_by"
ENTITY_KEY = "entity"


def _finding(code: str, message: str, locator: str = "") -> gates.Finding:
    return gates.Finding("entity-alias", code, message, locator=locator)


# ── reading one page's identity claim ─────────────────────────────────────────────────────────
def _read(worktree: str, rel: str) -> str | None:
    try:
        with open(os.path.join(worktree, *rel.split("/")), encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def page_stem(path: str) -> str:
    """The name a page is linkable BY — `deletion.page_stem`, reused rather than re-derived, so the
    two kinds cannot come to disagree about what a page is called."""
    return deletion.page_stem(path)


def entity_page_refusal(worktree: str, path: str) -> tuple[str, str]:
    """`(code, sentence)` for a path this kind may not treat as an entity page, `("", "")` when it
    may.

    ONE rule read by both ends, `deletion.target_refusal`'s posture: `plan` raises the sentence at
    the proposer, and `validate` turns the code into a finding the apply names in its refusal.
    """
    if not path.startswith(ENTITY_ZONE_PREFIX):
        return "outside-entity-zone", (
            f"{path} is not an entity page — a merge is a decision about two identities, and "
            f"identities live in {ENTITY_ZONE_PREFIX}")
    basename = path.rsplit("/", 1)[-1]
    if basename.startswith(".") or not basename.endswith(".md"):
        return "not-a-page", f"{path} is not a page: this kind touches `.md` files and never a dotfile"
    # Containment RESOLVED rather than inferred from the string: every check above is a shape
    # check, and a symlinked DIRECTORY component satisfies all of them.
    if not page_policy.is_inside(worktree, path):
        return "outside-worktree", f"{path} resolves outside the repo checkout"
    if os.path.islink(os.path.join(worktree, path)):
        return "symlinked-target", (
            f"{path} is a symlink and not a page — a merge would rewrite the thing it points at")
    if _read(worktree, path) is None:
        return "missing-target", f"{path} does not exist in the repo, or is not readable as text"
    return "", ""


# ── the two entity-page rewrites ──────────────────────────────────────────────────────────────
def _front_and_tail(text: str) -> tuple[list[str], str]:
    """`(frontmatter lines, everything from the closing fence onward)`.

    The tail is taken FROM THE FILE rather than reassembled, `deletion.scrubbed`'s rule: a page
    whose closing `---` has no newline after it must not gain one, because a page that gained a
    byte is a page in this merge's blast radius for a change nobody made.
    """
    front, rest = page_policy.split_frontmatter(text or "")
    if len(text or "") == len(rest):
        raise RepairError("this page has no `---` frontmatter block, so it declares no identity")
    head = (text or "")[:len(text or "") - len(rest)]
    return front.split("\n"), ("---" + ("\n" if head.endswith("\n") else "") + rest)


def _rebuilt(front_lines: list[str], tail: str) -> str:
    return "---\n" + "\n".join(front_lines) + "\n" + tail


def _list_values(front_lines: list[str], key: str) -> list[str]:
    """A frontmatter LIST field's current values, `[]` for an absent or unreadable one."""
    start, raw = page_policy.top_level_key_line(front_lines, key)
    if start < 0:
        return []
    inline = raw.strip()
    if inline:
        return page_policy.parse_list_value(inline)
    _start, end = page_policy.top_level_key_span(front_lines, key)
    return page_policy.parse_list_value(
        "\n".join(line.strip() for line in front_lines[start + 1:end] if line.strip()))


def _with_list(front_lines: list[str], key: str, values: list[str]) -> list[str]:
    """A frontmatter LIST field rewritten to exactly `values`, IN PLACE.

    Always a flow list, always on the field's own single line: this kind rewrites two fields whose
    shapes a steward never chose (the entity template writes `aliases: []`), and reproducing a
    block sequence's indentation would be a second opinion about a shape `librarian.page` already
    owns. A field the page does not declare is APPENDED at the end of the frontmatter, which is the
    one place a new line cannot land inside somebody else's block.
    """
    start, _raw = page_policy.top_level_key_line(front_lines, key)
    line = f"{key}: {page_policy.yaml_list(values)}"
    if start < 0:
        return [*front_lines, line]
    _start, end = page_policy.top_level_key_span(front_lines, key)
    return front_lines[:start] + [line] + front_lines[end:]


def _with_scalar(front_lines: list[str], key: str, value: str) -> list[str]:
    start, _raw = page_policy.top_level_key_line(front_lines, key)
    line = f"{key}: {page_policy.yaml_scalar(value)}"
    if start < 0:
        return [*front_lines, line]
    _start, end = page_policy.top_level_key_span(front_lines, key)
    return front_lines[:start] + [line] + front_lines[end:]


def aliased(text: str, aliases: list[str], *, absorbed_stem: str = "") -> str:
    """The SURVIVOR's page: its `aliases:` list rewritten, and a `related:` link to the identity it
    absorbed. Pure — no filesystem, so the property this kind rests on can be asserted about a
    string.

    The `related:` link is not decoration and it is why this function takes a stem at all. The
    absorbed page STAYS (governance retires an identity, it does not delete one), and a retired
    page nothing points at is a page nobody finds — the contract linter says so itself, with an
    `orphans` warning. It is also what guarantees the survivor's page CHANGES: the absorbed entity
    may declare no alias to move, and a merge whose survivor's page came out byte-identical would
    name a page in `target_paths` that the diff never touched.

    `librarian.page.with_related_link` does that half, never a second writer here: it is the one
    function that knows all three shapes a `related:` field can have, and a flow list and a block
    sequence must not start being rewritten two different ways.
    """
    if absorbed_stem:
        text, _changed = page_policy.with_related_link(text, absorbed_stem)
    front_lines, tail = _front_and_tail(text)
    return _rebuilt(_with_list(front_lines, ALIASES_KEY, aliases), tail)


def retired(text: str, survivor_stem: str) -> str:
    """The ABSORBED page: its `aliases:` emptied and `superseded_by:` pointing at the survivor.

    Emptied rather than left alone, and that is not tidiness: the knowledge repo's contract linter
    reports `alias 'X' already declared by <page>` when two pages claim one spelling, so a merge
    that copied the aliases without moving them would be vetoed at apply time by `gate_contract`.
    """
    front_lines, tail = _front_and_tail(text)
    front_lines = _with_list(front_lines, ALIASES_KEY, [])
    front_lines = _with_scalar(front_lines, SUPERSEDED_BY_KEY, f"[[{survivor_stem}]]")
    return _rebuilt(front_lines, tail)


def reanchored(text: str, *, absorbed_id: str, survivor_id: str) -> str:
    """One page's `entity:` list with the absorbed id replaced by the survivor's, IN PLACE.

    Order is preserved and every other id survives: a page anchored to two entities keeps both, and
    the survivor takes the absorbed one's POSITION rather than being appended, so a diff a person
    reads shows one value changing rather than a list being rewritten. A page already anchored to
    both loses the duplicate.
    """
    front_lines, tail = _front_and_tail(text)
    values = _list_values(front_lines, ENTITY_KEY)
    out: list[str] = []
    for value in values:
        replaced = survivor_id if str(value).strip() == absorbed_id else str(value)
        if replaced not in out:
            out.append(replaced)
    return _rebuilt(_with_list(front_lines, ENTITY_KEY, out), tail)


# ── which pages carry the absorbed identity ───────────────────────────────────────────────────
def anchored_paths(worktree: str, absorbed_id: str, *, excluding=()) -> list[str]:
    """Every corpus page whose `entity:` list names `absorbed_id`, sorted, minus `excluding`.

    The LITERAL id, never a registry fold: `entity:` holds canonical ids that the librarian
    resolved and stamped, and re-anchoring a page because its id happened to normalize onto this
    entity would move a page nobody's finding named.

    The walk is `deletion.corpus_pages` — the same wikilink-namespace walk, reused rather than
    re-derived, so the two non-additive kinds cannot disagree about which files are pages.
    """
    skip = {str(p) for p in excluding}
    out = []
    for rel in deletion.corpus_pages(worktree):
        if rel in skip:
            continue
        text = _read(worktree, rel)
        if text is None:
            continue
        front_lines = page_policy.frontmatter_lines(text)
        if not front_lines:
            continue
        if absorbed_id in [str(v).strip() for v in _list_values(front_lines, ENTITY_KEY)]:
            out.append(rel)
    return sorted(out)


# ── what the survivor may legally claim ───────────────────────────────────────────────────────
UNCLAIMABLE_ALIAS = (
    "{alias!r} cannot become an alias of {survivor}: {page} is a page of that name, and the "
    "knowledge repo's contract linter refuses an alias that collides with a page ("
    "`alias '{alias}' collides with page {page}`). The wikilink namespace is keyed on page names, "
    "and the absorbed page stays by governance — so this spelling stays with it")


def _page_names(worktree: str) -> dict[str, str]:
    """`{lowercased page stem: path}` for the whole wikilink namespace — the linter's own
    `lower_names`, hand-mirrored the way `deletion`'s link scanner is and for the same reason: this
    package talks to that linter through FILES, and a claim it would refuse must be refused HERE,
    with a sentence, rather than at apply time as a gate veto nobody can act on."""
    return {page_stem(rel).lower(): rel for rel in deletion.corpus_pages(worktree)}


def claimable_aliases(worktree: str, *, survivor_path: str, survivor_name: str,
                      survivor_aliases, absorbed_aliases) -> list[str]:
    """The survivor's `aliases:` after the merge, sorted and deduplicated.

    Raises `RepairError` for an alias the survivor may not claim, rather than dropping it: a merge
    that silently lost a spelling would be a repair whose whole point — that this name resolves to
    the surviving entity from now on — quietly did not happen for that name.
    """
    names = _page_names(worktree)
    survivor_stem = page_stem(survivor_path).lower()
    merged: list[str] = []
    for alias in [*survivor_aliases, *absorbed_aliases]:
        text = str(alias or "").strip()
        if not text or text in merged:
            continue
        lowered = text.lower()
        # A page may declare its OWN name as an alias; the linter skips that pair explicitly.
        if lowered != survivor_stem and lowered in names:
            raise RepairError(UNCLAIMABLE_ALIAS.format(
                alias=text, survivor=survivor_name or page_stem(survivor_path),
                page=names[lowered]))
        merged.append(text)
    return sorted(merged)


# ── the plan ──────────────────────────────────────────────────────────────────────────────────
def plan(worktree: str, survivor_path: str, absorbed_path: str) -> list[dict]:
    """The whole merge, as the stored `ops` list.

    Deterministic and ORDERED — the survivor, the absorbed page, the re-anchored pages by path,
    then the registry — because the apply's proof is `recomputed == stored`, and two runs over the
    same bytes have to produce the same LIST rather than the same set.

    Raises `RepairError`, with a sentence a steward reads verbatim, for anything this cannot do:
    a page that is not an entity page, a pair that is one page, an entity the registry does not
    register, or an alias the contract linter would refuse.
    """
    survivor_path, absorbed_path = str(survivor_path or ""), str(absorbed_path or "")
    if not survivor_path or not absorbed_path:
        raise RepairError("a merge names two entity pages: the one that survives and the one it "
                          "absorbs")
    if survivor_path == absorbed_path:
        raise RepairError(
            f"{survivor_path} cannot absorb itself — a merge is a decision about two identities, "
            f"and this proposal names one page twice")
    for path in (survivor_path, absorbed_path):
        _code, sentence = entity_page_refusal(worktree, path)
        if sentence:
            raise RepairError(sentence)

    claims = _registered(worktree)
    survivor = _claim_for(claims, survivor_path)
    absorbed = _claim_for(claims, absorbed_path)

    survivor_text, absorbed_text = _read(worktree, survivor_path), _read(worktree, absorbed_path)
    aliases = claimable_aliases(
        worktree, survivor_path=survivor_path, survivor_name=survivor.name,
        survivor_aliases=survivor.aliases, absorbed_aliases=absorbed.aliases)

    ops = [_op(OP_ALIAS, survivor_path, survivor_text,
               aliased(survivor_text, aliases, absorbed_stem=page_stem(absorbed_path))),
           _op(OP_RETIRE, absorbed_path, absorbed_text,
               retired(absorbed_text, page_stem(survivor_path)))]
    for rel in anchored_paths(worktree, absorbed.canonical_id,
                              excluding=(survivor_path, absorbed_path)):
        text = _read(worktree, rel)
        after = reanchored(text, absorbed_id=absorbed.canonical_id,
                           survivor_id=survivor.canonical_id)
        if after == text:
            continue
        ops.append(_op(OP_REANCHOR, rel, text, after))

    registry_before = _read(worktree, REGISTRY_RELPATH)
    if registry_before is None:
        raise RepairError(
            f"{REGISTRY_RELPATH} could not be read, and a merge regenerates it — every anchoring "
            f"decision resolves against that file, so a merge that could not rebuild it would "
            f"leave the corpus resolving names nobody registered")
    ops.append(_op(OP_REGISTRY, REGISTRY_RELPATH, registry_before,
                   _predicted_registry(claims, survivor.canonical_id, absorbed.canonical_id,
                                       aliases)))
    # The registry op is ALWAYS stored, even when the file comes out identical (the ordinary case:
    # the absorbed entity declared no alias to move), because it is what `apply_declared` proves
    # the real generator's output against. `schema.target_paths` is what keeps an unchanged file
    # out of the set the diff is cross-checked against.
    if not schema.target_paths(ops):
        raise RepairError(
            f"{survivor_path} and {absorbed_path} already say what this merge would say — the "
            f"survivor already links the absorbed page and carries its spellings, the absorbed "
            f"page is already marked superseded, and nothing is still anchored to it. There is "
            f"nothing here for a steward to approve")
    return ops


def _op(name: str, path: str, before: str, after: str) -> dict:
    return {schema.OP_KIND_KEY: name, "path": path,
            "expected_before_hash": _sha256(before), "planned_after": after}


def _registered(worktree: str):
    """Every entity page's identity claim, through `entities.generator`'s own reader.

    That reader is strict — an untitled page or a slug collision RAISES — and the strictness is
    inherited deliberately rather than worked around: those are exactly the states in which
    `stigmergy-entities regenerate` refuses to run, so a merge planned against one would store a
    registry prediction the apply could never produce. The `EntityError` is re-worded here because
    it reaches a steward through the review lane and this package's refusals are `RepairError`.
    """
    try:
        return generator.read_entity_pages(worktree)
    except EntityError as ex:
        raise RepairError(
            f"the entity registry cannot be rebuilt from this corpus as it stands, so a merge "
            f"cannot be planned against it: {ex}") from ex


def _claim_for(claims, path: str):
    """The identity claim belonging to one entity PAGE, by its relpath — the generator's own
    `relpath`, so this cannot disagree with the registry about which page is which entity."""
    found = next((c for c in claims if c.relpath == path), None)
    if found is None:
        raise RepairError(
            f"{path} is not one of the entity pages {REGISTRY_RELPATH} is derived from, so it "
            f"registers no identity that could be merged")
    return found


def _predicted_registry(claims, survivor_id: str, absorbed_id: str, aliases: list[str]) -> str:
    """What `stigmergy-entities regenerate` will write once the two pages are rewritten.

    Predicted rather than produced: `plan` is a pure function of the bytes on disk and writes
    nothing, and the apply proves the prediction by running the REAL generator and byte-comparing.
    Built through the generator's own `registry_of` and `kernel.registry.registry_text` — the one
    serializer — so the prediction cannot drift from the file format.
    """
    registry: Registry = generator.registry_of([
        _with_aliases(claim, aliases) if claim.canonical_id == survivor_id
        else _with_aliases(claim, []) if claim.canonical_id == absorbed_id
        else claim
        for claim in claims])
    return registry_text(registry)


def _with_aliases(claim, aliases):
    return dataclasses.replace(claim, aliases=tuple(aliases))


# ── the readers every other surface goes through ──────────────────────────────────────────────
def _paths_named(ops, name: str) -> list[str]:
    return [str(o.get("path", "")) for o in (ops or ())
            if str(o.get(schema.OP_KIND_KEY, "")) == name and o.get("path")]


def survivor_path(ops) -> str:
    return next(iter(_paths_named(ops, OP_ALIAS)), "")


def absorbed_path(ops) -> str:
    return next(iter(_paths_named(ops, OP_RETIRE)), "")


def reanchored_paths(ops) -> list[str]:
    return sorted(set(_paths_named(ops, OP_REANCHOR)))


def registry_paths(ops) -> list[str]:
    return sorted(set(_paths_named(ops, OP_REGISTRY)))


def expected_bytes(ops) -> dict[str, str]:
    """`{path: the exact bytes the plan would write}` — every op, because every op in this kind
    rewrites a whole file. The fact `gate_body_rewrite` is TOLD, replacing an additive proof that a
    re-anchored `entity:` line can never satisfy."""
    return {str(o["path"]): str(o.get("planned_after", "")) for o in (ops or ())
            if str(o.get(schema.OP_KIND_KEY, "")) in OP_NAMES and o.get("path")}


def derived_files(ops) -> frozenset[str]:
    """The paths in this plan that are NOT pages — the regenerated registry, and only it.

    The fact `gate_zone` is told, and it is deliberately narrower than "every op": `gate_zone`
    refuses a write in the lane that is not a `.md` page, which is the right default and the reason
    `ops/entity-registry.json` cannot ride an ordinary repair. A caller is trusted about WHICH
    derived file its approval covers and about nothing else.
    """
    return frozenset(registry_paths(ops))


def lane_for(ops) -> tuple[str, ...]:
    """The write lane THIS plan owns: the zone of every page it touches, and no other.

    Derived rather than fixed, `deletion.lane_for`'s reasoning: a merge legitimately spans the
    entity zone, `ops/`, and whichever zones the re-anchored pages happen to sit in. So the lane
    cannot be what proves the plan is confined — `validate` is — and what the narrowed lane still
    buys is everything OUTSIDE this plan.
    """
    return tuple(sorted({deletion.zone_prefix(str(o.get("path", "")))
                         for o in (ops or ()) if o.get("path")}))


def plan_bytes(ops) -> int:
    """How much of a steward's attention this plan is, measured in the bytes it stores."""
    return sum(len(str(o.get("planned_after", "")).encode("utf-8")) for o in (ops or ()))


OVERSIZE_REASON = (
    "entity-alias-plan-too-large({size}>{ceiling}): this merge would rewrite {pages} page(s), and "
    "the stored plan carries every one of them in full. One approval is one decision a person can "
    "actually have read — merge entities with fewer anchored pages, or raise the ceiling "
    "deliberately")


def oversize_reason(ops, ceiling: int) -> str:
    """`""` when the plan fits its ceiling, the named reason when it does not —
    `deletion.oversize_reason`'s shape, and it shares that kind's setting: both store whole pages,
    so one ceiling governs how much stored CONTENT one approval may carry."""
    size = plan_bytes(ops)
    if size <= int(ceiling):
        return ""
    return OVERSIZE_REASON.format(size=size, ceiling=int(ceiling),
                                  pages=len(reanchored_paths(ops)))


# ── the validator both ends run ───────────────────────────────────────────────────────────────
def validate(worktree: str, ops) -> list[gates.Finding]:
    """Every reason this plan could not be performed against `worktree`, or `[]`.

    The SHAPE half only — that the ops are well-formed, that there is exactly one survivor, one
    absorbed page and one registry regeneration, and that every path they name is something this
    kind may touch in this tree. Whether the plan is still the RIGHT plan is `apply_declared`'s
    recomputation, which is a question about the corpus rather than about the row.
    """
    ops = list(ops or ())
    if not ops:
        return [_finding("no-ops", "an entity-alias proposal carries the merge it would perform")]
    out: list[gates.Finding] = []
    seen: set[str] = set()
    for op in ops:
        name = str(op.get(schema.OP_KIND_KEY, ""))
        path = str(op.get("path", ""))
        if name not in OP_NAMES:
            out.append(_finding("unknown-kind",
                                f"declared a {name!r} op in a {schema.KIND_ENTITY_ALIAS} proposal, "
                                f"which performs {', '.join(OP_NAMES)} and nothing else", path))
            continue
        if path in seen:
            out.append(_finding("duplicate-path",
                                f"{path} appears twice in one merge: each page is rewritten once, "
                                f"and a second op would only see the first's result", path))
            continue
        seen.add(path)
        out += _path_findings(worktree, name, path)
        if not str(op.get("planned_after", "")):
            out.append(_finding("no-planned-bytes",
                                f"the {name} op on {path} carries no planned bytes, so there is "
                                f"nothing to compare a recomputation against", path))
    out += _shape_findings(ops)
    return out


def _path_findings(worktree: str, name: str, path: str) -> list[gates.Finding]:
    if name in schema.ALIAS_IDENTITY_OP_NAMES:
        code, sentence = entity_page_refusal(worktree, path)
        return [_finding(code, sentence, path)] if code else []
    if name == OP_REGISTRY:
        if path != REGISTRY_RELPATH:
            return [_finding("not-the-registry",
                             f"a merge regenerates {REGISTRY_RELPATH} and no other derived file; "
                             f"this op names {path}", path)]
        return []
    code, sentence = deletion.scrub_refusal(worktree, path)
    return [_finding(code, sentence, path)] if code else []


def _shape_findings(ops) -> list[gates.Finding]:
    """The three counts that make a merge a merge. Asked of the op LIST rather than assumed from
    `plan`, because `validate` also runs at apply time against a row somebody could have edited."""
    out = []
    for name, paths in ((OP_ALIAS, _paths_named(ops, OP_ALIAS)),
                        (OP_RETIRE, _paths_named(ops, OP_RETIRE)),
                        (OP_REGISTRY, _paths_named(ops, OP_REGISTRY))):
        if len(paths) != 1:
            out.append(_finding(
                "wrong-op-count",
                f"a {schema.KIND_ENTITY_ALIAS} proposal carries exactly one {name} op, not "
                f"{len(paths)}: a merge is one identity absorbing one other, and its registry is "
                f"rebuilt once"))
    return out


PLAN_DRIFT_CODE = "plan-drift"
REGISTRY_DRIFT_CODE = "registry-drift"


def apply_declared(worktree: str, ops) -> tuple[list[str], list[gates.Finding]]:
    """Validate, RECOMPUTE, byte-compare, then perform — all-or-nothing.

    The recomputation is this kind's whole propose-to-apply contract, `deletion.apply_declared`'s
    for the same reason: what a merge writes depends on every OTHER page in the corpus. A page that
    gained the absorbed entity's anchor since the proposal was made is a different merge, and
    performing the old one would leave that page anchored to a retired identity.

    The registry is the one file this does NOT write from the plan. The pages are written, then
    `generator.regenerate` — the mint door's own writer, and the only writer of that file in this
    codebase — rebuilds it, and the result is refused unless it is byte-identical to what the plan
    predicted. A prediction that turned out wrong is a fact about the corpus, not something to
    paper over by writing the stored bytes instead.
    """
    findings = validate(worktree, ops)
    if findings:
        return [], findings
    recomputed = plan(worktree, survivor_path(ops), absorbed_path(ops))
    if recomputed != [dict(o) for o in ops]:
        return [], [_finding(PLAN_DRIFT_CODE,
                             "the merge this repo needs now is not the merge that was approved — "
                             "the entity pages, or the pages anchored to them, have changed since "
                             "it was proposed")]
    planned = expected_bytes(ops)
    for path in (survivor_path(ops), absorbed_path(ops), *reanchored_paths(ops)):
        full = os.path.join(worktree, *path.split("/"))
        with page_policy.open_for_rewrite(full) as f:
            f.write(planned[path])
    try:
        generator.regenerate(worktree)
    except EntityError as ex:
        return [], [_finding("registry-refused",
                             f"the entity registry could not be regenerated after the merge: {ex}",
                             REGISTRY_RELPATH)]
    produced = _read(worktree, REGISTRY_RELPATH)
    if produced != planned[REGISTRY_RELPATH]:
        return [], [_finding(REGISTRY_DRIFT_CODE,
                             f"the registry {generator.FIX_COMMAND} produced is not the registry "
                             f"this merge planned, so the approval does not describe what would "
                             f"land", REGISTRY_RELPATH)]
    return sorted({survivor_path(ops), absorbed_path(ops), REGISTRY_RELPATH,
                   *reanchored_paths(ops)}), []
