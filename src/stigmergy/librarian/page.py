"""Placement policy and the server-owned half of a filed page's frontmatter.

Two things live here, and they are the same idea seen from two sides:

**Placement** — which types exist, which the fast lane may CREATE, and where each one goes.
The distinction is not cosmetic. The librarian READS the whole graph and may link to any page
of any type (a `decision` may link an `entity`); it may CREATE only three. Mixing those two
questions is how `notes/` becomes the next `sources/general/`, so both are answered from ONE
shared table (`PAGE_TYPES`) rather than by an `if` at each call site. `classify_page_type` is
that shared base and returns both answers — `known` is the management scope (what may be read and
linked), `creatable` is the operational scope (what the fast lane may mint). `ensure_creatable` is
the write guard, and `gates.gate_zone` calls it over the real diff for every page created,
deriving the type from the FOLDER the page landed in — so the agent's judgment is an input to the
decision and never the decision.

**Server-owned fields** — the frontmatter the SERVER computes and a document may never assert:
`submitted_by` from the queue row, `acl` from the path rules, `status` forced to `developing`,
`owner` and `verification` stripped (nothing computes a verification verdict, so no page may
claim one). Whatever the material declared about any of them was recorded as a flagged hint at
submit time and is inert here: `stamp_server_fields` deletes the declared line and writes the
server's value in its place.

**Path identity** — `path_key`, `path_keys` and `is_inside`: when do two spellings name the SAME
page, and does a path resolve inside the worktree. One seam because the first question was
answered twice with `str == str` (`agent.confined_write`, `edits.validate`) and both answers were
wrong on a case- and normalization-insensitive filesystem. Here rather than in `agent.py` so
`edits` can reach it without an `edits -> agent` edge.

**Page-text surgery** — the operations that change a page's bytes in place, all of them here so
there is one module that knows what a page's frontmatter looks like: `stamp_server_fields` (the
server-owned rewrite), `with_related_link` and `with_callout` (the two additive edits `edits.py`
performs from the agent's declaration), and `open_for_rewrite` (the symlink-refusing open both
writers share). `unnameable_reason` sits beside them because a page's NAME is its title, and
whether a title can be spelled is the same kind of question as what its frontmatter may say.

**What is deliberately NOT stamped**: `content_hash`, `id` and the rest of the page contract's
provenance group. That group belongs to source pages — a fast-lane capture page is not one, so
those fields do not belong on it. Dedup against
already-filed material is keyed on the QUEUE's own content hash instead (see `dedup.py`), which
is deterministic, cheaper than grepping the repo, and needs no page field at all.
"""
import json
import os
import re
import unicodedata
from dataclasses import dataclass

import yaml

from stigmergy.librarian.errors import WorktreeError

# The status every fast-lane page is filed as, whatever it declared about itself. A constant, not
# a default: nothing the material claims can change it.
FILED_STATUS = "developing"


@dataclass(frozen=True)
class PageType:
    """ONE row of the page vocabulary — the single table every placement question reads.

    This used to be three tables in two modules (`FOLDER_BY_TYPE`, `ALL_PAGE_TYPES` +
    `_EXCLUSION_REASON`, `report.TYPE_LABELS`, plus a hand-written type list in a triage
    sentence). Adding a type took four edits, and no test covered two of them. Everything below is
    derived from here.
    """
    name: str
    folder: str = ""     # "" unless the fast lane may CREATE one
    label: str = ""      # how a person is told about it in a sentence ("an entity page")
    reason: str = ""     # why the fast lane may not create it, in the submitter's language
    provenance: bool = False   # a RECORD OF AN EVENT, never a knowledge destination — see below


# The full vocabulary, in one place. The first THREE are the fast lane's OPERATIONAL scope;
# everything after them may be read, linked and cross-referenced but never created here —
# governed elsewhere, and landing in `triage` rather than being quietly downgraded to `note`,
# because a per-type exemption is exactly how ambient ownerless content accumulates.
# Mirrors the contract linter's own `VALID_TYPES`: a type absent here is one the graph lacks.
#
# **Seven types, one per WRITER.** The three below are the librarian's only genre choice; each of
# the rest has exactly one stamper. The vocabulary is deliberately small: entity KINDS
# (person/team/product/customer) live in the registry's own `type` field rather than becoming
# page types, because two taxonomies for one spine is the duplication this very table exists to
# end. Adding a type back is one row + one template + one linter line; removing one migrates
# pages — which is why erring small is the cheap direction.
_IDENTITY_REASON = "identity pages are created through a steward's review, not the fast lane"

# Every label is a noun phrase WITH its article and WITH the word `page`, and both halves are
# load-bearing for the one sentence that reads them (`report.triage_type`). The article is why the
# sentence drops the label in whole rather than qualifying it; `page` is why it still reads as English
# when it does. Without it, "This reads like a person" and "where does a team belong?" are what a
# submitter gets told about a PAGE TYPE — grammatical and about the wrong subject. `meta` already
# carried it, which is the shape the rest are now consistent with.
PAGE_TYPES = (
    PageType("note", folder="wiki/notes", label="a note page"),
    PageType("decision", folder="wiki/decisions", label="a decision page"),
    PageType("concept", folder="wiki/concepts", label="a concept page"),
    PageType("entity", label="an entity page", reason=_IDENTITY_REASON),
    PageType("source", label="a source page", provenance=True,
             reason="source pages are written by code from a captured document, never drafted"),
    PageType("meeting", label="a meeting page", provenance=True,
             reason="meeting pages arrive with the meeting distiller"),
    PageType("view", label="a view page",
             reason="view pages are regenerated from an entity's members, never captured"),
)

_BY_NAME = {page_type.name: page_type for page_type in PAGE_TYPES}

# Derived views. Kept as module constants because callers read them by name, but there is exactly
# one place to edit when the vocabulary changes.
ALL_PAGE_TYPES = frozenset(_BY_NAME)
FOLDER_BY_TYPE = {p.name: p.folder for p in PAGE_TYPES if p.folder}
FAST_LANE_TYPES = frozenset(FOLDER_BY_TYPE)

# ── what `entity: []` MEANS ───────────────────────────────────────────────────────────────────
# THE CONTRACT, stated here because this module is where the page vocabulary lives:
#
#   On an ordinary `wiki/**` page, `entity: []` is a CHECKED, EXPLICIT COMPANY-WIDE
#   DECLARATION — the author is claiming the page is about the whole company, and
#   `gate_anchoring` demands a written reason for it.
#
#   On a PROVENANCE page it is neither a claim nor an omission: a provenance page is a RECORD OF
#   AN EVENT, never a knowledge destination, so it HAS no aboutness to declare. `[]` there means
#   "about nothing", not "about everything".
#
# Why the distinction has to be written down rather than left to context: the same value in the
# same zone had grown three readings, and the third one had a consequence. A steward has to
# CONFIRM a company-wide claim before a page carrying `entity: []` is promoted — so a meeting
# page put a company-wide claim in front of a human and asked them to sign it, when the page had
# never made one. Asking a person to vouch for an assertion nobody wrote is exactly the failure
# that confirmation step exists to prevent, arrived at from the other side. Found by a real-agent
# walk, not by a test, because no meeting page had been promoted before.
#
# The second consequence is quieter: `stigmergy.views` reads `entity:` to build member sets, where
# a meeting page reads as "about everything" under the company-wide interpretation and "a member
# of nothing" under the provenance one. They agreed by luck of implementation; now a provenance
# page is a member of nothing BY CONTRACT.
PROVENANCE_PAGE_TYPES = frozenset(p.name for p in PAGE_TYPES if p.provenance)


def is_provenance_type(page_type: str) -> bool:
    """Does this page type record an EVENT rather than assert knowledge? See
    `PROVENANCE_PAGE_TYPES` above for the contract and why it is enforced rather than described.

    An unknown or empty type answers False — the conservative direction: it keeps the
    company-wide confirmation in place, which is a human being asked one extra question, whereas
    the other default would silently skip a governance gate for any type nobody classified."""
    return str(page_type or "").strip().lower() in PROVENANCE_PAGE_TYPES

# The creatable types, as a person reads them in a triage message. Derived from the one table, so
# a type added or removed cannot leave a sentence elsewhere claiming the old count.
FAST_LANE_TYPE_LIST = ", ".join(FOLDER_BY_TYPE)


@dataclass(frozen=True)
class TypePolicy:
    """What the fast lane may do with one page type. The shared answer both entry points read."""
    page_type: str
    known: bool          # does the graph have this type at all (management scope)
    creatable: bool      # may the fast lane mint one (operational scope)
    folder: str          # "" unless creatable
    reason: str          # why not, in the submitter's language; "" when creatable
    label: str = ""      # how a person is told about it in a sentence


def classify_page_type(page_type: str) -> TypePolicy:
    """THE shared base every placement question goes through. One table, one answer.

    Callers do not read `FOLDER_BY_TYPE` or `FAST_LANE_TYPES` directly — they ask this and read
    the field they care about, so "known" and "creatable" can never drift apart at a call site.
    """
    normalized = (page_type or "").strip().lower()
    known = _BY_NAME.get(normalized)
    if known is None:
        return TypePolicy(normalized, False, False, "",
                          "it is not a page type this brain has", "")
    if known.folder:
        return TypePolicy(normalized, True, True, known.folder, "", known.label)
    return TypePolicy(normalized, True, False, "",
                      known.reason or "it is not a fast-lane type", known.label)


def label_for(page_type: str) -> str:
    """How a person is told about this type in a sentence. Falls back to the raw value, sanitized
    by the caller — an unknown type is still something a triage message has to name."""
    return classify_page_type(page_type).label


def ensure_creatable(page_type: str) -> TypePolicy:
    """The write guard. Raises `ValueError` for anything the fast lane may not mint.

    Called by the zone gate over the real diff, AFTER the agent has run — read access to a type
    is not permission to create one, and the agent's own judgment about what it wrote is not
    what decides. A capture the agent labelled `note` but filed into `wiki/entities/` is
    caught here, by the path, not by the label.
    """
    policy = classify_page_type(page_type)
    if not policy.creatable:
        raise ValueError(f"the fast lane cannot create a {policy.page_type or 'typeless'!r} "
                         f"page: {policy.reason}")
    return policy


def folder_for(page_type: str) -> str:
    """The folder a creatable type belongs in. Raises for anything else — same guard."""
    return ensure_creatable(page_type).folder


def type_for_folder(path: str) -> str:
    """The type a path's folder implies, or "" when the folder is not a fast-lane one. The
    inverse lookup the zone gate uses to catch a page filed in the wrong drawer."""
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    for page_type, known_folder in FOLDER_BY_TYPE.items():
        if folder == known_folder:
            return page_type
    return ""


# ── path identity: when are two spellings the SAME page ───────────────────────────────────────
# One seam, because the question was answered twice with `str == str` and both answers were wrong.
# `agent.confined_write` asks "is this a page that does not exist yet" and `edits.validate` asks
# "is this a page this capture just created"; both compared raw strings against `git`'s own
# spelling, and on the primary deployment platform two different strings name ONE file. The
# placement vocabulary was correctly centralized into `PAGE_TYPES`; path identity was not, so it
# lives here now and there is one place to fix it.


def path_key(rel: str) -> str:
    """One comparable spelling for a repo-relative path.

    macOS/APFS is case-insensitive AND normalization-insensitive, so two byte-different strings
    can name ONE file. `git ls-files` reports the tracked bytes and `os.path.realpath` resolves
    symlinks while canonicalizing neither, so comparing raw strings answers "is this a new page"
    with "yes" for `EXISTING NOTE.md` and for the NFD spelling of an accented title — and the write
    lands on the human's page, with the diff showing only added lines.

    Case-folding is also correct on a case-SENSITIVE filesystem: the knowledge repo's own contract
    linter refuses duplicate basenames case-insensitively (`stigmergy_lint.py`: "macOS + Obsidian
    resolution"), so two pages differing only in case are a contract violation there too. Being
    stricter than the filesystem is the safe direction for this rule to be wrong in.
    """
    return unicodedata.normalize("NFC", rel or "").casefold()


def path_keys(paths) -> set[str]:
    """`path_key` over a collection — the set form both callers compare against.

    Deliberately recomputed per call rather than cached beside the tracked-paths set: a second
    representation of "the pages that already exist" is a second thing that can go stale, and the
    cost is a casefold per tracked path on a write the agent makes at most `max_tool_calls` times.
    """
    return {path_key(p) for p in (paths or ())}


def is_inside(root: str, candidate: str) -> bool:
    """Does `candidate` (absolute or `root`-relative) resolve inside `root`?

    Both sides go through `realpath`. That symmetry is the whole point: the root used to be
    `abspath` while the candidate was `realpath`, and on darwin the default temp root is a symlink
    (`/var` -> `/private/var`), so NOTHING matched and every legitimate write was denied — the SDK
    backend could not file a single page on a Mac.

    Resolving the candidate is also what makes this a check on a DIRECTORY component rather than on
    a leaf: `wiki/playbooks` being a symlink out of the worktree is invisible to an
    `os.path.islink` test on the final path and is caught here.
    """
    resolved_root = os.path.realpath(root)
    try:
        resolved = os.path.realpath(os.path.join(resolved_root, candidate))
    except (OSError, ValueError):
        return False
    return resolved == resolved_root or resolved.startswith(resolved_root + os.sep)


# ── frontmatter ───────────────────────────────────────────────────────────────────────────────
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---(\n|\Z)", re.S)

# THREE spellings of a top-level key — bare, "double-quoted", 'single-quoted' — the exact set the
# knowledge repo's own contract linter already recognizes (`stigmergy_lint.py`'s `KEY_RE`). A
# bare-only pattern (`^([A-Za-z_][\w.-]*)\s*:(.*)$`) leaves a capture-drafted `"entity": [...]`
# (or `'entity':`) invisible to `_strip_keys` — it survives next to the server's own line, and
# `yaml.safe_load` takes the LAST occurrence when a page is later read, so containment would rest
# on an incidental last-key-wins property nothing asserts and that inverts for any first-wins or
# duplicate-strict reader.
#
# **Widened again for non-ASCII: the bare-key branch's first character used to be `[A-Za-z_]`,
# ASCII only.** A bare `еntity:` (Cyrillic е, U+0435) is an ordinary PyYAML plain
# scalar key (`yaml.safe_load('еntity: ["x"]')` parses it fine) but did not match this regex AT
# ALL, so `_match_key` returned `None` for the whole line — not "recognized as a key and compared
# wrong", but "never recognized as a key line in the first place", which meant no downstream
# normalization (`normalize_key`) could have helped either. `[^\W\d]` is `\w` (Unicode-aware in a
# Python 3 `str` pattern) minus digits — any Unicode LETTER or underscore, matching what a bare
# YAML plain scalar can open with in practice.
_KEY_RE = re.compile(r"""^(?:"([^"]*)"|'([^']*)'|([^\W\d][\w.-]*))\s*:(.*)$""")


def _match_key(line: str) -> tuple[str, str] | None:
    """`(key, value_str)` for a line opening a `key:` pair under any of the three spellings
    above — whichever group matched IS the key, so every reader below sees one name regardless
    of how the line spelled it. `None` when the line does not open a key at all.

    Every caller that used to do `_KEY_RE.match(line).group(1)` for the key now goes through
    here, on purpose: this is the ONE place three-spellings-read-as-one is implemented, so a
    fourth reader added later cannot reintroduce the bare-only blindness by copying the old
    pattern instead of this function.
    """
    m = _KEY_RE.match(line or "")
    if not m:
        return None
    key = next((g for g in m.groups()[:3] if g is not None), "")
    return key, m.group(4)


# CYRILLIC letters that are visually indistinguishable from a Latin letter appearing in one of
# `SERVER_OWNED_KEYS`/`FORBIDDEN_PAGE_KEYS` (below) — verified NOT to be something NFKC folds:
# NFKC only collapses COMPATIBILITY variants within Unicode's own equivalence tables (full-width
# forms, ligatures, certain typographic variants of the SAME character), never across scripts, so
# `unicodedata.normalize("NFKC", "еntity")` leaves the Cyrillic е (U+0435) untouched — Cyrillic and
# Latin are unrelated scripts to Unicode, not a compatibility pair. The real tool for cross-script
# look-alikes is Unicode's own confusables table (TR39); pulling in the whole table is out of
# proportion to comparing against nine fixed ASCII identifiers, so this is a small, explicit,
# tested table scoped to exactly the letters those nine words use.
#
# **This table covers Cyrillic only — there is not a single Greek entry**, and it is not a gate.
# Verified gaps prove why it could never be one: Greek small omicron
# (`οwner:`, U+03BF), Greek capital epsilon (`Εntity:`, U+0395), Turkish dotless ı (`entıty:`,
# U+0131), small-caps letterforms (`ᴇɴᴛɪᴛʏ:`), a zero-width joiner inside the word, and a combining
# acute instead of a precomposed character all fold to themselves here and would keep reading
# as "not entity/owner" to `normalize_key` forever — enumerating confusables does not converge, and
# a fourth script or diacritic trick is always one edit away. This table earns its place for
# **strip-normalization** (`_strip_keys`, below): the Cyrillic spellings it DOES cover are cleaned
# from a stamped page silently rather than causing a refusal. The GATE is elsewhere —
# `gates.gate_frontmatter`'s top-level-key whitelist (`^[a-z_][a-z0-9_.-]*$`), which refuses
# every one of the six spellings above categorically, by construction, whether or not this table
# is ever extended to name them.
_HOMOGLYPH_FOLD = str.maketrans({
    "а": "a", "А": "a",   # Cyrillic а/А (U+0430/U+0410) -> Latin a
    "с": "c", "С": "c",   # Cyrillic с/С (U+0441/U+0421) -> Latin c
    "е": "e", "Е": "e",   # Cyrillic е/Е (U+0435/U+0415) -> Latin e
    "і": "i", "І": "i",   # Cyrillic/Ukrainian і/І (U+0456/U+0406) -> Latin i
    "о": "o", "О": "o",   # Cyrillic о/О (U+043E/U+041E) -> Latin o
    "р": "p", "Р": "p",   # Cyrillic р/Р (U+0440/U+0420) -> Latin p
    "ѕ": "s", "Ѕ": "s",   # Cyrillic ѕ/Ѕ (U+0455/U+0405) -> Latin s
    "у": "y", "У": "y",   # Cyrillic у/У (U+0443/U+0423) -> Latin y
    "х": "x", "Х": "x",   # Cyrillic х/Х (U+0445/U+0425) -> Latin x
})


def normalize_key(key: str) -> str:
    """A confusables-folded, NFKC + casefolded key — the same normalization doctrine `path_key`
    already applies to a path (NFC + casefold), extended here with `_HOMOGLYPH_FOLD` (see its own
    comment for why NFKC alone does not cover a cross-script look-alike) and NFKC (compatibility
    folding NFC does not do — a full-width variant, certain compatibility characters). Findings
    cycle 2, B1: `Entity:` (mixed case) and `еntity:` (Cyrillic е, U+0435) are both bypasses of an
    exact-string `in keys` test — `_strip_keys` and `gates.gate_frontmatter`'s `FORBIDDEN_PAGE_KEYS`
    check both compare on THIS form now, so a spelling that reads as `entity`/`owner` to a human
    but not to `==` still strips/refuses like the real thing."""
    folded = (key or "").translate(_HOMOGLYPH_FOLD)
    return unicodedata.normalize("NFKC", folded).casefold()

# Fields the SERVER computes; a document may declare them and be ignored. `status` is forced
# rather than removed (every page must carry one), `owner` is removed outright — a `developing`
# page has a submitter, not an accountable owner, and a capture must not be able to assign
# responsibility to a third party.
#
# `entity` is in this set because a page's aboutness is stamped from the anchoring outcome
# `gate_anchoring` already verified — a capture drafting its own `entity: [...]` has that value
# deleted and rewritten. `verification` is a strip-only key: nothing computes a verdict, and an
# agent must not be able to assert a check that does not run.
#
# **Containment here is enforced by two things, not one.** This line recognizes every spelling a
# duplicate could be declared under (bare, `"quoted"`, `'quoted'` — the same three the knowledge
# repo's own linter reads), because matching bare keys alone would let a quoted `"entity": [...]`
# survive beside the server's own line, invisible to both `yaml.safe_load` (last-key-wins) and to
# this module's `declared_keys`. AND `gates.gate_frontmatter`'s raw-text duplicate-key
# post-condition (`page.duplicate_top_level_keys`) vetoes a filed page outright if a server-owned
# key is declared more than once regardless of spelling — the backstop for exactly that failure
# mode, in case this line ever regresses.
SERVER_OWNED_KEYS = ("submitted_by", "verification", "acl", "content_hash", "id", "owner",
                     "status", "as_of", "entity")


def split_frontmatter(text: str) -> tuple[str, str]:
    """`(frontmatter_body, rest)`. `("", text)` when there is no leading `---` block."""
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return "", text or ""
    return match.group(1), (text or "")[match.end():]


def duplicate_top_level_keys(front: str) -> set[str]:
    """Top-level keys declared MORE THAN ONCE in a frontmatter BLOCK (already split from the page
    — no leading/trailing `---`), by a REAL PARSER'S OWN notion of key identity — entirely
    independent of `_match_key`'s line-based spelling rules.

    Findings cycle 1, 4.1's second half: `yaml.safe_load` takes the LAST occurrence of a duplicate
    key, and `stamp_server_fields` appends the server's own line last — so a page carrying a
    capture-drafted `"entity": [...]` ALONGSIDE the server's `entity: [...]` can satisfy a naive
    "does the parsed value match what the server stamped" check (`gates.gate_frontmatter`) even
    though the raw frontmatter still carries the capture's own attempt beside it. A post-condition
    that a duplicate can satisfy is not a post-condition; this is the raw-text check that closes
    that gap, independent of what the value parses to.

    **Findings cycle 2, B1: this used to call `_match_key` itself — the same regex the strip
    (`_strip_keys`) uses — which made the "second, independent post-condition" the docstring
    claimed not actually independent.** A spelling `_match_key` cannot see is a spelling this
    check could not see either: YAML explicit-key syntax (`? entity` / `: [...]`), a hex escape
    inside a quoted key (`"entit\\x79"`), a UTF-8 BOM as the block's first byte — all three parse
    to the plain string `"entity"` to a REAL YAML parser and are therefore genuine duplicates of
    the server's own `entity:` line, but none of them matches `_KEY_RE`. Delegating key identity
    to PyYAML's own composer (`yaml.compose`, which builds the parse tree WITHOUT constructing a
    dict, so a duplicate key is never silently overwritten before we get to look) closes that
    class categorically: whatever a future YAML edge case reads as the string `"entity"`, this
    function agrees, because it asks the same parser the generator (and every real consumer)
    uses — not a second regex trying to keep up with it.

    Mixed-case (`Entity:`) and homoglyph (`еntity:`, Cyrillic е) spellings are NOT duplicates
    under this definition — they are genuinely DIFFERENT strings to a real parser too, so PyYAML
    itself would keep both as separate dict keys. That bypass is closed separately, by comparing
    on `normalize_key` in `_strip_keys` (so the forged line never survives to be committed at
    all) and in `gates.FORBIDDEN_PAGE_KEYS`'s presence check.

    **Findings cycle 3: this function does NOT agree with the loader categorically, and the old
    docstring's claim that it did was false for one shape — a top-level YAML merge key (`<<:`).**
    `root.value` only enumerates the keys THIS mapping declares directly; a key contributed by a
    merge (`<<: *anchor`, where `anchor` itself declares e.g. `entity:`) lives one level down, on
    the aliased node, and never appears in `root.value` at all — so this function reports no
    duplicate even when the CONSTRUCTED mapping (`yaml.safe_load`) would receive a merged `entity`
    key sitting beside an explicit one. That divergence is non-exploitable today: YAML's own merge
    rule has explicit keys win over merged ones, and `gates.FORBIDDEN_PAGE_KEYS` reads the
    RESOLVED dict (post-merge), so a `SERVER_OWNED_KEYS` name introduced only through a merge is
    still caught there. It is closed categorically anyway, the same way the explicit-key and BOM
    shapes are: `gates.gate_frontmatter` refuses a top-level `<<:` line OUTRIGHT, before any of
    this runs, because a merge key has no legitimate use in this repo's page dialect either.
    """
    if not (front or "").strip():
        return set()
    try:
        root = yaml.compose(front, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return set()   # gate_frontmatter's own yaml.safe_load step reports the parse failure
    if root is None or not isinstance(root, yaml.MappingNode):
        return set()
    loader = yaml.SafeLoader("")
    seen, dupes = set(), set()
    for key_node, _value_node in root.value:
        try:
            key = loader.construct_object(key_node, deep=True)
        except yaml.YAMLError:
            continue
        if not isinstance(key, str):
            continue
        if key in seen:
            dupes.add(key)
        seen.add(key)
    return dupes


def _strip_key_lines(lines: list[str], keys) -> list[str]:
    """`_strip_keys`'s algorithm, on a LIST OF LINES it already has rather than a raw string it
    would otherwise have to re-join and re-split.

    Exported as `strip_key_lines` for `gates.gate_body_rewrite`,
    which used to carry its OWN naive `line.startswith(f"{key}:")` stripper
    (`gates._strip_top_level_keys`) for the exact same job — removing server-owned
    `status`/`owner` lines from a byte comparison — and disagreed with this one
    about a block-style value's continuation lines (this one drops them; the old one did not),
    which could produce a spurious veto on a well-formed page. One implementation of "what lines
    does a top-level key occupy", not two.
    """
    normalized_keys = {normalize_key(k) for k in keys}
    out, dropping = [], False
    for line in lines:
        matched = _match_key(line)
        top_level = matched is not None and not line[:1].isspace()
        if top_level:
            dropping = normalize_key(matched[0]) in normalized_keys
            if dropping:
                continue
        elif dropping and (not line.strip() or line[:1].isspace() or line.lstrip().startswith("-")):
            continue        # a continuation of the key we are dropping
        else:
            dropping = False
        out.append(line)
    return out


# Public alias — see `_strip_key_lines`'s own docstring.
strip_key_lines = _strip_key_lines


def _strip_keys(front: str, keys) -> list[str]:
    """Drop top-level `key:` lines and every indented continuation line beneath them.

    Line-based on purpose: a YAML round-trip would reformat the whole block (quoting, list
    style, comment loss) and the page contract is diffed by humans. Only the keys we are about
    to rewrite are touched; everything else survives byte for byte.

    Compares on `normalize_key`, not raw `==` — `Entity:`/`еntity:`
    (Cyrillic е) must strip exactly like `entity:` does, or the capture's own forged declaration
    survives beside the server's stamped line under a spelling `==` cannot see.
    """
    return _strip_key_lines(front.splitlines(), keys)


def _yaml_list(values) -> str:
    """A YAML/JSON flow-sequence literal (JSON scalars are valid YAML, and `json.dumps` gives us
    a real escaper for free instead of a bare `f'"{v}"'`, which produces invalid, unparseable
    YAML for any value containing a `"` or a backslash)."""
    return "[" + ", ".join(json.dumps(str(v)) for v in values) + "]"


def stamp_server_fields(text: str, *, submitted_by: str,
                        acl: list[str] | None, as_of: str, entity: list[str] = ()) -> str:
    """Rewrite a page's frontmatter so every server-owned field is the SERVER's value.

    Whatever the material declared is deleted first and re-written from arguments, so a
    pre-drafted page asserting `status: mature`, `submitted_by: someone.else@example.com` or
    `verification: verified` yields a filed page carrying `developing` and the real submitter —
    and no `owner` or `verification` line at all. `verification` stays in `SERVER_OWNED_KEYS`
    precisely so it is STRIPPED: nothing computes a verification verdict, so no page may claim
    one, least of all a page the agent drafted.

    `acl` of `None` omits the field entirely, which is how the page contract spells "open"
    (`acl_rules.resolve` returns `None` both when ACLs are off and when the matching rule
    resolves to no labels).

    `entity` is ALWAYS written, unlike `acl` — an empty list is itself the checked, explicit
    company-wide declaration, not the absence of one, so
    `entity: []` must be distinguishable from no line at all. `_yaml_list` renders it whether
    empty or not, using the same line-based rewrite (no YAML round-trip) every other server-owned
    field here already uses.
    """
    front, rest = split_frontmatter(text)
    if not front:
        # No frontmatter at all: the contract linter will reject the page for missing required
        # fields, which is the honest outcome. We still stamp, so the refusal names the real
        # problem (a page with no frontmatter) rather than a missing `submitted_by`.
        front, rest = "", text or ""

    kept = _strip_keys(front, set(SERVER_OWNED_KEYS))
    stamped = [
        f"status: {FILED_STATUS}",
        f"as_of: {as_of}",
        f"submitted_by: {submitted_by}",
        f"entity: {_yaml_list(entity)}",
    ]
    if acl:
        stamped.append(f"acl: {_yaml_list(acl)}")
    front_matter = "\n".join([*kept, *stamped]).strip("\n")

    # Exactly ONE blank line between the closing `---` and the body. The old expression preserved
    # `rest` as-is, and `split_frontmatter` consumes the newline that closes the block — so every
    # filed page came out with its body glued to its frontmatter (`---` immediately followed by
    # `# Title`). Normalizing rather than preserving also makes the stamp idempotent: re-stamping
    # a page it already stamped cannot accumulate blank lines.
    body = rest.lstrip("\n")
    return f"---\n{front_matter}\n---\n" + (f"\n{body}" if body else "")


def stamp_source_fields(text: str, *, submitted_by: str, as_of: str,
                        content_hash: str, extracted_at: str, tier: str = "1",
                        page_id: str = "") -> str:
    """The meeting flow's ONE `sources/` page — the transcript extraction — validated under the
    machine/provenance field group, never the fast-lane group `stamp_server_fields` above writes.
    That function's own docstring names exactly why it does NOT stamp `content_hash`/`id`: those
    belong to source pages, and an ordinary fast-lane page is not one.
    The meeting flow's source page IS one, so it gets this sibling instead — same line-based
    strip-then-append discipline (no YAML round-trip), a different field set.

    `content_hash` is `sha256:<hex>` of the archived material (the same digest
    `capture.schema.material_digest` computed at drop time — recomputed here from the bytes this
    run verified against, so the page's own claim and the evidence-store key can never disagree).
    `extracted_at` is this run's timestamp. `tier` defaults to `"1"` — `ops/templates/source.md`'s
    own vocabulary is "1 primary · 2 second-hand · 3 AI-generated", and a Granola transcript is a
    direct recording of the actual meeting: primary, not AI-generated (the tier is about
    PROVENANCE, not about how noisy speech-to-text is).

    `entity`/`acl` are deliberately absent from this signature: a source page under
    `sources/meetings/` is provenance for the whole capture, not itself anchored to one entity —
    it is the DECISION pages that must anchor, not the transcript. `status`/`as_of`/
    `submitted_by` are stamped for the same accountability reason every fast-lane page carries
    them.

    `page_id` (ADR 028 D6): the part's EXPLICIT chain identity, computed by the
    producer (`processing._build_source_parts`: `<stem>` for part 1, `<stem>#p<n>` after) and
    stamped here as `id:` — the field `index.corpus` already prefers over the filename stem, so
    the chain collapse keys on a declared fact and the older `-p<n>` filename inference steps
    down to belt-and-braces for pages filed before this existed. Quoted, because `#` starts a
    YAML comment unquoted. Empty means "stamp no id" — no caller passes empty today, but the
    parameter fails soft rather than writing `id: ""`.
    """
    front, rest = split_frontmatter(text)
    if not front:
        front, rest = "", text or ""
    kept = _strip_keys(front, set(SERVER_OWNED_KEYS) | {"content_hash", "extracted_at", "tier"})
    stamped = [
        f"status: {FILED_STATUS}",
        f"as_of: {as_of}",
        f"submitted_by: {submitted_by}",
        f'content_hash: "sha256:{content_hash}"',
        f'extracted_at: "{extracted_at}"',
        f"tier: {tier}",
    ]
    if page_id:
        stamped.append(f'id: "{page_id}"')
    front_matter = "\n".join([*kept, *stamped]).strip("\n")
    body = rest.lstrip("\n")
    return f"---\n{front_matter}\n---\n" + (f"\n{body}" if body else "")


def add_source_citation(text: str, stem: str) -> tuple[str, list[str]]:
    """Make a drafted page's `sources:` cite `[[<stem>]]` — the fast lane's
    source attachment, where CODE guarantees the synthesis cites the verbatim source page rather
    than asking the agent to (the same derived-not-drafted posture as the meeting flow's
    `_build_decision_page`, which writes the citation into pages code itself builds; here the
    agent drafted the page, so the citation is MERGED in at stamp time instead).

    Returns `(new_text, final_list)` — the caller records `final_list` in
    `ctx.stamped_by_path` so `gate_frontmatter`'s output-equality post-condition covers this
    field like every other stamped one.

    The agent's own citations survive: the existing `sources:` value is read with a real YAML
    parser (both flow and block styles), the link appended if absent, and the line rewritten in
    the flow style every template uses. Unparseable frontmatter yields `[link]` alone — harmless,
    because `gate_frontmatter` refuses the page as `unparseable` before the value matters.
    Idempotent, like the stamps beside it.
    """
    front, rest = split_frontmatter(text)
    if not front:
        front, rest = "", text or ""
    link = f"[[{stem}]]"
    try:
        parsed = yaml.safe_load(front) if front.strip() else {}
    except yaml.YAMLError:
        parsed = {}
    declared = parsed.get("sources") if isinstance(parsed, dict) else None
    values = [str(v) for v in declared] if isinstance(declared, list) else []
    if link not in values:
        values.append(link)
    kept = _strip_keys(front, ("sources",))
    front_matter = "\n".join([*kept, f"sources: {_yaml_list(values)}"]).strip("\n")
    body = rest.lstrip("\n")
    return f"---\n{front_matter}\n---\n" + (f"\n{body}" if body else ""), values


def open_for_rewrite(full: str):
    """Open an existing page for writing, refusing to follow a symlink.

    Shared by the two things that rewrite a page in place — the server-field stamp
    (`processing._stamp`) and the declared additive edits (`edits.apply`). Both take their paths
    from what the agent produced or declared, and `open(p, "w")` follows a symlink: a page replaced
    by a link to something outside the worktree would be written THROUGH that link, wherever it
    pointed, as the worker. The zone gate refuses a typechange and `edits.validate` refuses a
    symlinked target, but both of those run either after or before the write, so the write itself
    does not rely on them.
    """
    if os.path.islink(full):
        raise WorktreeError(f"refusing to write through a symlink at {os.path.basename(full)}")
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(full, flags), "w", encoding="utf-8")


def open_for_new(full: str):
    """Create a BRAND-NEW page, refusing if anything already exists at that path at all —
    symlink or regular file alike — atomically: the existence check and the write are the SAME
    syscall (`O_EXCL`), so nothing can be swapped in between a caller's own "does this path
    already exist" check and the write itself. A bare `open(path, "w")` truncates through
    anything already there — including a symlink — with no existence check at all, and the fast
    lane's `agent.confined_write` allow-lists only paths that do NOT yet exist; this is what
    makes that invariant hold at the moment of writing rather than a moment before it.

    `open_for_rewrite` (above) is the sibling for the other case — REWRITING a page that is
    already known to exist — and cannot be reused here: it opens with no `O_CREAT` at all, so it
    raises `FileNotFoundError` on a path that is not there yet, which is exactly the shape a
    brand-new proposal always starts from.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(full, flags, 0o644), "w", encoding="utf-8")


# ── page names: UTF-8 is the norm, not an exception ───────────────────────────────────────────
# A page filename carries the page's TITLE — it is what a wikilink resolves by and what a human
# reads in `git log`. So the rule is "keep the title", not "keep the ASCII of the title": a name
# may hold any character except the two a filename genuinely cannot represent, the path separator
# and a control byte.
#
# This constant exists because the opposite rule shipped and was permanent. A title sanitizer
# spelled `[^A-Za-z0-9 ]+ -> " "` turned "Zürich" into "Z rich" — in the filename, the H1, the
# `title` frontmatter AND the commit subject — and three pages on the real `main` still carry it.
# The body was fine throughout, so nothing about the encoding was broken; a whitelist of ASCII was.
# The direction of the fix is the important part: where a character truly cannot be in a filename,
# the NAME is refused (`unnameable_reason`, the zone gate) rather than silently mangled, because a
# mangled name is a wrong page title nobody notices until it is in history.
_FILENAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f/\\]")

# How long a page's STEM may be, **in UTF-8 BYTES** — the unit the filesystem actually counts.
# Linux and macOS both cap one path component at `NAME_MAX` = 255 bytes, and a name over it fails
# the `open` with `ENAMETOOLONG` rather than being truncated. 200 leaves the `.md` suffix and room
# for a `-p2`-style discriminator under that ceiling with margin.
#
# **Bytes, not characters, and the distinction is the whole point**: 200 accented or CJK characters
# are 400–600 bytes, so a character bound would pass names the filesystem refuses — and the corpus
# this runs on names European customers and is expected to carry non-ASCII titles routinely.
#
# Enforced HERE rather than at a writer, so the gate and the writer share one answer: a title over
# it is a REPAIRABLE refusal ("write a shorter title") instead of an `OSError` escaping every
# handler as stage `unexpected`, with the item's agent spend already banked.
MAX_PAGE_STEM_BYTES = 200


def unnameable_reason(stem: str) -> str:
    """Why this page name cannot be filed, or `""` when it is fine.

    Deliberately short: a path separator, a control byte, or a name the filesystem itself will not
    take. Accents, ideographs, emoji and punctuation all pass — a corpus naming European customers
    carries these routinely, so they are the normal case rather than the edge one.

    **The argument is the STEM — the name WITHOUT `.md`** — and the parameter is called that
    because both callers have to agree about which string this bound is on. It was `basename`, and
    `gate_zone` duly passed one while `processing._write_ordinary_page` passed the stem it was
    about to build a filename from: three bytes of disagreement, which turned a 198-byte title into
    a page the writer accepted and the gate then vetoed as a LIBRARIAN FAULT. One bound with two
    meanings is not one bound, and a parameter name is the cheapest place to say which.
    """
    if not stem:
        return "it has no name at all"
    if _FILENAME_FORBIDDEN.search(stem):
        return ("its name contains a character a filename cannot carry (a path separator or a "
                "control character) — spell the title without it rather than approximating it")
    size = len(stem.encode("utf-8"))
    if size > MAX_PAGE_STEM_BYTES:
        return (f"its name is {size} bytes long and a filename may not exceed "
                f"{MAX_PAGE_STEM_BYTES} — write a shorter title (accented and non-Latin "
                f"characters cost more than one byte each, so this is not a character count)")
    return ""


# ── additive edits to a page that already exists ──────────────────────────────────────────────
# Performed by CODE, from the agent's declaration. Both
# functions below are additive by construction and that is the whole point: the agent may no
# longer touch an existing page at all, so these are the only two shapes an existing page can
# change in, and `gates.gate_body_rewrite` still judges the result.
_RELATED_KEY = "related"


def _wikilink(name: str) -> str:
    return f"[[{name}]]"


def _parse_list_value(text: str) -> list[str]:
    """A YAML sequence value into a list of strings, `[]` when it is not one."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    return []


def _related_span(front_lines: list[str]) -> tuple[int, int]:
    """`(start, end)` of the top-level `related:` block in a frontmatter body, `(-1, -1)` when
    there is none. `end` is exclusive and covers block-sequence continuation lines."""
    for index, line in enumerate(front_lines):
        matched = _match_key(line)
        if not matched or line[:1].isspace() or matched[0] != _RELATED_KEY:
            continue
        end = index + 1
        while end < len(front_lines) and (front_lines[end][:1].isspace()
                                          or front_lines[end].lstrip().startswith("- ")):
            if _match_key(front_lines[end]) and not front_lines[end][:1].isspace():
                break
            end += 1
        return index, end
    return -1, -1


def related_links(text: str) -> list[str]:
    """The raw values the page's `related:` frontmatter declares (`["[[A]]", ...]`).

    Reads both YAML spellings the repo contains: the flow list every page actually uses
    (`related: ["[[A]]"]`) and the block list a template could grow into.
    """
    front, _ = split_frontmatter(text)
    lines = front.splitlines()
    start, end = _related_span(lines)
    if start < 0:
        return []
    inline = _match_key(lines[start])[1].strip()
    if inline:
        return _parse_list_value(inline)
    # A block sequence: dedented, the item lines ARE a top-level YAML sequence.
    return _parse_list_value("\n".join(line.strip() for line in lines[start + 1:end]
                                       if line.strip()))


def related_links_from_line(line: str) -> list[str] | None:
    """The values ONE `related:` line declares, or `None` when that cannot be established.

    `None` covers three different "unknown"s deliberately: the line is not a top-level `related:`
    key at all, its value is not a YAML list, or it is the bare `related:` that opens a block list
    (whose items live on other lines, so this line alone says nothing). `gates.gate_body_rewrite`
    treats every one of them as "unprovable" and refuses — an unparseable before-value must never
    be mistaken for an empty one.
    """
    matched = _match_key(line or "")
    if not matched or (line or "")[:1].isspace() or matched[0] != _RELATED_KEY:
        return None
    value = matched[1].strip()
    if not value:
        return None
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return None
    return [str(v) for v in parsed] if isinstance(parsed, list) else None


def frontmatter_lines(text: str) -> list[str]:
    """The page's frontmatter block as lines, normalized the way `with_related_link` reassembles it
    (`"\\n".join(lines).strip("\\n")`) so a before/after comparison cannot fail on a blank line the
    writer would have dropped anyway. `[]` when there is no frontmatter block at all."""
    front, _ = split_frontmatter(text)
    stripped = front.strip("\n")
    return stripped.split("\n") if stripped else []


def body_lines(text: str) -> list[str]:
    """The page's body as lines, normalized the way both writers reassemble it — leading blank
    lines dropped (`with_related_link`'s `rest.lstrip("\\n")`) and trailing ones dropped
    (`with_callout`'s `text.rstrip("\\n")`). Comparing these two lists is what proves an edit
    additive without reading a rendered diff."""
    _, rest = split_frontmatter(text)
    stripped = rest.strip("\n")
    return stripped.split("\n") if stripped else []


def related_declaration(text: str) -> tuple[list[str], list[str] | None]:
    """`(the raw lines of the top-level `related:` block, the links it declares)`.

    The links are `None` when the declaration exists but what it USED to hold cannot be
    established — an unparseable value, or a bare `related:` opening a block list with no items.
    `gate_body_rewrite` treats every such case as unprovable and refuses: "I could not tell what
    was lost" must never be read as "nothing was lost". `([], [])` means the page declares no
    `related:` key at all, which is provably an empty link set rather than an unknown one.

    Returning the raw BLOCK alongside the parsed links is what lets a caller compare the rest of
    the frontmatter byte-for-byte while judging this one field semantically — the only field an
    additive edit is allowed to rewrite (`with_related_link`, flow-list spelling).
    """
    lines = frontmatter_lines(text)
    start, end = _related_span(lines)
    if start < 0:
        return [], []
    block = lines[start:end]
    inline = _match_key(lines[start])[1].strip()
    if inline:
        return block, related_links_from_line(lines[start])
    items = [line.strip() for line in lines[start + 1:end] if line.strip()]
    if not items:
        return block, None
    parsed = _parse_list_value("\n".join(items))
    return block, (parsed or None)


def with_related_link(text: str, name: str) -> tuple[str, bool]:
    """Add `[[name]]` to the page's `related:` list. Returns `(text, changed)`.

    Three shapes, all of them additions:
    - **no `related:` key** — a new line is appended to the frontmatter;
    - **a block list** — a new `  - "[[name]]"` item line is inserted, touching nothing;
    - **a flow list** — the one line is rewritten with the same items plus this one. Every page in
      the repo uses this spelling, so it is the common case, and it is the one case where a line
      is replaced rather than added. `gate_body_rewrite` proves that replacement additive by
      parsing both sides and requiring the link set to GROW — a rewrite that dropped a link is
      refused there like any other.
    """
    link = _wikilink(name)
    front, rest = split_frontmatter(text)
    if not front:
        return text, False              # no frontmatter: the linter refuses the page anyway
    lines = front.splitlines()
    start, end = _related_span(lines)

    if start < 0:
        lines.append(f"{_RELATED_KEY}: [{_yaml_scalar(link)}]")
    else:
        existing = related_links(text)
        if link in existing:
            return text, False
        inline = _match_key(lines[start])[1].strip()
        if inline:
            values = existing + [link]
            lines[start] = f"{_RELATED_KEY}: {_yaml_list(values)}"
        else:
            indent = " " * 2
            for candidate in lines[start + 1:end]:
                if candidate.lstrip().startswith("- "):
                    indent = candidate[:len(candidate) - len(candidate.lstrip())]
                    break
            lines.insert(end, f"{indent}- {_yaml_scalar(link)}")

    body = rest.lstrip("\n")
    front_matter = "\n".join(lines).strip("\n")
    return f"---\n{front_matter}\n---\n" + (f"\n{body}" if body else ""), True


def _yaml_scalar(value: str) -> str:
    return f'"{value}"'


CALLOUT_STYLES = {
    # kind -> (obsidian callout type, the phrase that opens the line)
    "overlap": ("NOTE", "Overlaps with"),
    "contradiction": ("WARNING", "Contradiction with"),
}

# The three shapes an existing page may change in, and nothing else. `backlink` is the reciprocal
# `related:` entry on its own; the two callout kinds add that link AND the callout, because the
# page contract asks both sides of an overlap to carry both.
#
# Here rather than in `edits.py` so `agent.parse_outcome` can validate a declared kind at the
# outcome boundary without importing the applier — `page` is this package's placement vocabulary
# and an edit kind is part of it.
EDIT_KINDS = ("backlink", *CALLOUT_STYLES)
NOTE_REQUIRED_KINDS = tuple(CALLOUT_STYLES)


def with_callout(text: str, *, kind: str, name: str, note: str) -> str:
    """Append an overlap/contradiction callout naming `[[name]]` to the end of the page body.

    An append and nothing else — no existing line is read, moved or replaced, so the diff for it
    contains only `+` lines whatever the page looked like before.
    """
    callout, phrase = CALLOUT_STYLES[kind]
    base = (text or "").rstrip("\n")
    return (f"{base}\n\n> [!{callout}] {phrase} {_wikilink(name)}\n"
            f"> {' '.join(str(note or '').split())}\n")
