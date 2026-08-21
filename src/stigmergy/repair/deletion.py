"""The `delete` kind: a page leaves the corpus, and every reference to it leaves with it.

Deletion is the one repair whose blast radius is not the page it names. Removing a file is trivial;
what is not trivial is that the corpus afterwards still has to be a graph — the knowledge repo's
contract linter treats an unresolvable `[[wikilink]]` as an ERROR, and `gate_contract` turns that
into a veto. So this kind is not "remove a file", it is a **sweep**: the pages that go, and the
full planned bytes of every page that mentioned one of them.

**No model is asked, ever.** Every judgment here is a lookup — which pages name this stem, which
entries in a list point at it — and a judgment-free decision belongs to code (ADR 039's second
amendment). The one automatic road (exact-duplicate `sources/` pages) is deterministic for the same
reason; a human types the rest at `stigmergy-repair delete`, because judging that a page is stale
is the judgment that is neither code's nor a model's.

Three properties buy this kind its safety, and each is asked of a different thing:

  · **The zone is a whitelist.** `wiki/entities/` is absent by construction — an identity is
    retired through governance, not deletion (ADR 016) — and so is everything outside the corpus.
  · **The plan is a pure function of the bytes on disk.** It is computed at propose time against
    the operator's checkout and RECOMPUTED at apply time against a fresh clone, and the apply
    refuses unless the two agree byte for byte. A corpus that moved under a proposal is a
    re-proposal, never a best-effort sweep.
  · **The plan proves itself.** Before it is stored, every planned page is re-scanned for a link to
    a page that is going. This module knows how to rewrite four frontmatter fields and the body; a
    reference anywhere else refuses the whole plan rather than becoming a question whose answer a
    gate would later veto.

Every link question is asked EXACTLY as the frozen contract linter asks it — code fences and inline
code blanked first, alias and anchor split off, the last path segment minus `.md` — and the regexes are
hand-mirrored from it rather than imported, the posture `entity_body` states for the same reason:
this package talks to the linter through FILES. A scanner that sees more links than the linter edits
prose nobody asked about; one that sees fewer leaves a dead link and a veto at apply time.
"""
import hashlib
import os
import re
from pathlib import PurePosixPath

import yaml

from stigmergy.librarian import gates
from stigmergy.librarian import page as page_policy
from stigmergy.repair import schema
from stigmergy.repair.errors import RepairError

# The two op names, from the vocabulary module that also has to tell them apart (`content_key`
# keys a deletion on its removals alone). Unlike the other two kinds, the KIND is not the op name:
# one approval performs two different actions, and a steward reading `ops_preview` has to be able
# to tell "three pages removed" from "eleven pages rewritten" without opening the row.
OP_DELETE = schema.DELETE_OP_NAME
OP_SCRUB = schema.SCRUB_OP_NAME
OP_NAMES = schema.DELETE_OP_NAMES

# What may be deleted: the fast lane's own folders plus the two machine zones. `wiki/entities/` is
# absent BY CONSTRUCTION rather than by exclusion — an entity page's type carries no folder in
# `page.FOLDER_BY_TYPE`, so it is not in `gates.ALLOWED_WRITE_PREFIXES` and could only be added
# here deliberately.
PROVENANCE_ZONE_PREFIXES = ("sources/", "views/")
DELETABLE_PREFIXES = (*gates.ALLOWED_WRITE_PREFIXES, *PROVENANCE_ZONE_PREFIXES)

# The zone an identity page lives in, spelled here so the refusal can name it. `entity_body` owns
# the same string one module over and states why it is not imported from `entities.generator`.
ENTITY_ZONE_PREFIX = "wiki/entities/"

# The wikilink namespace — the frozen linter's `CONTENT_ROOTS`, hand-mirrored. Not
# `index.corpus.ZONES`: `tests/test_architecture.py` closes this package's reach at
# `stigmergy.index`, and the definition that governs here is the LINTER's anyway.
CONTENT_ZONES = ("wiki", "sources", "views")
CONTENT_ZONE_PREFIXES = tuple(f"{zone}/" for zone in CONTENT_ZONES)

# The frozen linter's own three, verbatim (`stigmergy_lint.WIKILINK_RE`, `FENCE_RE`,
# `INLINE_CODE_RE`). The fence pattern is DOTALL; the inline one deliberately is not, and both
# `[^`]` and `[^\[\]]` already match a newline.
_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")

# The frontmatter fields a wikilink legitimately lives in. LIST fields lose the entries that name a
# page that is going; POINTER fields lose the whole line, because a supersession pointer at a page
# that no longer exists is not a pointer at all. Any other field carrying such a link refuses the
# plan (`_unremovable_reference`) rather than being guessed at.
LINK_LIST_FIELDS = ("related", "sources")
POINTER_FIELDS = ("supersedes", "superseded_by")


def _finding(code: str, message: str, locator: str = "") -> gates.Finding:
    return gates.Finding("deletion", code, message, locator=locator)


# ── the link question, asked the linter's way ─────────────────────────────────────────────────
def link_stem(target: str) -> str:
    """The page name a wikilink target resolves to, `""` when it names nothing.

    `stigmergy_lint.link_targets` + its `link_stem`, in one function: the last path segment, minus
    a trailing `.md`, with every dot a title has kept. The linter once answered this with
    `Path(target).stem`, which amputated a dotted name (`[[Booking.com]]` resolved to `Booking`),
    and this mirror amputated with it on purpose — a plan that disagrees with the gate passes
    propose time and fails apply. The linter stopped amputating (a live `[[Acme Inc. Invoice …]]`
    was vetoed as dead), so this answers the same question the same new way.
    """
    text = str(target or "").split("|", 1)[0].split("#", 1)[0].strip()
    name = text.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def page_stem(path: str) -> str:
    """The name a page is linkable BY — the linter's `by_name` key, which is the file's stem."""
    return PurePosixPath(str(path or "")).stem


def _blanked_code(text: str) -> str:
    """`text` with every fenced block and inline-code span replaced by spaces of the same length.

    The linter DELETES them before it looks for links; blanking is equivalent for that question —
    no backtick survives either way, so the same spans pair up — and it keeps every offset, which
    is what lets the body rewriter below map a match back onto the real bytes.
    """
    blanked = _FENCE_RE.sub(lambda m: " " * (m.end() - m.start()), text or "")
    return _INLINE_CODE_RE.sub(lambda m: " " * (m.end() - m.start()), blanked)


def _live_links(text: str):
    """Every wikilink match the linter would count, as `(match, stem)` over the ORIGINAL text."""
    blanked = _blanked_code(text)
    for match in _WIKILINK_RE.finditer(blanked):
        stem = link_stem(match.group(1))
        if stem:
            yield match, stem


def _names_a_going_page(value: str, stems: set[str]) -> bool:
    """Does this frontmatter VALUE point at a page that is going? A wikilink is read as one; a
    bare value is read as a page name, which is how `supersedes: "Old Decision"` is spelled."""
    text = str(value or "")
    links = [link_stem(m.group(1)) for m in _WIKILINK_RE.finditer(text)]
    if links:
        return any(stem in stems for stem in links if stem)
    return link_stem(text) in stems


# ── the sweep, on one page's bytes ────────────────────────────────────────────────────────────
def scrubbed(text: str, stems: set[str]) -> tuple[str, str]:
    """`(planned bytes, refusal)` for ONE page against the set of stems that are going.

    A non-empty refusal means this page still names a page that is going after everything this
    module knows how to rewrite has been rewritten — the self-check that keeps a stored plan from
    being a question the contract linter would later veto.

    Pure, and byte-exact about the parts it does not touch: the frontmatter block is reassembled
    from its own lines and the body is spliced rather than re-rendered, so a page whose `related:`
    entry went is otherwise the file that was read.
    """
    text = text or ""
    front, rest = page_policy.split_frontmatter(text)
    if len(text) == len(rest):
        # No frontmatter block at all. The linter refuses such a page for other reasons; scrubbing
        # its body is still the honest thing to do rather than leaving a dead link behind.
        after = _scrubbed_body(text, stems)
    else:
        # The separator is taken FROM THE FILE rather than assumed: a page whose closing `---` has
        # no newline after it would otherwise gain one, and a page that gained a byte is a page in
        # the sweep's blast radius — a scrub op, a steward, an approval — for a change nobody made.
        head = text[:len(text) - len(rest)]
        front_lines = _scrubbed_front(front.split("\n"), stems)
        after = ("---\n" + "\n".join(front_lines) + "\n---" + ("\n" if head.endswith("\n") else "")
                 + _scrubbed_body(rest, stems))
    return after, _unremovable_reference(after, stems)


def _unremovable_reference(text: str, stems: set[str]) -> str:
    """The first stem this page still points at after the sweep, or `""`."""
    return next((stem for _match, stem in _live_links(text) if stem in stems), "")


def _scrubbed_body(text: str, stems: set[str]) -> str:
    """Unlink, never delete. `[[X]]` becomes `X` and `[[X|alias]]` becomes `alias`, so the sentence
    that cited a page survives the page — the whole difference between a sweep and a shredder."""
    out, cut = [], 0
    for match, stem in _live_links(text):
        if stem not in stems:
            continue
        target, _, alias = match.group(1).partition("|")
        display = alias.strip() or target.split("#", 1)[0].strip()
        out.append(text[cut:match.start()])
        out.append(display)
        cut = match.end()
    out.append(text[cut:])
    return "".join(out)


def _scrubbed_front(front_lines: list[str], stems: set[str]) -> list[str]:
    lines = list(front_lines)
    for key in LINK_LIST_FIELDS:
        lines = _without_list_entries(lines, key, stems)
    for key in POINTER_FIELDS:
        lines = _without_pointer(lines, key, stems)
    return lines


def _without_list_entries(front_lines: list[str], key: str, stems: set[str]) -> list[str]:
    """Drop the entries of a frontmatter LIST that name a page that is going. An emptied field's
    whole block goes: `related: []` would be a declaration nobody made, and the linter treats a
    missing recommended field as a warning rather than an error."""
    start, raw = page_policy.top_level_key_line(front_lines, key)
    if start < 0:
        return front_lines
    _start, end = page_policy.top_level_key_span(front_lines, key)
    inline = raw.strip()
    if inline:
        values = page_policy.parse_list_value(inline)
        if not values:
            return front_lines          # not a list this can rewrite; the self-check decides
        kept = [v for v in values if not _names_a_going_page(v, stems)]
        if len(kept) == len(values):
            return front_lines
        replacement = [f"{key}: {page_policy.yaml_list(kept)}"] if kept else []
        return front_lines[:start] + replacement + front_lines[end:]

    # A block sequence: the surviving ITEM LINES are kept verbatim, so a page a human diffs keeps
    # the indentation and the quoting it was written with.
    kept_lines, dropped = [], False
    for line in front_lines[start + 1:end]:
        values = page_policy.parse_list_value(line.strip()) if line.strip() else []
        if values and all(_names_a_going_page(v, stems) for v in values):
            dropped = True
            continue
        kept_lines.append(line)
    if not dropped:
        return front_lines
    if not any(line.strip() for line in kept_lines):
        return front_lines[:start] + front_lines[end:]
    return front_lines[:start + 1] + kept_lines + front_lines[end:]


def _without_pointer(front_lines: list[str], key: str, stems: set[str]) -> list[str]:
    """A `supersedes:`/`superseded_by:` line naming a page that is going goes whole. A pointer at a
    page that does not exist is dead whether or not it is spelled as a wikilink, and there is no
    such thing as half a supersession."""
    start, raw = page_policy.top_level_key_line(front_lines, key)
    if start < 0:
        return front_lines
    _start, end = page_policy.top_level_key_span(front_lines, key)
    try:
        parsed = yaml.safe_load(raw) if raw.strip() else None
    except yaml.YAMLError:
        parsed = raw
    if isinstance(parsed, list):
        return _without_list_entries(front_lines, key, stems)
    if parsed is None or not _names_a_going_page(str(parsed), stems):
        return front_lines
    return front_lines[:start] + front_lines[end:]


# ── what may be deleted, and what may be scrubbed ─────────────────────────────────────────────
def target_refusal(worktree: str, path: str) -> tuple[str, str]:
    """`(code, sentence)` for a page this kind may not delete, `("", "")` when it may.

    ONE rule, read by both ends: `plan` raises the sentence at a human typing the command, and
    `validate` turns the code into a finding the apply names in its refusal. Two spellings of the
    same question would be two answers.
    """
    if path.startswith(ENTITY_ZONE_PREFIX):
        return "entity-page", (
            f"{path} is an entity page, and an identity is retired through governance rather than "
            f"deleted: the pages anchored to it would lose the thing they are about. Retire the "
            f"entity with a steward, or delete the pages that are actually stale")
    if not path.startswith(DELETABLE_PREFIXES):
        return "outside-lane", (
            f"{path} is not a corpus page this kind may delete — deletion is confined to "
            f"{', '.join(DELETABLE_PREFIXES)}, and nothing under `ops/` or `.claude/` is a page at "
            f"all")
    return page_refusal(worktree, path, symlink_why=DELETE_SYMLINK_WHY,
                        missing_why=DELETE_MISSING_WHY)


def scrub_refusal(worktree: str, path: str) -> tuple[str, str]:
    """The same question for a page the sweep would REWRITE. Its lane is wider than the deletable
    one on purpose: an entity page may perfectly well cite a note that is going, and refusing to
    scrub it would leave the dead link the sweep exists to prevent."""
    if not path.startswith(CONTENT_ZONE_PREFIXES):
        return "outside-corpus", (
            f"{path} is outside the corpus zones ({', '.join(CONTENT_ZONE_PREFIXES)}), so no sweep "
            f"can have planned a rewrite of it")
    return page_refusal(worktree, path, symlink_why=DELETE_SYMLINK_WHY,
                        missing_why=DELETE_MISSING_WHY)


# The two sentences that read the same whichever kind is asking — a dotfile is a dotfile, and a
# path resolving outside the checkout is outside it — so `page_refusal` defaults to them. The other
# two name what THIS kind would have done to the page, so they are the caller's.
NOT_A_PAGE_WHY = "{path} is not a page: this kind touches `.md` files and never a dotfile"
OUTSIDE_WORKTREE_WHY = "{path} resolves outside the repo checkout"

# This kind's own two, for the caller-supplied half above.
DELETE_SYMLINK_WHY = ("{path} is a symlink and not a page — removing it would remove the pointer "
                      "and leave the page it names in place")
DELETE_MISSING_WHY = "{path} does not exist in the repo"


def page_refusal(worktree: str, path: str, *, symlink_why: str, missing_why: str,
                 not_a_page_why: str = NOT_A_PAGE_WHY,
                 outside_worktree_why: str = OUTSIDE_WORKTREE_WHY,
                 require_readable: bool = False) -> tuple[str, str]:
    """`(code, sentence)` for a path that is not a real, non-symlinked `.md` page inside
    `worktree`, `("", "")` when it is — THE confinement predicate, for every kind in this package.

    ONE implementation because it is a SECURITY predicate: hardening it — an NFD-spelling check, a
    `..` segment rule, a stricter ancestor test — has to reach every kind at once, and a second
    copy is how the kinds that did not get the hardening keep the hole in silence.

    Each `*_why` is a `{path}` TEMPLATE rather than a finished sentence, because a refusal is read
    by a steward and has to say what the kind asking would have done to the page. The two that
    carry no such verb have defaults.

    `require_readable` picks the last check and the difference is deliberate: a kind that goes on to
    PARSE the file wants `read_text(...) is None` (a merge rewrites frontmatter it has to read
    first, so unreadable is missing), and one that only needs the page to exist wants
    `os.path.isfile`.
    """
    basename = path.rsplit("/", 1)[-1]
    if basename.startswith(".") or not basename.endswith(".md"):
        return "not-a-page", not_a_page_why.format(path=path)
    # Containment RESOLVED rather than inferred from the string: every check above is a shape check,
    # and a symlinked DIRECTORY component satisfies all of them.
    if not page_policy.is_inside(worktree, path):
        return "outside-worktree", outside_worktree_why.format(path=path)
    full = os.path.join(worktree, path)
    if os.path.islink(full):
        return "symlinked-target", symlink_why.format(path=path)
    missing = read_text(worktree, path) is None if require_readable else not os.path.isfile(full)
    if missing:
        return "missing-target", missing_why.format(path=path)
    return "", ""


# ── the plan ──────────────────────────────────────────────────────────────────────────────────
def plan(worktree: str, paths) -> list[dict]:
    """The whole sweep for a deletion set, as the stored `ops` list.

    Deterministic and ORDERED — the deletions by path, then the scrubs by path — because the
    apply's proof is `recomputed == stored`, and two runs over the same bytes have to produce the
    same list rather than the same set.

    Raises `RepairError`, with a sentence a steward reads verbatim, for a target this kind may not
    delete and for a page whose reference the sweep cannot remove.
    """
    targets = sorted({str(p) for p in (paths or ()) if str(p)})
    if not targets:
        raise RepairError("a deletion names at least one page")
    for path in targets:
        _code, sentence = target_refusal(worktree, path)
        if sentence:
            raise RepairError(sentence)

    stems = {page_stem(p) for p in targets}
    ops: list[dict] = [{schema.OP_KIND_KEY: OP_DELETE, "path": path} for path in targets]
    going = set(targets)
    for rel in corpus_pages(worktree):
        if rel in going:
            # A page on its way out is never rewritten first: the plan would then carry planned
            # bytes for a file that must not exist when the apply finishes.
            continue
        text = read_text(worktree, rel)
        if text is None:
            continue                    # unreadable as text: it declares no wikilink either
        after, unremovable = scrubbed(text, stems)
        if unremovable:
            raise RepairError(
                f"{rel} refers to [[{unremovable}]] somewhere this sweep cannot rewrite — the "
                f"reference would survive the deletion as a dead link and the contract linter "
                f"would refuse the commit. Move or remove that reference by hand first, then "
                f"propose the deletion again")
        if after == text:
            continue
        ops.append({schema.OP_KIND_KEY: OP_SCRUB, "path": rel,
                    "expected_before_hash": sha256(text), "planned_after": after})
    return ops


def corpus_pages(worktree: str) -> list[str]:
    """Every page in the wikilink namespace, sorted. Symlinks are skipped: they are not pages the
    linter indexes by stem, and a sweep must never write through one."""
    out = []
    for zone in CONTENT_ZONES:
        for parent, _dirs, files in os.walk(os.path.join(worktree, zone)):
            for name in files:
                if not name.endswith(".md") or name.startswith("."):
                    continue
                full = os.path.join(parent, name)
                if os.path.islink(full):
                    continue
                out.append(os.path.relpath(full, worktree).replace(os.sep, "/"))
    return sorted(out)


def read_text(worktree: str, rel: str) -> str | None:
    """One repo-relative file as text, `None` when it cannot be read as text at all. PUBLIC:
    `entity_alias` reads the same checkout through this one, so the two non-additive kinds cannot
    come to disagree about which files exist."""
    try:
        with open(os.path.join(worktree, *rel.split("/")), encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def sha256(text: str) -> str:
    """The propose-to-apply drift proof for both non-additive kinds: the bytes an op was computed
    FROM, so "the corpus moved" is a fact rather than a guess. PUBLIC for the same reason
    `read_text` is."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ── the readers every other surface goes through ──────────────────────────────────────────────
def deleted_paths(ops) -> list[str]:
    return sorted({str(o.get("path", "")) for o in (ops or ())
                   if str(o.get(schema.OP_KIND_KEY, "")) == OP_DELETE and o.get("path")})


def scrubbed_paths(ops) -> list[str]:
    return sorted({str(o.get("path", "")) for o in (ops or ())
                   if str(o.get(schema.OP_KIND_KEY, "")) == OP_SCRUB and o.get("path")})


def provenance_scrubs(ops) -> frozenset[str]:
    """The scrubbed pages whose provenance frontmatter is LEGITIMATE — the machine zones.

    A sweep is the first thing in this system that modifies a `sources/` or `views/` page at all,
    and `gate_frontmatter` refuses `content_hash`/`tier`/`extracted_at` on any modified in-lane page
    the caller has not declared a provenance page. Those fields are the librarian's own stamps from
    when it filed the page; a scrub only ever REMOVES, so it cannot be the thing that asserted one.
    """
    return frozenset(path for path in scrubbed_paths(ops)
                     if path.startswith(PROVENANCE_ZONE_PREFIXES))


def expected_bytes(ops) -> dict[str, str]:
    """`{path: the exact bytes the plan would write}` — the fact `gate_body_rewrite` is TOLD for
    this kind, replacing an additive proof a scrub can never satisfy."""
    return {str(o["path"]): str(o.get("planned_after", "")) for o in (ops or ())
            if str(o.get(schema.OP_KIND_KEY, "")) == OP_SCRUB and o.get("path")}


def zone_prefix(path: str) -> str:
    """The write lane one page sits in — the frozen linter's `zone_key`, with its trailing slash.
    Under `wiki/` the zone is the FOLDER (`wiki/notes/`), because that is the granularity
    `gates.ALLOWED_WRITE_PREFIXES` is written in; elsewhere it is the root."""
    parts = str(path or "").split("/")
    if parts[0] == "wiki" and len(parts) > 2:
        return f"{parts[0]}/{parts[1]}/"
    return f"{parts[0]}/"


def lane_for(ops) -> tuple[str, ...]:
    """The write lane THIS plan owns: the zone of every page it touches, and no other.

    Derived rather than fixed, unlike `entity_body`'s one-zone lane, because a sweep legitimately
    spans several — a note goes and a decision page, a source page and an entity page each stop
    pointing at it. So the lane cannot be what proves the plan is confined; `validate` is (a
    deletion may only name `DELETABLE_PREFIXES`, a scrub only a corpus page). What the narrowed
    lane still buys is everything OUTSIDE this plan: a write the sweep did not plan, in a zone this
    plan never touches, is out of lane and vetoed.
    """
    return tuple(sorted({zone_prefix(str(o.get("path", "")))
                         for o in (ops or ()) if o.get("path")}))


def plan_bytes(ops) -> int:
    """How much of a steward's attention this plan is, measured in the bytes it stores."""
    return sum(len(str(o.get("planned_after", "")).encode("utf-8")) for o in (ops or ()))


OVERSIZE_REASON = (
    "delete-plan-too-large({size}>{ceiling}): this deletion would rewrite {pages} page(s), and the "
    "stored plan carries every one of them in full. One approval is one decision a person can "
    "actually have read — delete fewer pages at a time, or raise the ceiling deliberately")


def oversize_reason(ops, ceiling: int) -> str:
    """`""` when the plan fits its ceiling, the named reason when it does not.

    A string rather than a raise, because the two doors need it differently: the CLI turns it into
    a refusal a person reads, and the nightly duplicate road records it as a skip.
    """
    size = plan_bytes(ops)
    if size <= int(ceiling):
        return ""
    return OVERSIZE_REASON.format(size=size, ceiling=int(ceiling), pages=len(scrubbed_paths(ops)))


# ── the validator both ends run ───────────────────────────────────────────────────────────────
def validate(worktree: str, ops) -> list[gates.Finding]:
    """Every reason this plan could not be performed against `worktree`, or `[]`.

    The SHAPE half only — that the ops are well-formed and every path they name is a page this kind
    may touch in this tree. Whether the plan is still the RIGHT plan is `apply_declared`'s
    recomputation, which is a question about the corpus rather than about the row.
    """
    ops = list(ops or ())
    if not ops:
        return [_finding("no-ops", "a delete proposal carries at least one page to delete")]
    out: list[gates.Finding] = []
    seen: set[str] = set()
    for op in ops:
        name = str(op.get(schema.OP_KIND_KEY, ""))
        path = str(op.get("path", ""))
        if name not in OP_NAMES:
            out.append(_finding("unknown-kind",
                                f"declared a {name!r} op in a {schema.KIND_DELETE} proposal, which "
                                f"performs {' and '.join(OP_NAMES)} and nothing else", path))
            continue
        if path in seen:
            out.append(_finding("duplicate-path",
                                f"{path} appears twice in one plan: a page is deleted once and "
                                f"rewritten once, and a second op would only see the first's "
                                f"result", path))
            continue
        seen.add(path)
        code, sentence = (target_refusal(worktree, path) if name == OP_DELETE
                          else scrub_refusal(worktree, path))
        if code:
            out.append(_finding(code, sentence, path))
            continue
        if name == OP_SCRUB and not str(op.get("planned_after", "")):
            out.append(_finding("no-planned-bytes",
                                f"the scrub of {path} carries no planned bytes, so there is "
                                f"nothing to compare a recomputation against", path))
    if not deleted_paths(ops):
        out.append(_finding("no-deletion",
                            f"a {schema.KIND_DELETE} proposal that removes no page is a rewrite of "
                            f"other people's pages wearing a deletion's name"))
    return out


# ── the one automatic road, and it asks no model ──────────────────────────────────────────────
# Two `sources/` pages declaring the same `content_hash:` are the same captured document filed
# twice. Which one goes is not a judgment — it is a lookup — so it is CODE's, and this is the one
# deliberate exception to "a model proposes" (ADR 039's second amendment). A model asked "are these
# duplicates?" would be asked to re-derive a fact the frontmatter already states, and would
# sometimes get it wrong.
SOURCES_ZONE_PREFIX = "sources/"
CONTENT_HASH_KEY = "content_hash"

# The fields that answer "which of these was captured later", in the order a `sources/` page is
# likely to carry them. `extracted_at` is what the librarian stamps on one; the other two are the
# authored-page convention, read as a fallback rather than assumed absent.
AGE_FIELDS = ("extracted_at", "created", "updated")

DUPLICATE_RATIONALE = (
    "{doomed} and {survivor} declare the same content_hash, so they are one captured document "
    "filed twice. This removes {doomed_short} — {why} — and keeps {survivor_short}.")
_FEWER_LINKS_WHY = "{n} page(s) link to it against {m}"
_NEWER_WHY = "the same number of pages link to each, so the older filing stays"


def duplicate_source_groups(worktree: str) -> list[tuple[str, list[str]]]:
    """`[(survivor, [pages to delete]), ...]` for every set of `sources/` pages that declare the
    same `content_hash:`.

    BOTH tie-breaks are deterministic, which is the whole reason this road needs no model:

      1. the page with MORE inbound links survives — the corpus has already voted on which copy it
         cites, and deleting that one would scrub the citations off every page that made them;
      2. on a tie the OLDER filing survives — the later one is the accident, and the earlier one is
         the copy any external reference to this document is likelier to have been made against;
      3. on a tie in both, the lexicographically first path survives, so the answer never depends
         on the order a directory happened to be walked in.
    """
    hashes: dict[str, list[str]] = {}
    for rel in corpus_pages(worktree):
        if not rel.startswith(SOURCES_ZONE_PREFIX):
            continue
        text = read_text(worktree, rel)
        digest = _content_hash(text) if text is not None else ""
        if digest:
            hashes.setdefault(digest, []).append(rel)

    inbound = _inbound_counts(worktree)
    out = []
    for _digest, group in sorted(hashes.items()):
        if len(group) < 2:
            continue
        ranked = sorted(group, key=lambda p: (-inbound.get(p, 0), _age_key(worktree, p), p))
        out.append((ranked[0], sorted(ranked[1:])))
    return sorted(out, key=lambda pair: pair[1])


def duplicate_rationale(worktree: str, survivor: str, doomed: str) -> str:
    """What a steward reads beside Approve — composed by CODE from the two facts that decided it,
    because no model was asked and a sentence claiming otherwise would be a lie about provenance."""
    inbound = _inbound_counts(worktree)
    doomed_links, survivor_links = inbound.get(doomed, 0), inbound.get(survivor, 0)
    why = (_FEWER_LINKS_WHY.format(n=doomed_links, m=survivor_links)
           if doomed_links != survivor_links else _NEWER_WHY)
    return DUPLICATE_RATIONALE.format(doomed=doomed, survivor=survivor, why=why,
                                      doomed_short=page_stem(doomed),
                                      survivor_short=page_stem(survivor))


def _content_hash(text: str) -> str:
    front, _rest = page_policy.split_frontmatter(text or "")
    if not front.strip():
        return ""
    try:
        parsed = yaml.safe_load(front)
    except yaml.YAMLError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get(CONTENT_HASH_KEY) or "").strip()


def _age_key(worktree: str, rel: str) -> str:
    """One comparable spelling of "when was this captured", `""` when the page says nothing —
    which sorts FIRST, so a page carrying no date is treated as the older filing and survives."""
    text = read_text(worktree, rel)
    front, _rest = page_policy.split_frontmatter(text or "")
    try:
        parsed = yaml.safe_load(front) if front.strip() else {}
    except yaml.YAMLError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    return next((str(parsed[field]) for field in AGE_FIELDS if parsed.get(field)), "")


def _inbound_counts(worktree: str) -> dict[str, int]:
    """How many OTHER pages link to each page, by the linter's own resolution rule."""
    by_stem: dict[str, list[str]] = {}
    for rel in corpus_pages(worktree):
        by_stem.setdefault(page_stem(rel), []).append(rel)
    counts: dict[str, int] = {}
    for rel in corpus_pages(worktree):
        text = read_text(worktree, rel)
        if text is None:
            continue
        for stem in {stem for _match, stem in _live_links(text)}:
            for target in by_stem.get(stem, ()):
                if target != rel:
                    counts[target] = counts.get(target, 0) + 1
    return counts


PLAN_DRIFT_CODE = "plan-drift"


def apply_declared(worktree: str, ops) -> tuple[list[str], list[gates.Finding]]:
    """Validate, RECOMPUTE, byte-compare, then perform — all-or-nothing.

    The recomputation is this kind's whole propose-to-apply contract. `entity_body` can re-run its
    validator against the clone and know the draft still applies; a sweep cannot, because what it
    would write depends on every OTHER page in the corpus. A page that gained a link to the doomed
    page since the proposal was made is a different sweep, and performing the old one would leave
    exactly the dead link this kind exists to prevent. So the plan is derived again from the clone's
    own bytes and refused unless it is identical — the corpus moved, propose again.
    """
    findings = validate(worktree, ops)
    if findings:
        return [], findings
    recomputed = plan(worktree, deleted_paths(ops))
    if recomputed != [dict(o) for o in ops]:
        return [], [_finding(PLAN_DRIFT_CODE,
                             "the sweep this repo needs now is not the sweep that was approved — "
                             "the pages that reference the deletion have changed since it was "
                             "proposed")]
    for path in scrubbed_paths(ops):
        full = os.path.join(worktree, *path.split("/"))
        with page_policy.open_for_rewrite(full) as f:
            f.write(expected_bytes(ops)[path])
    for path in deleted_paths(ops):
        os.remove(os.path.join(worktree, *path.split("/")))
    return sorted({*deleted_paths(ops), *scrubbed_paths(ops)}), []
