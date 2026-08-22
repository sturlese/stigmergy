"""Corpus loading: a knowledge-repo checkout -> index-ready page rows. Pure code, no DB.

Walks the three included zones (`wiki/`, `sources/`, `views/`; `ops/`, `meta/` and `datasets/`
are deliberately absent), parses each page's frontmatter into the queryable columns of
`pages_index`, resolves the wikilink graph into per-page inlink counts, and computes the
`content_hash` the embedding cache is keyed by.

The frontmatter parser is deliberately TOLERANT — an unparseable page still indexes as body-only,
because a page that exists must be findable even when its metadata is broken.

**This layer knows no identity.** `acl` labels are parsed and stored here; access is decided above
by `stigmergy.server.acl.visible()` at `BrainService`'s read paths.
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from stigmergy.index import rank

log = logging.getLogger(__name__)

# The content zones, and the ONLY list that governs: a directory absent here is not indexed,
# which is why `ops/` (the registry, identities, templates) never reaches retrieval. An
# include-list needs no exclude-list.
ZONES = ("wiki", "sources", "views")

# Wikilink shape shared with the knowledge repo's linter (hand-mirrored, not imported: the
# linter lives with the content, this parser with the index — packages talk through files, never
# imports).
WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


@dataclass
class PageRow:
    """One page as the index stores it. Field names mirror the `pages_index` columns."""
    path: str                    # repo-relative path (the primary key)
    zone: str
    page_id: str                 # frontmatter `id`, falling back to the file stem
    title: str
    body: str
    type: str = ""
    status: str = ""
    entity: list[str] = field(default_factory=list)   # aboutness, plural
    owner: str = ""
    tier: int = 0
    as_of: str = ""
    updated: str = ""            # authored `updated`, or the machine page's extraction date
    superseded_by: str = ""
    supersedes: str = ""
    acl: list[str] | None = None   # None = no acl (open), [] = nobody. Stored here; ENFORCED
    #                                above, by `server.acl.visible()`.
    tags: str = ""
    mentions: str = ""
    entity_meta: str = ""        # `type: entity` pages' own `role`/`aliases`, tsv-only
    inlinks: int = 0
    generated_at: str = ""       # a view's own `generated_at` frontmatter (ISO-8601)
    content_hash: str = ""       # sha256 of the embedded text — the embedding-cache key
    # Outbound wikilink targets — two-stage like `inlinks`: `page_row` alone yields raw STEMS;
    # `load_pages` (whole corpus) and the incremental webhook (one query against existing paths)
    # overwrite with RESOLVED repo-relative paths via `resolve_links`.
    links: list[str] = field(default_factory=list)

    @property
    def embed_text(self) -> str:
        # contextual retrieval: title prepended, because the page IS the chunk
        return f"{self.title}\n{self.body}"


# CRLF (`\r?\n`), a BOM, and horizontal whitespace after either fence are all TOLERATED: each once
# cost a well-formed page its entire frontmatter — `acl:` included — and a frontmatter this cannot
# see indexes the page as body-only, silently.
_FRONTMATTER_RE = re.compile(r"^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$", re.S)

# The block starts at byte zero (after an optional BOM) and nothing else. Do NOT widen to `\s*---`
# (claims a leading thematic break as an unterminated block: an ordinary page indexes `acl=[]`,
# visible to NOBODY, and body above a later `---` vanishes) and do NOT accept a `...` closer (the
# lazy group stops at the FIRST `...`, dropping `acl:` into the body and indexing the page OPEN).
#
# `malformed` is not "the regex did not match" — it is the narrower question: did this page ASK
# for restriction in a block that cannot be read. Failing that closed trades a silent leak for a
# retrieval gap, an improvement only if it is LOUD (`page_row` logs it).
_OPENS_FRONTMATTER_RE = re.compile(r"^﻿?---[ \t]*\r?\n")

# Last-resort probe for a region that will not parse at all: a TOP-LEVEL `acl` key only — column
# zero or after `{`/`,` of a flow mapping, quoted spellings included. It must NOT match an
# INDENTED `acl:` nested under another key (`^\s*` once turned `meta:\n  acl: [x]` into a page
# visible to nobody).
_DECLARES_ACL_RE = re.compile(r"""(?m)(?:^|[,{][ \t]*)['"]?acl['"]?[ \t]*:""")


def _asked_for_an_audience(after_opener: str) -> bool:
    """Whether the unreadable region below an unclosed `---` declares an audience.

    The region is bounded the way YAML bounds a block (contiguous, first blank line ends it) and
    then PARSED: a top-level `acl` key is the request, anything nested under another key is not —
    a whole-file grep is too generous (an `acl:` inside a fenced example or prose makes the page
    invisible to everyone) and too narrow (quoted and flow spellings match nothing). The regex
    probe survives only for a region that will not parse at all.
    """
    block = after_opener.replace("\r\n", "\n").partition("\n\n")[0]
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return bool(_DECLARES_ACL_RE.search(block))
    return isinstance(parsed, dict) and "acl" in parsed


def split_frontmatter_checked(text: str) -> tuple[dict, str, bool]:
    """(frontmatter dict, body, malformed).

    `malformed` is True when the page MEANT to carry frontmatter and this could not read it — the
    signal that tells "carries no frontmatter" apart from "frontmatter unreadable", which the dict
    alone cannot express (both arrive as `{}`). A YAML syntax error is not the only unreadable
    shape: an unclosed block that declares an audience counts too. `malformed` must NOT become
    "the regex did not match" — see `_OPENS_FRONTMATTER_RE` for what that costs.
    """
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            return {}, text, True
        if fm is None:
            return {}, m.group(2), False      # an EMPTY block is well-formed, just empty
        if not isinstance(fm, dict):
            return {}, m.group(2), True
        return fm, m.group(2), False
    # No block extracted: only a page that opened one and declared an audience inside it is
    # unreadable rather than absent.
    opener = _OPENS_FRONTMATTER_RE.match(text)
    unreadable = bool(opener) and _asked_for_an_audience(text[opener.end():])
    return {}, text, unreadable


def split_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter dict, body); tolerant — an unparseable page indexes as body-only.

    Callers that make an ACCESS decision from the result must use `split_frontmatter_checked`
    instead: this signature cannot distinguish an absent block from an unreadable one.
    """
    fm, body, _malformed = split_frontmatter_checked(text)
    return fm, body


def entity_list(value) -> list[str]:
    """`entity:` normalized to a list of ids — fail CLOSED, like `_acl_labels`: a malformed
    element is DROPPED, never stringified into a label no human wrote. Bools are rejected (YAML
    1.1 truthy/falsy words, never ids), nested list/dict elements are dropped rather than
    repr'd, string elements are stripped (an unstripped id misses the membership filter and the
    rank boost). A bare top-level scalar (`entity: initech`) is a valid dialect read as a
    one-element list; `""` normalizes to `[]`, never `[""]`.
    """
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, list):
        out = []
        for v in value:
            if v is None or isinstance(v, bool):
                continue
            if isinstance(v, str | int | float):
                s = str(v).strip()
                if s:
                    out.append(s)
            # else: a nested list/dict — dropped, not stringified into its repr
        return out
    if isinstance(value, str | int | float):
        text = str(value).strip()
        return [text] if text else []
    return []


def _mentions_text(fm: dict) -> str:
    ms = fm.get("mentions")
    if not isinstance(ms, list):
        return ""
    return " ".join(str(m.get("name", "")) for m in ms if isinstance(m, dict))


def _entity_meta_text(fm: dict, page_type: str) -> str:
    """An entity page's own `role`/`aliases`, folded into the tsv source so metadata that lives
    in the frontmatter — where no body-text extractor looks — is lexically findable. Changes what
    MATCHES, never how a match is scored; only `type: entity` pages contribute."""
    if page_type != "entity":
        return ""
    role = str(fm.get("role", "") or "")
    aliases = fm.get("aliases")
    alias_text = " ".join(str(a) for a in aliases if a) if isinstance(aliases, list) else ""
    return " ".join(x for x in (role, alias_text) if x)


def _strip_code(text: str) -> str:
    """Blank fenced blocks and inline code so a literal `[[x]]` is not a link."""
    return _INLINE_CODE_RE.sub("", _FENCE_RE.sub("", text))


def link_targets(text: str) -> list[str]:
    targets = []
    for m in WIKILINK_RE.finditer(_strip_code(text)):
        t = m.group(1).split("|")[0].split("#")[0].strip()
        # Only a trailing `.md` comes off — NOT `Path(t).stem`, which amputates a dotted name:
        # `[[Booking.com]]` must key `booking.com`, not `booking`, or the link dies — or silently
        # resolves to a different page and the wrong edge lands in links/inlinks/backlinks.
        if t.lower().endswith(".md"):
            t = t[:-3]
        if t:
            targets.append(t.lower())
    return targets


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _acl_labels(fm: dict) -> list[str] | None:
    """None = no acl (open); [] = an EMPTY one (nobody) — a distinction the enforcement layer
    above depends on. FAIL CLOSED on malformed shapes: a non-empty scalar (`acl: sales`) is the
    one-label list it meant; anything else non-null and unrecognized becomes [] (visible to
    nobody) — a loud retrieval gap beats a silent leak."""
    if "acl" not in fm:
        return None
    value = fm.get("acl")
    if value is None:
        return None                    # explicit `acl: null` carries no restriction request
    if isinstance(value, list):
        return [s for s in (str(a).strip() for a in value) if s]
    if isinstance(value, str | int | float) and not isinstance(value, bool) and str(value).strip():
        return [str(value).strip()]
    return []


def page_row(rel_path: str, zone: str, text: str) -> PageRow:
    """One page's raw text -> its `PageRow`. THE parser — `load_pages` (full walk) and the
    incremental webhook both call this and only this, so there is provably one parser, not two
    that can drift. `inlinks` stays at 0: resolving it needs the whole corpus's wikilink graph,
    which a single changed file does not have — and `store._UPSERT_SET` excludes it so the
    default only ever reaches storage on a fresh INSERT, never over a rebuild's computed count."""
    fm, body, malformed = split_frontmatter_checked(text)
    if malformed:
        # THE operator signal that makes the fail-closed gap LOUD — nothing else says it anywhere.
        # Per page, at WARNING, naming the path: the fix is a one-line edit to that file.
        log.warning("%s: frontmatter could not be read and the page declares an audience — "
                    "indexing it visible to nobody rather than to everyone; fix the block and "
                    "the next rebuild restores it", rel_path)
    stem = Path(rel_path).stem
    tags = fm.get("tags")
    page_type = str(fm.get("type", "") or "")
    return PageRow(
        path=rel_path,
        zone=zone,
        page_id=str(fm.get("id") or stem),
        title=str(fm.get("title", "") or stem),
        body=body,
        type=page_type,
        status=str(fm.get("status", "") or ""),
        entity=entity_list(fm.get("entity")),
        owner=str(fm.get("owner", "") or ""),
        tier=int(fm.get("tier") or 0) if str(fm.get("tier") or "0").isdigit() else 0,
        as_of=str(fm.get("as_of", "") or ""),
        updated=str(fm.get("updated", "") or fm.get("extracted_at", "") or "")[:10],
        superseded_by=str(fm.get("superseded_by", "") or ""),
        supersedes=str(fm.get("supersedes", "") or ""),
        # Unreadable frontmatter is the one case `_acl_labels` cannot judge (`"acl" not in fm`
        # answers None — OPEN — for a page that may have asked to be restricted), so `malformed`
        # fails it closed to [] like a malformed shape: a loud retrieval gap beats a silent leak.
        acl=[] if malformed else _acl_labels(fm),
        tags=" ".join(str(t) for t in tags if t) if isinstance(tags, list) else "",
        mentions=_mentions_text(fm),
        entity_meta=_entity_meta_text(fm, page_type),
        generated_at=str(fm.get("generated_at", "") or ""),
        content_hash=content_hash(f"{fm.get('title', '') or stem}\n{body}"),
        links=link_targets(text),
    )


def by_stem_index(paths: list[str]) -> dict[str, list[str]]:
    """`stem -> [matching paths]` — the wikilink-resolution index both `load_pages` (in-memory
    walk) and the webhook (`store.existing_paths` snapshot) key off: one algorithm, two snapshots.

    `views/` paths are never link TARGETS: a view is derived — nobody authors or wikilinks it —
    and its filename is the entity ID, which collides case-insensitively with the Title-Case
    entity page's stem. Excluding the zone keeps `[[Entity]]` resolving to exactly the entity
    page; the knowledge repo's linter states the same rule at its end."""
    index: dict[str, list[str]] = {}
    for path in paths:
        if path.split("/", 1)[0] == "views":
            continue
        index.setdefault(Path(path).stem.lower(), []).append(path)
    return index


def is_chain_primary(page_id: str) -> bool:
    """True iff `page_id` carries no trailing continuation marker — the only shape the
    `superseded_by` propagation (build-time here, incremental in `server.webhook`) treats as a
    DONOR."""
    return page_id == rank.chain_base(page_id)


def chain_part_pattern(base: str) -> re.Pattern:
    """The exact `^{base}(#|-)p<n>$` pattern a row's `page_id` must match to RECEIVE `base`'s
    propagated `superseded_by` — shared by the build-time and the webhook's incremental
    propagation, so "what counts as a sibling" is decided in exactly one place."""
    return re.compile(rf"^{re.escape(base)}(?:#p|-p)\d+$")


def resolve_links(own_path: str, stems: list[str], by_stem: dict[str, list[str]]) -> list[str]:
    """Outbound wikilink stems -> resolved repo-relative paths, via `by_stem_index`. An ambiguous
    stem stores every match (the same semantics `inlinks` counts); a dead link stores nothing (the
    linter's finding, not the index's); `own_path` is excluded from its own result. THE one
    resolution step both `load_pages` and `server.webhook.process_push` call — a parity test
    guards the sharing."""
    resolved: dict[str, None] = {}
    for stem in dict.fromkeys(stems):
        for path in by_stem.get(stem, ()):
            if path != own_path:
                resolved[path] = None
    return sorted(resolved)


def is_indexable_page(rel_path: str) -> bool:
    """Whether a repo-relative path inside a zone becomes a `pages_index` row — THE predicate for
    both walkers (`load_pages` and `server.webhook.in_zone_changes`): a path one admits and the
    other does not is a row that flickers between rebuilds, or one a deletion never reaches.

    NOT excluded, deliberately: a `.md` page inside a dot-directory (only the file's own name is
    checked, so `wiki/.obsidian/note.md` is a page and `wiki/.hidden.md` is not) — changing that
    would silently drop rows the corpus has. What matters is both walkers answer identically.
    """
    return rel_path.endswith(".md") and not rel_path.rsplit("/", 1)[-1].startswith(".")


def load_pages(repo_dir: str) -> list[PageRow]:
    """Every page of the included zones, inlinks/links resolved, sorted by path (determinism)."""
    root = Path(repo_dir)
    rows: list[PageRow] = []
    for zone in ZONES:
        zone_dir = root / zone
        if not zone_dir.is_dir():
            continue
        for path in sorted(zone_dir.rglob("*.md")):
            rel = str(path.relative_to(root))
            if not is_indexable_page(rel):
                continue
            rows.append(page_row(rel, zone, path.read_text(encoding="utf-8", errors="replace")))
    rows.sort(key=lambda r: r.path)

    # wikilink graph: one resolution feeds BOTH directions — outbound `links` (resolved paths,
    # kept on the row) and inbound `inlinks` (a bare count, not a ranking factor).
    by_stem = by_stem_index([r.path for r in rows])
    inbound: dict[str, set[str]] = {}
    for r in rows:
        r.links = resolve_links(r.path, r.links, by_stem)
        for target in r.links:
            inbound.setdefault(target, set()).add(r.path)
    for r in rows:
        r.inlinks = len(inbound.get(r.path, ()))

    # A split document's continuation parts carry an EMPTY `superseded_by`; the field is stamped
    # on the PRIMARY page only. Propagating the primary's value onto every chain sibling HERE, at
    # build time, makes the column true on every stored row — `contract_factors` then needs no
    # rank-time reconstruction. The propagation is marker-gated AND directional: a row RECEIVES
    # only via a genuine continuation marker (`chain_part_pattern`) and the donor is exactly the
    # row whose `page_id` equals the base (`is_chain_primary`) — never "whichever came first",
    # which would let two ID-less same-stem pages copy values between themselves. The chain key
    # carries the DIRECTORY beside the base, like rank's collapse key: `report-p2.md` is a
    # plausible human filename, and two such pages in different folders must never exchange
    # supersession; a real chain's parts sit beside their primary, so the gate never splits one.
    by_chain: dict[tuple[str, str], list[PageRow]] = {}
    for r in rows:
        key = (str(Path(r.path).parent.as_posix()), rank.chain_base(r.page_id))
        by_chain.setdefault(key, []).append(r)
    for (_directory, base), chain in by_chain.items():
        donors = [r for r in chain if r.page_id == base and r.superseded_by]
        if not donors:
            continue
        donor_values = {d.superseded_by for d in donors}
        if len(donor_values) > 1:
            log.warning(
                "supersession propagation: conflicting superseded_by donor values for chain "
                "base %r: %s — using the path-sorted first (%s)",
                base, sorted(donor_values), donors[0].path)
        value = donors[0].superseded_by
        pattern = chain_part_pattern(base)
        for r in chain:
            if pattern.fullmatch(r.page_id):
                r.superseded_by = value
    return rows
