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
# which is why `ops/` (the registry, identities, templates) never reaches retrieval. There used
# to be an `EXCLUDED_ZONES` beside this naming `ops`, `meta` and `datasets` — it had ZERO readers
# anywhere in the codebase, named two directories that no longer exist, and its own comment
# claimed it was "asserted by the zone tests" when nothing asserted it. An include-list needs no
# exclude-list.
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
    # Outbound wikilink targets. `page_row` alone (a single-file parse) can only produce
    # the raw STEMS `link_targets` finds in the text; `load_pages` (whole-corpus) and the
    # incremental webhook (`server.webhook`, one query against `pages_index`'s existing paths)
    # both overwrite this with RESOLVED repo-relative paths afterward (`resolve_links`) — the same
    # two-stage shape `inlinks` already has (0 from `page_row` alone, a real count from
    # `load_pages`), now shared by a second field.
    links: list[str] = field(default_factory=list)

    @property
    def embed_text(self) -> str:
        # contextual retrieval: the title is prepended to the chunk, because the page IS the
        # chunk — the same shape that was benchmarked.
        return f"{self.title}\n{self.body}"


# `\r?\n` throughout: a CRLF checkout is not a malformed page. Anchoring on `\n` alone meant a
# page written on Windows (or normalized by a `.gitattributes` rule) matched NOTHING here, so its
# whole frontmatter — `acl:` included — was silently invisible and it indexed as body-only. That is
# the same silent leak as an unparseable block below, applied to an entire checkout at once.
# A BOM and leading blank lines are TOLERATED, and `...` is accepted as the closer YAML itself
# allows: an editor that writes a BOM, or an author who left a blank first line, wrote a page whose
# frontmatter is perfectly readable, and refusing to see it is how `acl:` went missing.
_FRONTMATTER_RE = re.compile(r"^﻿?\s*---\r?\n(.*?)\r?\n(?:---|\.\.\.)\r?\n?(.*)$", re.S)

# "This page MEANT to carry frontmatter" — the question `malformed` really turns on. Anchoring it
# on `text.startswith("---")` alone was too narrow in one direction and too generous in the other.
_INTENDED_FRONTMATTER_RE = re.compile(r"^﻿?\s*---\r?\n")


def split_frontmatter_checked(text: str) -> tuple[dict, str, bool]:
    """(frontmatter dict, body, malformed).

    `malformed` is True when the page MEANT to carry frontmatter and this could not read it. It is
    the signal `page_row` needs to tell "this page carries no frontmatter" apart from "this page's
    frontmatter could not be read", which the dict alone cannot express: both arrive as `{}`.

    **Every unreadable shape sets it, not just a YAML syntax error.** That distinction was drawn too
    narrowly once already: the fail-closed ACL was reached only when the block regex MATCHED and
    `yaml.safe_load` then raised, so four other shapes still indexed a page carrying
    `acl: [finance]` as open to everyone — an unterminated block, a block closed with `...`, a
    leading blank line, and a BOM. The first two are genuinely unreadable and now fail closed; the
    last two are readable and now simply parse, which is the better answer for both.
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
    # No block extracted. If the page opened one anyway, it is unreadable — not absent.
    return {}, text, bool(_INTENDED_FRONTMATTER_RE.match(text))


def split_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter dict, body); tolerant — an unparseable page indexes as body-only.

    Callers that make an ACCESS decision from the result must use `split_frontmatter_checked`
    instead: this signature cannot distinguish an absent block from an unreadable one.
    """
    fm, body, _malformed = split_frontmatter_checked(text)
    return fm, body


def entity_list(value) -> list[str]:
    """`entity:` normalized to a list of ids — matching `_acl_labels`'s own fail-CLOSED doctrine,
    not a fail-open normalizer that stringifies whatever YAML happened to produce.

    A naive normalizer gets four things wrong, and all four have real consequences. Keeping list
    elements UNSTRIPPED (`entity: ["initech "]`) stores the trailing space, so the membership
    filter and the rank boost both miss it, so the page loses the promotion that would
    have put it first for a query naming its own entity. Stringifying `None` (a YAML block
    list with an empty dash item) produces the
    literal text `"None"`. A YAML 1.1 boolean becomes a plausible-looking id (`entity: no` ->
    `["False"]`), which is a SCORING change for any query containing the token "false". A nested
    list/dict element becomes its Python `repr`. None of those are real entity declarations, and
    every one of them reaches the boost carrying a label no human ever wrote.

    So: bools are rejected explicitly (a bool is a YAML 1.1 truthy/falsy word, never a plausible
    entity id), any element that is not a string/int/float is DROPPED rather than stringified (a
    nested list/dict has no `str()` that is a real id), and every string element is stripped. A
    bare STRING at the top level (`entity: initech`) is a valid dialect and is read as a
    one-element list; `0` (an int, not a bool) is a legal, if odd, id spelling, exactly as
    `_acl_labels` treats it. `""` normalizes to `[]`, never to `[""]`: an empty declaration is an
    empty list of entities, not a list holding one empty one.
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
            # else: a nested list/dict — not a plausible entity id, dropped rather than
            # stringified into its repr.
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
    """An entity page's own `role`/`aliases` frontmatter, folded into the tsv source text so
    steward-authored metadata is lexically findable. This changes what MATCHES, never how a match
    is scored — no ranking factor is added or altered. Only `type: entity` pages contribute
    anything here; the fields carry no meaning on any other page type."""
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
        # Only a trailing `.md` comes off — NOT `Path(t).stem`, which strips the last dotted
        # suffix of whatever it is given. On a FILE PATH that suffix is the extension, which is
        # what `by_stem_index` wants; on LINK TEXT, which carries no extension by convention, it
        # amputates part of the name: `[[Booking.com]]` became the key `booking` while the page
        # indexed under `booking.com`. The link then died — or, with a `Booking.md` in the corpus,
        # silently resolved to that OTHER page, and the wrong edge landed in `links`, in `inlinks`
        # and in the backlinks `read_page` serves.
        if t.lower().endswith(".md"):
            t = t[:-3]
        if t:
            targets.append(t.lower())
    return targets


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _acl_labels(fm: dict) -> list[str] | None:
    """None = the page carries no acl (open); [] = it carries an EMPTY one (nobody) — a
    distinction the enforcement layer above depends on.

    FAIL CLOSED on malformed shapes: a page that ASKED for restriction must never index as
    open because its author mistyped the YAML. A non-empty scalar (`acl: sales`) is read as
    the one-label list it obviously meant; anything else non-null and unrecognized becomes
    [] (visible to nobody) — a loud retrieval gap beats a silent leak."""
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
    """One page's raw text -> its `PageRow`. THE parser — `load_pages` (a full directory walk)
    and the incremental webhook (one changed file at a time) both call this and only this, so
    there is provably one parser rather than two that can drift into an incremental rebuild and a
    full rebuild disagreeing. `inlinks` is left at its default (0): resolving it needs the WHOLE
    corpus's wikilink graph, which is exactly what a single changed file does not have — the
    nightly full rebuild is the reconciler for this field.

    **That default only ever reaches storage on a fresh INSERT.**
    `store.upsert_pages`'s `_UPSERT_SET` excludes `inlinks` from its `ON CONFLICT DO UPDATE SET`
    list (same as `path`, the conflict key) precisely so that an UPDATE (an edited page the
    webhook already knew about) leaves the last full rebuild's computed count alone instead of
    resetting it to this function's honest-but-uninformed 0 — a retrieval regression `search.py`'s
    ranking would otherwise take on every incrementally-edited page until the next nightly
    rebuild. A genuinely NEW page (no existing row to conflict with) still lands at 0 here, which
    is the correct answer for a page nothing has ever resolved the graph for yet."""
    fm, body, malformed = split_frontmatter_checked(text)
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
        # A page whose frontmatter could not be READ is the one case `_acl_labels` cannot judge:
        # every key is gone, so `"acl" not in fm` is true and it answers `None` — the OPEN value —
        # for a page that may well have asked to be restricted. That is precisely the leak its own
        # docstring refuses ("a page that ASKED for restriction must never index as open because
        # its author mistyped the YAML"), reached by the one route that skips the shape checks: a
        # syntax error rather than a wrong shape. `[]` (visible to nobody) is the same fail-closed
        # answer a malformed SHAPE already gets — a loud retrieval gap beats a silent leak.
        acl=[] if malformed else _acl_labels(fm),
        tags=" ".join(str(t) for t in tags if t) if isinstance(tags, list) else "",
        mentions=_mentions_text(fm),
        entity_meta=_entity_meta_text(fm, page_type),
        generated_at=str(fm.get("generated_at", "") or ""),
        content_hash=content_hash(f"{fm.get('title', '') or stem}\n{body}"),
        links=link_targets(text),
    )


def by_stem_index(paths: list[str]) -> dict[str, list[str]]:
    """`stem -> [matching paths]` — the wikilink-resolution index both `load_pages` (built from
    its own in-memory walk) and the incremental webhook (built from `store.existing_paths`'s
    one-query snapshot of the indexed table) key off. Centralizing the stem computation here is
    the shared half of `resolve_links`'s one algorithm over two snapshots. This module stays
    pure/DB-less itself (module docstring) — `store.existing_paths` is where the query lives.

    `views/` paths are never link TARGETS: a view is derived — nobody authors it, nobody
    wikilinks it, `describe_entity` serves it by path — and its filename is the entity ID, which
    for any single-word entity collides case-insensitively with the Title-Case entity page's stem
    (views/vantage.md vs wiki/entities/Vantage.md, the first real regeneration). Excluding the zone
    here keeps `[[Entity]]` resolving to exactly the entity page; the knowledge repo's linter
    states the same rule at its end (the duplicate-basename and orphan checks skip views/)."""
    index: dict[str, list[str]] = {}
    for path in paths:
        if path.split("/", 1)[0] == "views":
            continue
        index.setdefault(Path(path).stem.lower(), []).append(path)
    return index


def is_chain_primary(page_id: str) -> bool:
    """True iff `page_id` carries no trailing continuation marker (`#p<n>` historical, `-p<n>`
    live — see `rank._PART_MARKER_RE`) — the only shape the `superseded_by` propagation
    (build-time here, incremental in `server.webhook`) will ever treat as a DONOR. A `page_id`
    that already IS its own `chain_base` is, by definition, not itself a continuation part."""
    return page_id == rank.chain_base(page_id)


def chain_part_pattern(base: str) -> re.Pattern:
    """The exact `^{base}(#|-)p<n>$` continuation-marker pattern a row's `page_id` must match to
    RECEIVE `base`'s propagated `superseded_by` (both the historical `#p<n>` and the live `-p<n>`
    convention the meeting flow's splitter writes) — shared by `load_pages`'s
    build-time propagation and `server.webhook`'s incremental one, so "what counts as a sibling"
    is decided in exactly one place rather than two regexes that could drift."""
    return re.compile(rf"^{re.escape(base)}(?:#p|-p)\d+$")


def resolve_links(own_path: str, stems: list[str], by_stem: dict[str, list[str]]) -> list[str]:
    """Outbound wikilink stems (`link_targets`'s output) -> resolved repo-relative paths, via a
    `stem -> [paths]` index (`by_stem_index`). A stem resolving to several pages stores every
    match (ambiguous stems get full credit — the same semantics `inlinks` already counts); a stem
    resolving to no page stores nothing (a dead link is the linter's finding, not the index's).
    `own_path` is excluded from its own result (a page is never its own outbound neighbour,
    mirroring the inbound side's pre-existing self-exclusion below).

    THE one resolution step both `load_pages` (whole-corpus, in memory) and
    `server.webhook.process_push` (one file at a time, one query against `pages_index`'s existing
    paths) call. Two resolution code paths cannot drift if there is only one; a parity test guards
    the sharing rather than the drift."""
    resolved: dict[str, None] = {}
    for stem in dict.fromkeys(stems):
        for path in by_stem.get(stem, ()):
            if path != own_path:
                resolved[path] = None
    return sorted(resolved)


def load_pages(repo_dir: str) -> list[PageRow]:
    """Every page of the included zones, inlinks/links resolved, sorted by path (determinism)."""
    root = Path(repo_dir)
    rows: list[PageRow] = []
    for zone in ZONES:
        zone_dir = root / zone
        if not zone_dir.is_dir():
            continue
        for path in sorted(zone_dir.rglob("*.md")):
            if path.name.startswith("."):
                continue
            rel = str(path.relative_to(root))
            rows.append(page_row(rel, zone, path.read_text(encoding="utf-8", errors="replace")))
    rows.sort(key=lambda r: r.path)

    # wikilink graph: one resolution (`resolve_links`, keyed off `by_stem_index`) feeds BOTH
    # directions — outbound `links` (resolved repo-relative paths, kept on the row) and inbound
    # `inlinks` (a bare count, not currently a ranking factor).
    by_stem = by_stem_index([r.path for r in rows])
    inbound: dict[str, set[str]] = {}
    for r in rows:
        r.links = resolve_links(r.path, r.links, by_stem)
        for target in r.links:
            inbound.setdefault(target, set()).add(r.path)
    for r in rows:
        r.inlinks = len(inbound.get(r.path, ()))

    # A split document's continuation parts ("<id>#p2", "#p3", …) carry an EMPTY `superseded_by`
    # in their own frontmatter — `versions.py` stamps the field on the PRIMARY page only.
    # Propagate the primary's value onto every sibling sharing the same `rank.chain_base`, here,
    # at build time, so the field is TRUE on every row that reaches storage. That removes the need
    # for any rank-time reconstruction: `contract_factors` already turns a truthy `superseded_by`
    # into the "superseded" penalty+label for any row, part or primary, once the column itself
    # carries the right value.
    #
    # Grouping by `chain_base(page_id)` ALONE is not enough to decide who may give and who may
    # receive. Two ID-LESS pages sharing a file STEM in different directories (`page_id` falls
    # back to the stem) both reduce to the SAME base with no `#p<n>` marker at all, and a "first
    # non-empty value found in the group" rule copies one's `superseded_by` onto the other even
    # though neither is a real continuation part. So the propagation is marker-gated AND
    # directional: a row may only RECEIVE via a genuine `^{base}#p\d+$` continuation marker
    # (`chain_part_pattern`), and the donor is exactly the row whose `page_id` equals the base
    # (`is_chain_primary`) — never "whichever came first".
    #
    # The chain key carries the page's DIRECTORY beside the base, exactly like rank's collapse
    # key: two ID-LESS `-p<n>`-stemmed pages in different folders must never exchange
    # supersession. That matters specifically because the live `-p` convention is matched, and
    # while `#p` could essentially never appear in a human filename stem, `report-p2.md` plausibly
    # can. A real chain's parts sit beside their primary, so the gate never splits one.
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
