"""Placement policy, path identity, and the server-owned half of a filed page's frontmatter.

`PAGE_TYPES` is the one placement table; `stamp_server_fields` rewrites the fields only the
server may assert; `path_key`/`is_inside` are the one answer to "same page?" and containment.
"""
import json
import os
import re
import unicodedata
from dataclasses import dataclass

import yaml

from stigmergy.librarian.errors import WorktreeError

# A constant, not a default: nothing the material claims can change it.
FILED_STATUS = "developing"


@dataclass(frozen=True)
class PageType:
    """ONE row of the page vocabulary."""
    name: str
    folder: str = ""     # "" unless the fast lane may CREATE one
    label: str = ""      # how a person is told about it in a sentence ("an entity page")
    reason: str = ""     # why the fast lane may not create it, in the submitter's language
    provenance: bool = False   # a RECORD OF AN EVENT, never a knowledge destination


# Mirrors the contract linter's own `VALID_TYPES`. Only types carrying a `folder` may be created
# here; everything else lands in `triage` rather than being downgraded. Every label carries its
# article and the word `page`, because `report.triage_type` drops it in whole.
_IDENTITY_REASON = "identity pages are created through a steward's review, not the fast lane"
# Named because three separate rules ask "is this an identity page?" and must not drift:
# `gates.gate_body_rewrite`'s permitted-rewrite branch, `repair.entity_body`'s validator, and this
# table itself.
ENTITY_PAGE_TYPE = "entity"
PAGE_TYPES = (
    PageType("note", folder="wiki/notes", label="a note page"),
    PageType("decision", folder="wiki/decisions", label="a decision page"),
    PageType("concept", folder="wiki/concepts", label="a concept page"),
    PageType(ENTITY_PAGE_TYPE, label="an entity page", reason=_IDENTITY_REASON),
    PageType("source", label="a source page", provenance=True,
             reason="source pages are written by code from a captured document, never drafted"),
    PageType("meeting", label="a meeting page", provenance=True,
             reason="meeting pages arrive with the meeting distiller"),
    PageType("view", label="a view page",
             reason="view pages are regenerated from an entity's members, never captured"),
)

_BY_NAME = {page_type.name: page_type for page_type in PAGE_TYPES}

ALL_PAGE_TYPES = frozenset(_BY_NAME)
FOLDER_BY_TYPE = {p.name: p.folder for p in PAGE_TYPES if p.folder}
FAST_LANE_TYPES = frozenset(FOLDER_BY_TYPE)

# `entity: []` is a checked company-wide declaration on a `wiki/**` page, but "about nothing" on a
# PROVENANCE page — which `stigmergy.views` therefore makes a member of nothing, by contract.
PROVENANCE_PAGE_TYPES = frozenset(p.name for p in PAGE_TYPES if p.provenance)


def is_provenance_type(page_type: str) -> bool:
    """Does this page type record an EVENT rather than assert knowledge? An unknown or empty type
    answers False — the conservative direction, keeping the company-wide confirmation in place."""
    return str(page_type or "").strip().lower() in PROVENANCE_PAGE_TYPES


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
    """THE shared base every placement question reads, so "known" and "creatable" cannot drift."""
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
    """How a person is told about this type in a sentence; "" for an unknown type."""
    return classify_page_type(page_type).label


def ensure_creatable(page_type: str) -> TypePolicy:
    """The write guard; raises `ValueError` for anything the fast lane may not mint. The zone gate
    calls it with the type derived from the landed folder, so the agent's judgment is never it."""
    policy = classify_page_type(page_type)
    if not policy.creatable:
        raise ValueError(f"the fast lane cannot create a {policy.page_type or 'typeless'!r} "
                         f"page: {policy.reason}")
    return policy


def folder_for(page_type: str) -> str:
    """The folder a creatable type belongs in. Raises for anything else — same guard."""
    return ensure_creatable(page_type).folder


def type_for_folder(path: str) -> str:
    """The type a path's folder implies, or "" when the folder is not a fast-lane one."""
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    for page_type, known_folder in FOLDER_BY_TYPE.items():
        if folder == known_folder:
            return page_type
    return ""


# ── path identity: when are two spellings the SAME page ───────────────────────────────────────


def path_key(rel: str) -> str:
    """One comparable spelling for a repo-relative path: NFC + casefold. On a case- and
    normalization-insensitive filesystem a raw comparison calls a re-cased or NFD-spelled existing
    title new and the write lands on the human's page; the knowledge repo's linter refuses
    duplicate basenames case-insensitively anyway."""
    return unicodedata.normalize("NFC", rel or "").casefold()


def path_keys(paths) -> set[str]:
    """`path_key` over a collection. Recomputed per call, never cached: a second representation
    of "the pages that already exist" is a second thing that can go stale."""
    return {path_key(p) for p in (paths or ())}


def is_inside(root: str, candidate: str) -> bool:
    """Does `candidate` (absolute or `root`-relative) resolve inside `root`? BOTH sides go through
    `realpath`: an unresolved root matches nothing when it is itself a symlink, and resolving the
    candidate is what checks DIRECTORY components, which an `islink` test on the leaf misses."""
    resolved_root = os.path.realpath(root)
    try:
        resolved = os.path.realpath(os.path.join(resolved_root, candidate))
    except (OSError, ValueError):
        return False
    return resolved == resolved_root or resolved.startswith(resolved_root + os.sep)


# ── frontmatter ───────────────────────────────────────────────────────────────────────────────
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---(\n|\Z)", re.S)

# THREE spellings — bare, "double-quoted", 'single-quoted' — because a quoted `"entity": [...]`
# a bare-only pattern misses survives `_strip_keys` under last-key-wins. The bare branch is
# `[^\W\d]`, not `[A-Za-z_]`: a Cyrillic `еntity:` is a valid key and must read as one before
# normalization can judge it.
_KEY_RE = re.compile(r"""^(?:"([^"]*)"|'([^']*)'|([^\W\d][\w.-]*))\s*:(.*)$""")


def _match_key(line: str) -> tuple[str, str] | None:
    """`(key, value_str)` for a line opening a `key:` pair under any of the three spellings;
    `None` otherwise. Every reader goes through here, never through `_KEY_RE` directly."""
    m = _KEY_RE.match(line or "")
    if not m:
        return None
    key = next((g for g in m.groups()[:3] if g is not None), "")
    return key, m.group(4)


# Cyrillic letters indistinguishable from the Latin ones in `SERVER_OWNED_KEYS`; NFKC collapses
# compatibility variants only, never across scripts, so these survive it. Strip-normalization only,
# never a gate — enumerating confusables does not converge. The gate is `gate_frontmatter`'s
# top-level-key whitelist, which refuses every non-ASCII spelling categorically.
_HOMOGLYPH_FOLD = str.maketrans({
    "а": "a", "А": "a",
    "с": "c", "С": "c",
    "е": "e", "Е": "e",
    "і": "i", "І": "i",
    "о": "o", "О": "o",
    "р": "p", "Р": "p",
    "ѕ": "s", "Ѕ": "s",
    "у": "y", "У": "y",
    "х": "x", "Х": "x",
})


def normalize_key(key: str) -> str:
    """Explicit homoglyph fold, then NFKC, then casefold — NFKC alone does not cover a cross-script
    look-alike. `_strip_keys` and `gates.FORBIDDEN_PAGE_KEYS` compare on THIS form, so a spelling
    that reads as `entity`/`owner` to a human but not to `==` still strips or refuses."""
    folded = (key or "").translate(_HOMOGLYPH_FOLD)
    return unicodedata.normalize("NFKC", folded).casefold()

# Fields the SERVER computes; a document may declare them and be ignored. `status` and `entity` are
# forced; `owner` and `verification` are stripped and never re-stamped — nothing computes a
# verdict, so no page may claim one.
SERVER_OWNED_KEYS = ("submitted_by", "verification", "acl", "content_hash", "id", "owner",
                     "status", "as_of", "entity")


def split_frontmatter(text: str) -> tuple[str, str]:
    """`(frontmatter_body, rest)`. `("", text)` when there is no leading `---` block."""
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return "", text or ""
    return match.group(1), (text or "")[match.end():]


def duplicate_top_level_keys(front: str) -> set[str]:
    """Top-level keys declared MORE THAN ONCE, by `yaml.compose`'s notion of key identity — a parse
    tree, so a duplicate is never overwritten before we look. Mixed-case/homoglyph spellings are
    different strings here (`normalize_key` closes those); a merge key `gate_frontmatter` does."""
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
    """ONE implementation of "what lines does a top-level key occupy", continuation lines included,
    so two strippers cannot disagree about a block-style value."""
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


# Public alias for `gates.gate_body_rewrite`.
strip_key_lines = _strip_key_lines


def top_level_key_line(front_lines: list[str], key: str) -> tuple[int, str]:
    """`(index, raw value text)` of the top-level `key:` line in a frontmatter block, `(-1, "")`
    when there is none — the LOCATION half of `_strip_key_lines`, for a writer that rewrites one
    line IN PLACE rather than dropping and re-appending it.

    Compared on `normalize_key`, exactly as the stripper compares: a re-cased or homoglyph
    spelling is the line a YAML parser will read, so it is the line a rewriter must replace, or
    the page ends up declaring the field twice.
    """
    wanted = normalize_key(key)
    for index, line in enumerate(front_lines):
        matched = _match_key(line)
        if matched and not line[:1].isspace() and normalize_key(matched[0]) == wanted:
            return index, matched[1]
    return -1, ""


def _strip_keys(front: str, keys) -> list[str]:
    """Drop top-level `key:` lines and every indented continuation beneath them. Line-based on
    purpose: a YAML round-trip would reformat a block humans diff. Compares on `normalize_key`,
    not `==`, or a re-cased or homoglyph forged declaration survives."""
    return _strip_key_lines(front.splitlines(), keys)


def _yaml_list(values) -> str:
    """A YAML/JSON flow-sequence literal. `json.dumps` is a real escaper; a bare `f'"{v}"'`
    produces unparseable YAML for any value containing a quote or backslash. `ensure_ascii=False`
    because these lines are read back LITERALLY, not JSON-decoded: the contract linter resolves a
    `[[sesión]]` backlink by its bytes, and the `\\uXXXX` spelling of it is a dead link."""
    return "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in values) + "]"


def _rebuild(front_matter: str, body: str) -> str:
    """A page from its two halves, with exactly ONE blank line after the closing `---`. THE one
    reassembly every writer here goes through: normalizing the gap is what makes a stamp
    idempotent, and what lets `frontmatter_lines`/`body_lines` compare a before and an after
    without failing on a blank line the writer would have dropped anyway."""
    return f"---\n{front_matter}\n---\n" + (f"\n{body}" if body else "")


def _restamp(text: str, strip_keys, stamped_lines: list[str]) -> str:
    """Drop the top-level keys the caller names and append the values it asserts, in one pass. The
    CALLER decides what the server owns on its kind of page; the split, the strip and the rebuild
    are the same either way, so a page with no frontmatter at all still gets stamped rather than
    silently keeping a claim of its own."""
    front, rest = split_frontmatter(text)
    if not front:
        front, rest = "", text or ""
    kept = _strip_keys(front, strip_keys)
    front_matter = "\n".join([*kept, *stamped_lines]).strip("\n")
    return _rebuild(front_matter, rest.lstrip("\n"))


def stamp_server_fields(text: str, *, submitted_by: str,
                        acl: list[str] | None, as_of: str, entity: list[str] = ()) -> str:
    """Rewrite a page's frontmatter so every server-owned field is the SERVER's value. A falsy `acl`
    omits the field — the page contract's spelling of "open". `entity` is ALWAYS written, since
    `entity: []` is itself the company-wide declaration and must differ from no line at all."""
    stamped = [
        f"status: {FILED_STATUS}",
        f"as_of: {as_of}",
        f"submitted_by: {submitted_by}",
        f"entity: {_yaml_list(entity)}",
    ]
    if acl:
        stamped.append(f"acl: {_yaml_list(acl)}")
    return _restamp(text, set(SERVER_OWNED_KEYS), stamped)


def stamp_source_fields(text: str, *, submitted_by: str, as_of: str,
                        content_hash: str, extracted_at: str, tier: str = "1",
                        page_id: str = "") -> str:
    """`stamp_server_fields`'s sibling for a `sources/` page. `content_hash` is recomputed from the
    bytes this run verified, so the page's claim and the evidence-store key cannot disagree.
    `entity`/`acl` are absent by contract: a source page is provenance, not anchored."""
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
    return _restamp(text, set(SERVER_OWNED_KEYS) | {"content_hash", "extracted_at", "tier"},
                    stamped)


def add_source_citation(text: str, stem: str) -> tuple[str, list[str]]:
    """Make a drafted page's `sources:` cite `[[<stem>]]` — CODE guarantees the citation rather than
    asking the agent for it. Idempotent; the agent's own citations survive. The caller must record
    the returned list in `ctx.stamped_by_path`, or `gate_frontmatter` will not cover the field."""
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
    return _rebuild(front_matter, rest.lstrip("\n")), values


def open_for_rewrite(full: str):
    """Open an existing page for writing, refusing to follow a symlink: `open(p, "w")` writes THROUGH
    a page swapped for a link, and the write must not rely on gates running around it."""
    if os.path.islink(full):
        raise WorktreeError(f"refusing to write through a symlink at {os.path.basename(full)}")
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(full, flags), "w", encoding="utf-8")


def open_for_new(full: str):
    """Create a BRAND-NEW page, refusing if anything already exists at that path — symlink or file
    alike. The existence check and the write are the SAME syscall (`O_EXCL`), so nothing can be
    swapped in between; this is what holds `agent.confined_write`'s "must not exist yet"."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(full, flags, 0o644), "w", encoding="utf-8")


# A filename carries the page's TITLE, so a bad name is REFUSED rather than mangled.
_FILENAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f/\\]")

# The stem bound in UTF-8 BYTES — the unit the filesystem counts (`NAME_MAX` is 255), leaving room
# for `.md` and a `-p2` discriminator. Not characters: 200 CJK characters are 400-600 bytes.
MAX_PAGE_STEM_BYTES = 200


def unnameable_reason(stem: str) -> str:
    """Why this page name cannot be filed, or `""` when it is fine: a path separator, a control
    byte, or a stem over `MAX_PAGE_STEM_BYTES`. The argument is the STEM, without `.md` — both
    callers must agree which string is bounded."""
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
# Performed by CODE from the agent's declaration; `gate_body_rewrite` still judges the result.
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
    """The raw values the page's `related:` frontmatter declares, in flow or block spelling."""
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
    """The values ONE `related:` line declares, or `None` when that cannot be established (not a
    top-level `related:`, not a YAML list, or a bare `related:` opening a block). `None` is
    "unknown", never "empty" — `gate_body_rewrite` refuses on it."""
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
    """The frontmatter block as lines, normalized the way `with_related_link` reassembles it, so a
    before/after comparison cannot fail on a blank line the writer would have dropped anyway."""
    front, _ = split_frontmatter(text)
    stripped = front.strip("\n")
    return stripped.split("\n") if stripped else []


def body_lines(text: str) -> list[str]:
    """The body as lines, normalized the way both writers reassemble it. Comparing these lists is
    what proves an edit additive without reading a rendered diff."""
    _, rest = split_frontmatter(text)
    stripped = rest.strip("\n")
    return stripped.split("\n") if stripped else []


def related_declaration(text: str) -> tuple[list[str], list[str] | None]:
    """`(raw lines of the top-level `related:` block, the links it declares)`. Links are `None` when
    the declaration exists but its contents cannot be established — "I could not tell what was
    lost" must never read as "nothing was lost". `([], [])` means no `related:` key at all."""
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
    """Add `[[name]]` to the page's `related:` list; returns `(text, changed)`. Three shapes, all
    additions — the flow list is rewritten in place, and `gate_body_rewrite` proves that rewrite
    additive by requiring the link set to GROW."""
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

    front_matter = "\n".join(lines).strip("\n")
    return _rebuild(front_matter, rest.lstrip("\n")), True


def _yaml_scalar(value: str) -> str:
    """One quoted scalar, through the same real escaper as `_yaml_list` — this was the bare
    `f'"{v}"'` that function's docstring warns about, two definitions above it."""
    return json.dumps(str(value), ensure_ascii=False)


# Public alias for `repair.entity_body`, which writes one frontmatter scalar of its own and must
# quote it the way every other writer here does.
yaml_scalar = _yaml_scalar


CALLOUT_STYLES = {
    # kind -> (obsidian callout type, the phrase that opens the line)
    "overlap": ("NOTE", "Overlaps with"),
    "contradiction": ("WARNING", "Contradiction with"),
}

# The three shapes an existing page may change in: `backlink` is the reciprocal `related:` entry
# alone, a callout kind adds that link AND the callout. Here, not in `edits.py`, so
# `agent.parse_outcome` can validate a declared kind without importing the applier.
EDIT_KINDS = ("backlink", *CALLOUT_STYLES)
NOTE_REQUIRED_KINDS = tuple(CALLOUT_STYLES)


def with_callout(text: str, *, kind: str, name: str, note: str) -> str:
    """Append an overlap/contradiction callout naming `[[name]]` to the end of the page body — an
    append and nothing else, so its diff contains only `+` lines."""
    callout, phrase = CALLOUT_STYLES[kind]
    base = (text or "").rstrip("\n")
    return (f"{base}\n\n> [!{callout}] {phrase} {_wikilink(name)}\n"
            f"> {' '.join(str(note or '').split())}\n")
