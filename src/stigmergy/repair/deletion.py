"""The `delete` kind: a page leaves the corpus, and every page that referred to it is rewritten.

Deletion is the one repair whose blast radius is not the page it names. Removing a file is trivial;
what is not trivial is that the corpus afterwards still has to be a graph — the knowledge repo's
contract linter treats an unresolvable `[[wikilink]]` as an ERROR, and `gate_contract` turns that
into a veto — and still has to READ: a sentence that cited the page, a callout that announced an
overlap with it, must not be left saying something that stopped being true. So this kind is not
"remove a file", it is a **sweep**: the pages that go, and the full planned bytes of every page
that referred to one of them.

**Structure is code's; prose is a model's (ADR 043 D1).** This module owns the deterministic half
of the plan — which pages go, which pages refer to them, and their FRONTMATTER with the entries
that named a going page dropped — and every bound the written half has to satisfy. The bodies of
the referring pages are written by `repair.sweep`, one model call over the whole referring set,
and land here as the same `planned_after` bytes. Which pages go is never a model's: a person names
them, or `duplicate_source_groups` derives them for exact-duplicate `sources/` pages, where the
decision is a lookup (ADR 039's second amendment, which stands).

Three properties buy this kind its safety, and each is asked of a different thing:

  · **The zone is a whitelist.** `wiki/entities/` is absent by construction — an identity is
    retired through governance, not deletion (ADR 016) — and so is everything outside the corpus.
  · **The plan proves its own bounds, at both ends.** `validate` — run when a plan is stored and
    again against the clone it lands on — proves every scrubbed page's frontmatter is code's own
    scrub of the page as it stands, and that no planned page still names a going one. A base hash
    per page says whether the corpus moved under a stored plan; the apply refuses on either.
  · **Nothing outside the plan refers to a going page.** Asked of the whole corpus at apply time,
    so a page that gained a reference since a plan was stored refuses it rather than surviving it
    as a dead link.

Every link question is asked EXACTLY as the frozen contract linter asks it — code fences and inline
code blanked first, alias and anchor split off, the last path segment minus `.md` — and the regexes
are hand-mirrored from it rather than imported, the posture `entity_body` states for the same
reason: this package talks to the linter through FILES. A scanner that sees fewer links than the
linter leaves a dead link and a veto at apply time. It sees one MORE shape than the linter does —
a markdown link at a going page's path — because a writer reconciles prose, and a path in prose is
a reference whether or not the linter counts it.
"""
import difflib
import hashlib
import os
import re
import urllib.parse
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


# A markdown link's target — `[text](wiki/notes/Old Memo.md)`, `[text](../Old%20Memo.md)`,
# `[text](<wiki/notes/Old Memo.md>)`, `[text](path "title")` — the one reference shape the linter
# does not count and a writer still has to reconcile. Resolved to a stem exactly as a wikilink
# target is, after the angle brackets, the optional title and the URL-encoding come off.
#
# Everything up to the closing paren is taken, SPACES INCLUDED: a path with a bare space is not
# well-formed markdown, and a scanner that stopped at whitespace would have missed
# `[the memo](wiki/notes/Old Memo.md)` — a reference a reader plainly sees and the contract linter
# never counts, so nothing downstream would have caught it either. Matching a little more than
# markdown does is the safe direction here: a false positive only means a page is handed to the
# writer, and it reconciles what it finds.
_MD_LINK_RE = re.compile(r"\]\(([^)]*)\)")


def _md_target(raw: str) -> str:
    """One markdown link's target when it names a PAGE IN THIS CORPUS, `""` otherwise.

    Two shapes are dropped, and the second is the one that matters: a target carrying a URL SCHEME
    (`https://`, `mailto:`) is somebody's external link, and a target not ending in `.md` is not a
    page. Without that rule `[the roadmap](https://notion.so/team/Roadmap)` counted as a reference
    to `Roadmap` — and since a surviving reference REFUSES a deletion rather than merely widening
    it, deleting `Roadmap.md` would have demanded that the writer destroy an unrelated external
    link, or the deletion could never happen at all. A bound must not fire on something the writer
    has no business touching.
    """
    text = str(raw or "").strip()
    if text.startswith("<") and ">" in text:
        text = text[1:text.index(">")]
    else:
        # `[x](path "title")` — the title is not the target, and it is the only thing after a
        # space that markdown allows there.
        quote = min((i for i in (text.find(' "'), text.find(" '")) if i != -1), default=-1)
        if quote != -1:
            text = text[:quote]
    text = text.strip()
    if _URL_SCHEME_RE.match(text):
        return ""
    return text if text.split("#", 1)[0].strip().lower().endswith(".md") else ""


# `scheme:` at the start of a link target. Anything with one is a URL somebody wrote, not a page
# in this repo — and `mailto:`/`tel:` carry no `.md` anyway, so this is belt and braces on the
# suffix rule below it.
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _live_references(text: str):
    """Every stem this text still refers to: the linter's wikilinks, plus markdown links. The
    question `references` and the post-sweep proof both ask, over the code-blanked text."""
    for _match, stem in _live_links(text):
        yield stem
    blanked = _blanked_code(text)
    for match in _MD_LINK_RE.finditer(blanked):
        stem = link_stem(urllib.parse.unquote(_md_target(match.group(1))))
        if stem:
            yield stem


def references(text: str, stems: set[str]) -> bool:
    """Does this page, as these bytes, still refer to a page that is going — anywhere?"""
    return any(stem in stems for stem in _live_references(text or ""))


def _frontmatter_reference(text: str, stems: set[str]) -> str:
    """The first going stem the FRONTMATTER still refers to after code's scrub, or `""`.

    Frontmatter is code's half and the writer never sees it, so a reference in a field this kind
    does not rewrite is one nothing can remove — and the plan refuses to exist, with a sentence
    naming the page, rather than becoming a question a gate would later veto. It asks the SAME
    question `validate`'s bound asks (`_live_references`, markdown links included): asking a
    narrower one here meant a `see: "[x](Old Memo.md)"` field surfaced two model calls later as a
    body problem the writer structurally could not fix.
    """
    front, _rest = page_policy.split_frontmatter(text or "")
    return next((stem for stem in _live_references(front) if stem in stems), "")


def _names_a_going_page(value: str, stems: set[str]) -> bool:
    """Does this frontmatter VALUE point at a page that is going? A wikilink is read as one; a
    bare value is read as a page name, which is how `supersedes: "Old Decision"` is spelled."""
    text = str(value or "")
    links = [link_stem(m.group(1)) for m in _WIKILINK_RE.finditer(text)]
    if links:
        return any(stem in stems for stem in links if stem)
    return link_stem(text) in stems


def _readable_frontmatter(text: str) -> bool:
    """Does this page open with a frontmatter block `split_frontmatter` recognises?

    A page that does not — CRLF, a BOM, an unterminated `---` — is one whose frontmatter code
    cannot scrub and whose whole file would otherwise be handed to the writer as "the body". Every
    outcome of that is wrong: the writer returns the frontmatter and is refused for opening a
    `---`, or drops it and the page loses what it declares. So such a page refuses the DELETION,
    by name, at plan time — the one moment a person can act on it.
    """
    front, rest = page_policy.split_frontmatter(text or "")
    return bool(front.strip()) and len(rest) != len(text or "")


# ── the sweep, on one page's bytes ────────────────────────────────────────────────────────────
def scrubbed(text: str, stems: set[str], *, machine_written: bool = False) -> str:
    """Code's half of ONE page's planned bytes: the frontmatter with every entry naming a going
    page dropped, and the body VERBATIM — the body is the writer's (`repair.sweep`), and this is
    the page it is handed and the frontmatter `validate` holds its answer to.

    `machine_written` scrubs the BODY too, deterministically, and it is how the machine zones are
    treated: a `views/` page is REGENERATED wholesale by the view sweep, and a `sources/` page is a
    filed document's provenance. Neither is prose anybody reconciles, so handing one to a writer
    asks a model to argue with a generated file and produce bytes the next regeneration overwrites.
    There, unlinking IS the right answer — ADR 039 B3's own rule, kept exactly where it was right.

    Pure, and byte-exact about the parts it does not touch: the frontmatter block is reassembled
    from its own lines and the body is spliced rather than re-rendered, so a page whose `related:`
    entry went is otherwise the file that was read.
    """
    text = text or ""
    front, rest = page_policy.split_frontmatter(text)
    if len(text) == len(rest):
        # No frontmatter block at all. The linter refuses such a page for other reasons; the body
        # is still the writer's to reconcile rather than left pointing at a page that is gone.
        return _unlinked_body(text, stems) if machine_written else text
    # The separator is taken FROM THE FILE rather than assumed: a page whose closing `---` has no
    # newline after it would otherwise gain one, and a page that gained a byte is a page in the
    # sweep's blast radius — a scrub op, a steward, an approval — for a change nobody made.
    head = text[:len(text) - len(rest)]
    front_lines = _scrubbed_front(front.split("\n"), stems)
    body = _unlinked_body(rest, stems) if machine_written else rest
    return ("---\n" + "\n".join(front_lines) + "\n---" + ("\n" if head.endswith("\n") else "")
            + body)


# A markdown link WITH its text: `_MD_LINK_RE` asks only where a link points, which is the
# reference question; unlinking needs what to leave behind.
_MD_LINK_WITH_TEXT_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def is_machine_written(path: str) -> bool:
    """Is this page one nobody authored — a derived view, or a filed document's provenance? THE
    one place that question is asked, because it decides which half of a sweep owns the body."""
    return str(path or "").startswith(PROVENANCE_ZONE_PREFIXES)


def _unlinked_body(text: str, stems: set[str]) -> str:
    """Unlink, never delete — ADR 039 B3's rule, kept for the machine zones alone. `[[X]]` becomes
    `X`, `[[X|alias]]` becomes `alias`, a markdown link becomes its text, so the line that named a
    page survives the page."""
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

    def _md(match):
        target = _md_target(match.group(2))
        if not target or link_stem(urllib.parse.unquote(target)) not in stems:
            return match.group(0)
        return match.group(1)

    return _MD_LINK_WITH_TEXT_RE.sub(_md, "".join(out))


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
    """Code's half of the whole sweep, as the stored `ops` list: the deletions, then one scrub op
    for every page that refers to a going page — its frontmatter already scrubbed, its body
    verbatim, its base hash recorded. `repair.sweep.write` is what turns the bodies into the
    reconciled ones; a plan stored without that step is refused by `validate`, because a body
    still naming a going page is exactly what the bound forbids.

    Deterministic and ORDERED — the deletions by path, then the scrubs by path — so two runs over
    the same bytes produce the same list rather than the same set.

    Raises `RepairError`, with a sentence a steward reads verbatim, for a target this kind may not
    delete and for a page whose FRONTMATTER names a going page in a field this kind does not
    rewrite.
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
        after = scrubbed(text, stems, machine_written=is_machine_written(rel))
        if references(text, stems) and not _readable_frontmatter(text):
            raise RepairError(
                f"{rel} refers to a page this deletion removes, and its frontmatter is not a shape "
                f"this can read (a CRLF or BOM page, or an unterminated `---` block) — so the "
                f"entries naming that page cannot be taken out, and the rest of the page cannot be "
                f"written around it. Fix that page's frontmatter by hand first, then delete again")
        unremovable = _frontmatter_reference(after, stems)
        if unremovable:
            raise RepairError(
                f"{rel} names [[{unremovable}]] in a frontmatter field this sweep does not "
                f"rewrite — the reference would survive the deletion as a dead link and the "
                f"contract linter would refuse the commit. Move or remove that reference by hand "
                f"first, then delete again")
        if after == text and not references(text, stems):
            continue
        ops.append({schema.OP_KIND_KEY: OP_SCRUB, "path": rel,
                    "expected_before_hash": sha256(text), "planned_after": after})
    return ops


def written_paths(ops) -> list[str]:
    """The scrubbed pages a MODEL writes: the authored ones. A machine-written page is code's
    whole answer (`scrubbed(..., machine_written=True)`), so it is never in a writer's prompt and
    never one of the pages it must hand back."""
    return [path for path in scrubbed_paths(ops) if not is_machine_written(path)]


def going_stems(ops) -> set[str]:
    """The stems a plan removes — the set every reference question about it is asked against."""
    return {page_stem(path) for path in deleted_paths(ops)}


def unified_diffs(worktree: str, ops) -> dict[str, str]:
    """`{path: unified diff}` of what this plan does to each page it rewrites, against the page as
    it stands in `worktree` — what a person reads when the act road hands the result back (ADR
    043 D5): nobody read the written prose before it landed, so the diff IS the reading. A removed
    page is listed by its deletion op and needs no diff."""
    out: dict[str, str] = {}
    for path in scrubbed_paths(ops):
        before = read_text(worktree, path) or ""
        after = expected_bytes(ops).get(path, "")
        out[path] = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=path, tofile=path, n=2))
    return out


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
# The two bounds on a WRITTEN sweep, as finding codes a steward reads: the writer owns the body
# and nothing else, and afterwards nothing it wrote may still name a page that is going.
FRONTMATTER_REWRITTEN_CODE = "frontmatter-rewritten"
REFERENCE_SURVIVES_CODE = "reference-survives"


def _frontmatter_of(text: str) -> str:
    """The frontmatter block plus its fences, exactly as the bytes carry it — the head `scrubbed`
    reassembles, compared whole so a byte moved in it is a byte noticed."""
    _front, rest = page_policy.split_frontmatter(text or "")
    return (text or "")[:len(text or "") - len(rest)]


def validate(worktree: str, ops) -> list[gates.Finding]:
    """Every reason this plan could not be performed against `worktree`, or `[]`.

    The SHAPE half — that the ops are well-formed and every path they name is a page this kind
    may touch in this tree — and the two bounds a written sweep has to satisfy (ADR 043 D1): every
    scrubbed page's frontmatter is code's own scrub of the page as it stands here, byte for byte,
    and no planned page still refers to a page that is going. Both are asked of the stored bytes
    against THIS tree, which is what lets the same function prove a plan at propose time and again
    against the clone it lands on. Whether the corpus moved under the plan is `apply_declared`'s
    question, asked of the base hashes.
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
                                f"nothing to write and nothing to prove", path))
    stems = going_stems(ops)
    for path in scrubbed_paths(ops) if stems else ():
        planned = expected_bytes(ops).get(path, "")
        current = read_text(worktree, path)
        if not planned or current is None:
            continue                    # already refused above, by name
        if is_machine_written(path):
            if planned != scrubbed(current, stems, machine_written=True):
                out.append(_finding(FRONTMATTER_REWRITTEN_CODE,
                                    f"the planned bytes of {path} are not code's own scrub of it — "
                                    f"a derived or provenance page is nobody's prose to write",
                                    path))
            continue
        if _frontmatter_of(planned) != _frontmatter_of(scrubbed(current, stems)):
            out.append(_finding(FRONTMATTER_REWRITTEN_CODE,
                                f"the planned bytes of {path} carry a frontmatter that is not "
                                f"code's own scrub of the page as it stands: the writer owns the "
                                f"body and nothing else", path))
        if references(planned, stems):
            out.append(_finding(REFERENCE_SURVIVES_CODE,
                                f"{path} would still refer to a page this sweep removes after it "
                                f"is written — the reference would survive as a dead link", path))
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
    """Validate, prove the corpus did not move, then perform — all-or-nothing.

    Two questions replace the recomputation ADR 039 B4 ran, because a written sweep cannot be
    recomputed (ADR 043 D3). The base hash every scrub op carries says whether a page the plan
    rewrites changed since the plan was made; a walk of the corpus says whether a page the plan
    does NOT rewrite now refers to a going page — the latecomer that would otherwise survive the
    deletion as a dead link. Either refuses the whole plan: the corpus moved, delete again. On the
    act road the plan was made against this very tree moments ago, so both are a formality that
    costs one walk; on the inbox road they are the whole contract.
    """
    findings = validate(worktree, ops)
    if findings:
        return [], findings
    stems = going_stems(ops)
    planned = set(scrubbed_paths(ops)) | set(deleted_paths(ops))
    for op in ops:
        if str(op.get(schema.OP_KIND_KEY, "")) != OP_SCRUB:
            continue
        current = read_text(worktree, str(op["path"]))
        if sha256(current) != str(op.get("expected_before_hash", "")):
            return [], [_finding(PLAN_DRIFT_CODE,
                                 f"{op['path']} has changed since this sweep was written, so the "
                                 f"bytes it would land are a rewrite of a page nobody read — the "
                                 f"corpus moved, delete again", str(op["path"]))]
    for rel in corpus_pages(worktree):
        if rel in planned:
            continue
        text = read_text(worktree, rel)
        if text is None:
            continue
        if references(text, stems) or scrubbed(text, stems) != text:
            return [], [_finding(PLAN_DRIFT_CODE,
                                 f"{rel} now refers to a page this sweep removes and the plan "
                                 f"never rewrote it — the corpus moved, delete again", rel)]
    for path in scrubbed_paths(ops):
        full = os.path.join(worktree, *path.split("/"))
        with page_policy.open_for_rewrite(full) as f:
            f.write(expected_bytes(ops)[path])
    for path in deleted_paths(ops):
        os.remove(os.path.join(worktree, *path.split("/")))
    return sorted({*deleted_paths(ops), *scrubbed_paths(ops)}), []
