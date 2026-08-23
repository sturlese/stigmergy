#!/usr/bin/env python3
"""Deterministic contract linter for the `stigmergy` knowledge repository.

Enforces the page contract and the two-zone repository layout — the same
contract `docs/reference/page-contract.md` states in prose for readers, stated
here as the check a commit has to survive.

Scans the content zones (wiki/, sources/) and reports contract
violations:

  frontmatter  missing block / required field, invalid enum (type, status,
               tier, source_kind), non-list list fields, invalid calendar dates
  zones        folder<->type mismatch; authored wiki/ pages carrying
               machine-only provenance fields; sources/ pages missing provenance
  schema       per-type required fields (templates in ops/templates/ are the
               human-facing source of truth; this dict enforces them)
  size         body outside 30-150 lines (the page is the retrieval chunk, D7):
               oversize is an error, undersize a warning (machine pages with
               oversize is an error, undersize a warning)
  links        dead wikilinks, duplicate basenames, alias collisions
  hygiene      orphan pages, empty sections (warnings)

Usage: stigmergy_lint.py [--repo PATH] [--json] [--strict]
Exit codes:
  0  scan completed, no errors (warnings allowed)
  1  --strict and at least one error-severity finding (CI gate)
  2  fatal error (repo layout missing)
Stdlib only, no third-party dependencies (mirrors the zero-deps content repo).
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# --- the page contract ------------------------------------------------------

# R-2 (P2 open, 2026-08-01): SEVEN types, one per writer. An earlier design also split pages into
# PERSONAL and COMPANY; that axis went with it, because it only earns its keep in a single-user
# vault and every page here belongs to a team. Three are the fast lane's genre choice, the rest each have
# exactly one stamper: `entity` the governed door, `meeting` the distiller, `source` provenance
# (the transcript today, P4's Drive door next).
#
# CUT at R-2 and why, so nobody re-adds one by pattern-matching: `person`/`team`/`product`/
# `customer` are ENTITY KINDS (the registry carries `type` per entity — one spine, not two
# taxonomies); `project` is an entity here, which it cannot be without a registry to carry it; `meta`
# because index/log/schema are Postgres, git history and CLAUDE.md; `dataset`/`metric` died with
# the facts store at P1; `playbook`/`postmortem`/`policy` appear in no reference and no code.
# Four types a page may BE. A conclusion is a `note`:
# splitting conclusions into two folders by their grammatical mood bought nothing. A meeting
# is an EVENT, so its transcript is a `source` and what it established is a `note`.
VALID_TYPES = {"note", "concept", "entity", "source"}

# `canonical` died with the canon lane (R-1/P1): status is a maturity axis, never a court.
# `evergreen` is its successor as the top of the axis — it says "kept current", not "approved".
VALID_STATUS = {"seed", "developing", "mature", "evergreen"}
VALID_SOURCE_KIND = {"google-drive", "slack", "meeting", "github", "upload", "distilled"}
VALID_TIER = {"1", "2", "3"}

REQUIRED_FIELDS = ["type", "title", "created", "updated", "tags", "status"]
# Machine-owned pages (sources/) are validated against their
# provenance/trust field group, not the authored-page conventions:
# no created/updated/status —
# they carry `extracted_at` and the D8 lifecycle (supersedes) instead. Some of
# that machine vocabulary is still the pipeline's own dialect awaiting M1b's
# mapping to the contract enum (kit SI-02, resolution A) — `sources/`'s
# `type: contract` and friends.
MACHINE_REQUIRED_FIELDS = ["type", "title", "tags"]
RECOMMENDED_FIELDS = ["related", "sources"]
LIST_FIELDS = ("tags", "related", "sources", "aliases", "acl")

# Per-type required extras beyond the universal set. Templates in
# ops/templates/ are the source of truth for structure; this mirrors their
# frontmatter so the linter enforces what the templates promise.
TYPE_REQUIRED = {
    "source": ["source_kind", "content_hash"],
}

# Content zones and the page types each accepts. `sources/` is machine-owned
# and provenance-checked rather than type-locked.
#
# Every wiki/ zone an author can actually write to needs its own entry here.
# The generic "invalid type" check only refuses a type outside VALID_TYPES, so
# an unmapped zone would accept any valid type — a note filed under
# wiki/concepts would pass with zero findings.
ZONE_TYPES = {
    "wiki/entities": {"entity"},
    "wiki/concepts": {"concept"},
    "wiki/notes": {"note"},
}
CONTENT_ROOTS = ("wiki", "sources")

# Fields that make a page an sources/source page (provenance required).
PROVENANCE_REQUIRED = ["content_hash", "tier"]

# M8a findings cycle 1, group 3: machine provenance the librarian stamps onto `sources/` pages.
# A SHAPE rule, not an authorship one: `extracted_at` has no legitimate meaning on a `wiki/**`
# page whoever wrote it, so it needs no zone-ownership exemption and is never keyed on
# `submitted_by` (criterion 13). `content_hash` is deliberately NOT here — the knowledge repo's
# CI author check polices it over commit history, which a stateless scan of one working tree
# cannot do. The ONE machine-stamped field an authored page must never claim; it was four until
# P2 (`extraction_method`, `blob`, `source_uri` were a retired pipeline's vocabulary, no writer left —
# a rule listing ghosts is a rule that teaches the ghosts' names).
MACHINE_ONLY_FIELDS = ("extracted_at",)

SIZE_MIN, SIZE_MAX = 30, 150

# --- the derived view: entity pages <-> ops/entity-registry.json (M6c §4.4) -
# `ops/entity-registry.json` is DERIVED from wiki/entities/*.md (SI-06, DB-20b):
# the pages are the source of truth and the registry is generated from them by
# the librarian worker, in the same commit that introduces or merges an entity.
# Nothing enforced that until now, which made "derived view" a claim rather than a
# property — a hand-edited registry entry, or a page edited without regenerating,
# left the graph resolving names nobody registered (or failing to resolve names
# somebody did).
#
# This is the KNOWLEDGE repo's own gate on that contract: CI here goes red on drift,
# so a page and the registry cannot disagree on `main`. The platform has the mirror
# check (`stigmergy-index --check`) for a local clone.
ENTITIES_DIR = ("wiki", "entities")
REGISTRY_RELPATH = "ops/entity-registry.json"
# `ops/entity-registry.json` is DERIVED from the entity pages and written by the librarian worker
# alone (ADR 044): a DRIFT between pages and registry is reported as `warn`, not `error`, because
# nothing in the repo is broken — but it does not heal itself either, since the worker refuses to
# write an identity while the two sides disagree. A page the worker cannot regenerate over at all
# (no title, two titles with one id) stays `error`: only a person fixes it.
REGISTRY_FIX = ("an operator puts the pages and the registry back in step in the knowledge repo, "
                "and the librarian refuses to write an identity until they do")
DEFAULT_ENTITY_TYPE = "organization"


def registry_id(title):
    """The registry id a page titled `title` generates as: the slug of the title.

    **Deliberately duplicated, not shared.** The platform's
    `stigmergy.kernel.normalize.slugify` is the other half of this contract, and
    this repo is stdlib-only with zero dependencies on purpose (the content repo does
    not import the code repo). So the function is mirrored BY HAND, and named here so
    the mirroring is visible at both ends rather than discovered when they drift —
    the same posture the platform's own `numbers.py` duplication takes across its
    ingest/serving boundary. If one changes, this comment is the pointer to the other.
    """
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60] or "x"


# Legal suffixes stripped for canonicalization (longest/compound first) — the exact list
# `stigmergy.kernel.normalize._SUFFIXES` uses, mirrored by hand for the same reason
# `registry_id` above mirrors `slugify`: this repo is stdlib-only and does not import the code
# repo. If one changes, this comment is the pointer to the other.
_MATCH_SUFFIXES = [
    "s.a.p.i. de c.v.", "s. de r.l. de c.v.", "s.l.u.", "s.a.u.", "s.c.r.", "s.l.", "s.a.",
    "sociedad limitada", "sociedad anonima", "inc", "ltd", "llc",
    "gmbh", "b.v.", "s.r.l.", "limited", "corp", "co", "sl", "sa",
]
_MATCH_SUFFIXES_N = [re.sub(r"\s+", " ", re.sub(r"[.,]", " ", s)).strip()
                     for s in _MATCH_SUFFIXES]


def normalize_name(name):
    """The entity-MATCHING key `stigmergy.kernel.normalize.normalize` computes: no accents,
    lowercase, no punctuation or legal suffixes. Strictly more folding than `registry_id`'s slug —
    `normalize_name("Acme Corp.") == normalize_name("Acme")`, which is the exact collapse
    `_duplicate_match_keys` (M8a, criterion 11) and `check_entity_field`'s alias/name resolution
    both depend on."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.,()\"'/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    changed = True
    while changed:
        changed = False
        for suf in _MATCH_SUFFIXES_N:
            if s.endswith(" " + suf):
                s = s[: -len(suf)].strip()
                changed = True
    return s.strip()


def _entity_pages(root, pages, texts, frontmatter, add):
    """Entity pages exactly as the generator scans them: direct children of
    wiki/entities/, every type, non-recursive. Matching that scan is the whole
    point — a linter that judged a different set than the generator produces would
    report drift the fix command cannot fix.

    **And it reports the two pages the generator refuses to READ, rather than
    quietly agreeing with itself about them.** `generator.read_entity_pages` raises
    on a page with no title and on two pages whose titles slug to one id; this
    function used to `continue` past the first ("the missing-required-field rule
    already reported this") and silently last-wins on the second. Both leave CI
    green on a repo where the worker cannot regenerate the registry at all — the
    linter is the knowledge repo's own gate on the derived view, so a state the
    generator cannot survive has to be red HERE, not discovered by the reader whose
    approval it blocks.

    The first delegation was not even reliable: the frontmatter rule reports a
    `title` that is absent or empty, and a whitespace-only one satisfies it while
    still naming no entity. A rule that depends on another rule's edge cases is a
    rule with holes.
    """
    out = {}
    seen_match_keys = {}   # normalize_name(title) -> {"path", "name", "id"} — M8a criterion 11
    for p in pages:
        rel = p.relative_to(root).parts
        if len(rel) != 3 or rel[:2] != ENTITIES_DIR:
            continue
        fm = frontmatter.get(p) or {}
        title = str(fm.get("title") or fm.get("name") or "").strip()
        if not title:
            add("error", "registry", p,
                f"an entity page with no title names no entity, and the worker cannot "
                f"regenerate the registry at all while it is in "
                f"{'/'.join(ENTITIES_DIR)}/ — give it a title (the page contract "
                f"requires one anyway) or move it out of the entity folder")
            continue
        canonical_id = registry_id(title)
        first = out.get(canonical_id)
        if first is not None:
            add("error", "registry", p,
                f"{title!r} and {first['name']!r} "
                f"({first['path'].relative_to(root)}) both produce the registry id "
                f"{canonical_id!r}, so one would overwrite the other and the worker "
                f"cannot regenerate the registry while both exist — ids are the "
                f"slug of the page title, so two titles differing only by "
                f"punctuation or case collide. Rename one, or make it an alias of "
                f"the other and delete its page")
            continue

        # M8a (spec §4.4, criterion 11): mirrors `entities.generator._duplicate_match_keys` — two
        # pages whose TITLES fold to one entity-MATCHING key even though their ids (slugs) differ,
        # which the worker refuses to regenerate the registry over. A literal mirror of the
        # generator's own wording, not a paraphrase, so the two checks agree about what the fix is.
        match_key = normalize_name(title)
        earlier = seen_match_keys.get(match_key) if match_key else None
        if earlier is not None:
            add("error", "registry", p,
                f'"{earlier["name"]}" ({earlier["path"].relative_to(root)}) and "{title}" '
                f'({p.relative_to(root)}) resolve to the same entity-matching key '
                f'("{match_key}") even though their registry ids differ '
                f'("{earlier["id"]}" vs "{canonical_id}") — every mention of that name would '
                f'silently anchor to whichever page the registry happens to index last. Rename '
                f'one, or make it an alias of the other and delete its page — {REGISTRY_FIX}')
        elif match_key:
            seen_match_keys[match_key] = {"path": p, "name": title, "id": canonical_id}

        aliases = fm.get("aliases")
        if not isinstance(aliases, list):
            aliases = [aliases] if aliases else []
        alias_set = {str(a).strip() for a in aliases if str(a).strip()}

        # The identity's lifecycle has ONE state (ADR 044): `approved_by` names the person whose
        # capture introduced the entity — born confirmed, never waiting on anybody. ABSENT is a
        # page from before the field existed, which reads the same; EMPTY is the old "proposed"
        # mark, and nothing may write it any more. `proposed_aliases` is not a field: a spelling
        # the material uses is an alias.
        approved_by = fm.get("approved_by")
        if approved_by is not None and not isinstance(approved_by, str):
            add("error", "lifecycle", p,
                f"approved_by must be a string (who introduced this identity), got: "
                f"{approved_by!r}")
            approved_by = str(approved_by)
        if approved_by is not None and not approved_by.strip():
            add("error", "lifecycle", p,
                "approved_by is empty: an identity is born confirmed by the person whose capture "
                "introduced it, and nothing waits on a steward any more (ADR 044) — name them, or "
                "drop the field on a page that predates it")
        if "proposed_aliases" in fm:
            add("error", "lifecycle", p,
                "proposed_aliases is not a field any more (ADR 044): a spelling the material uses "
                "is one of the entity's aliases — move it to `aliases`")

        out[canonical_id] = {
            "path": p, "name": title,
            "type": str(fm.get("entity_type") or DEFAULT_ENTITY_TYPE).strip()
                    or DEFAULT_ENTITY_TYPE,
            "aliases": sorted(alias_set),
        }
    return out


def _fmt(values):
    return "{" + ", ".join(values) + "}" if values else "{}"


def check_registry(root, pages, texts, frontmatter, add):
    """Every way an entity page and the registry disagree. Each message names the fix.

    Silent when there is no registry file at all: a repo that has never registered an
    entity is not drifting, and a linter that demanded the file exist would fail every
    fresh clone of a repo layout this rule is not responsible for creating.

    **Except for the pages the generator cannot read.** `_entity_pages` runs FIRST and
    above that early return, because "the worker cannot regenerate the registry" is true
    whether or not a registry file exists yet — a title-less page or a slug collision blocks
    the regeneration that would create one. Comparing pages against a registry is the part
    that needs the file; reading the pages at all is not.
    """
    declared = _entity_pages(root, pages, texts, frontmatter, add)
    registry_file = root / REGISTRY_RELPATH
    if not registry_file.is_file():
        return
    try:
        data = json.loads(registry_file.read_text(encoding="utf-8"))
        registered = data["entities"]
        if not isinstance(registered, dict):
            raise ValueError("top-level 'entities' must be an object")
    except (ValueError, KeyError, OSError) as ex:
        add("warn", "registry", REGISTRY_RELPATH,
            f"could not be read as a registry ({ex}) — it is what every entity anchor "
            f"resolves against, so a broken one silently stops resolving names; "
            f"{REGISTRY_FIX}")
        return

    for canonical_id in sorted(set(declared) | set(registered)):
        mine, theirs = declared.get(canonical_id), registered.get(canonical_id)
        if theirs is None:
            add("warn", "registry", mine["path"],
                f"{mine['name']!r} is an entity page that {REGISTRY_RELPATH} does not "
                f"register at all — {REGISTRY_FIX}")
            continue
        if mine is None:
            add("warn", "registry", REGISTRY_RELPATH,
                f"{REGISTRY_RELPATH} registers {canonical_id!r} "
                f"({theirs.get('name')!r}) but no page in {'/'.join(ENTITIES_DIR)}/ "
                f"declares it — {REGISTRY_FIX}")
            continue
        if mine["name"] != theirs.get("name"):
            add("warn", "registry", mine["path"],
                f"page title is {mine['name']!r} but {REGISTRY_RELPATH} registers "
                f"{canonical_id!r} as {theirs.get('name')!r} — {REGISTRY_FIX}")
        if mine["type"] != str(theirs.get("type") or DEFAULT_ENTITY_TYPE):
            add("warn", "registry", mine["path"],
                f"{mine['name']!r} declares entity_type {mine['type']!r} but "
                f"{REGISTRY_RELPATH} has type {theirs.get('type')!r} — "
                f"{REGISTRY_FIX}")
        listed = sorted({str(a) for a in (theirs.get("aliases") or [])})
        if mine["aliases"] != listed:
            add("warn", "registry", mine["path"],
                f"{mine['name']!r} declares aliases {_fmt(mine['aliases'])} but "
                f"{REGISTRY_RELPATH} has {_fmt(listed)} — {REGISTRY_FIX}")


# --- entity: aboutness, validated against the registry (M8a spec §4.1/§4.4) -------------------
# How many registered ids the unresolvable-id message lists in full before it switches to naming
# only the count — the same bounded-listing discipline the platform's own
# `gates.MAX_BRIEF_REGISTRY_NAMES`/`report.MAX_QUESTION_CANDIDATES` already apply (M8a UX §3.1,
# adopted per the ruled §3.1 question): show the ids in full below the bound, name the count
# above it, never truncate silently.
MAX_ENTITY_LISTING = 40


def _committed_registry(root):
    """`ops/entity-registry.json` as a plain `{id: {name, type, aliases}}` dict, or `{}` on
    missing/malformed. Silent on a malformed file — `check_registry` above already reports that as
    its own finding — because this reader exists only to VALIDATE `entity:` values elsewhere, and a
    broken registry means every declared value reads as unresolved, not a second copy of that
    error."""
    registry_file = root / REGISTRY_RELPATH
    if not registry_file.is_file():
        return {}
    try:
        data = json.loads(registry_file.read_text(encoding="utf-8"))
        registered = data["entities"]
        return registered if isinstance(registered, dict) else {}
    except (ValueError, KeyError, OSError):
        return {}


def _entity_registry_index(root):
    """`(ids, name_index, alias_index)` — the linter's own reading of "which entities exist and
    under what names", built once per scan and shared by every page's `entity:` validation.
    `name_index`/`alias_index` map a `normalize_name()` key to `(id, the_original_spelling)`, so a
    finding can name the id AND show the display name/alias the page actually used."""
    registered = _committed_registry(root)
    ids = set(registered)
    name_index, alias_index = {}, {}
    for cid, entity in registered.items():
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        key = normalize_name(name)
        if key:
            name_index.setdefault(key, (cid, name))
        for alias in (entity.get("aliases") or []):
            alias = str(alias)
            key = normalize_name(alias)
            if key:
                alias_index.setdefault(key, (cid, alias))
    return ids, name_index, alias_index


def _entity_values(raw):
    """`entity:`'s bare-string-or-list dialect, normalized (M8a criterion 2): a list stays a list,
    a bare string becomes a one-element list, and an empty string normalizes to NO values at all
    (`""` -> `[]`, never `[""]` — the exact edge the migration witness watches for on the platform
    side)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(v) for v in raw if str(v).strip()]
    text = str(raw).strip()
    return [text] if text else []


def _unresolved_entity_message(value, ids):
    """M8a UX §3.1, the recommended (adopted) shape: the registry listed in full below the bound,
    named by count above it — never silently truncated."""
    fix = ("Register it first (capture something about it, or register it from the console), or use one "
          "of these ids if it's actually about an entity already registered under a different "
          "name." if ids else
          "Register it first (capture something about it, or register it from the console).")
    if not ids:
        return (f'entity: "{value}" does not match any registered entity — nothing is '
                f'registered yet. {fix}')
    if len(ids) <= MAX_ENTITY_LISTING:
        listing = ", ".join(sorted(ids))
        return (f'entity: "{value}" does not match any registered entity — not as an id, a '
                f'display name, or an alias of one. Registered ids today: {listing}. {fix}')
    return (f'entity: "{value}" does not match any registered entity — not as an id, a display '
            f'name, or an alias of one. {len(ids)} ids are registered today, too many to list '
            f'here. Register it first (capture something about it, or register it from the console), or '
            f'check `{REGISTRY_RELPATH}` if it is actually about an entity already registered '
            f'under a different name.')


def check_entity_field(p, fm, registry_index, add):
    """`entity:`'s own contract (M8a spec §4.1/§4.4, criteria 2/3): every declared value must be a
    REGISTERED ID — never a display name, never an alias, and never something nothing registers at
    all. Silent when the page declares no `entity` key (absence is a pre-contract page, not a
    validity error — spec §4.1).

    All three findings are `error`, matching this file's existing severity doctrine (§3.5): a
    value that silently anchors to nothing, or to the wrong id, is the class this repo already
    blocks on rather than treats as a judgment call.
    """
    if "entity" not in fm:
        return
    ids, name_index, alias_index = registry_index
    for value in _entity_values(fm.get("entity")):
        if value in ids:
            continue
        key = normalize_name(value)
        hit = name_index.get(key)
        if hit:
            cid, _name = hit
            add("error", "frontmatter", p,
                f'entity: "{value}" is a registered entity\'s DISPLAY NAME, not its id — use the '
                f'id instead: entity: ["{cid}"]')
            continue
        hit = alias_index.get(key)
        if hit:
            cid, alias = hit
            add("error", "frontmatter", p,
                f'entity: "{value}" is a registered entity\'s ALIAS ("{alias}"), not its id — '
                f'use the id instead: entity: ["{cid}"]')
            continue
        add("error", "frontmatter", p, _unresolved_entity_message(value, ids))


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+\S")


def valid_date(s):
    if not DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# A top-level frontmatter key, QUOTED OR NOT.
#
# The quoted alternatives are a fix, not generality. This parser used to read
# `^([A-Za-z_][\w-]*):`, which cannot see a quoted key at all — while PyYAML, and
# therefore everything downstream that actually consumes these pages, reads
# `"owner": "someone@else.com"` as a perfectly ordinary `owner` field. So a page
# could carry a machine-only field, an `acl`, or an out-of-enum `status`, and EVERY
# rule below was blind to it. That is worse than a missing rule: it is a rule that
# reports clean about something it never looked at.
#
# Normalizing here rather than in each rule is what makes the fix total — the rules
# are unchanged and all of them gain the coverage at once, which is the only way to
# be sure none was left behind.
KEY_RE = re.compile(r"""^(?:"([^"]*)"|'([^']*)'|([A-Za-z_][\w-]*))\s*:\s*(.*)$""")

# A `#` that starts a YAML comment: preceded by whitespace or the start of the value — NOT
# required to be followed by whitespace too. The old rule here (`\s+#\s`) demanded a trailing
# space, which PyYAML does not: `title: Acme #2 Holdings` is `Acme` plus a comment to PyYAML
# (the `#` is preceded by a space), not `Acme #2 Holdings` verbatim. That single-character
# disagreement is exactly the M8a findings-cycle-1 criterion-11 reproduction — `registry_id`
# folds the *linter's* (wrong) reading, the generator folds PyYAML's, and the two ids differ on
# a repo with no registry file to catch it, so the linter stays clean while the worker's
# regeneration refuses. Fixed here for the RULE itself; getting the rule applied correctly to
# every SHAPE a value can take (a quoted scalar with a trailing comment, a `#` inside a quoted
# inline-list element) took a second pass — `_strip_comment` and `_end_of_bracket` below are
# where that shows up (findings cycle 2, A2: six more silent-divergence constructs, both
# directions). "Once, and used everywhere" was true of the regex; it was not yet true of the
# parser as a whole.
_COMMENT_RE = re.compile(r"(?:^|\s)#")

# YAML node markers this line-oriented parser does not implement AT ALL: `>`/`|` open a
# folded/literal block scalar (whose continuation lines this parser cannot follow), `&` defines
# an anchor, `*` dereferences one, `!` opens an explicit tag (`!!str`, `!custom`). Guessing at
# these (treating the marker as literal text) is unsafe: it is exactly how the linter and the
# generator (real PyYAML) silently disagreed (M8a findings cycle 1, group 2). A value opening
# with one of these becomes a loud parse error instead — the safe direction, and it collapses
# what would otherwise be separate silent-divergence instances into one message.
_UNREPRESENTABLE_MARKERS = ">|&*!"

# YAML 1.1 implicit-typing words a BARE (unquoted) frontmatter value must never be, because
# PyYAML's resolver reads each of these as a bool/null, not the literal text — the contract's
# fields are all strings, so accepting one here as a string is a silent divergence from what
# the worker's registry regeneration (real PyYAML) actually stores (findings cycle 2, A2.3). Refusing
# is the doctrine this cycle sets: "when you cannot represent something faithfully, refuse it
# loudly — do not guess." Quoting the value sidesteps the whole class (a quoted scalar is never
# implicitly retyped), so the fix is always available to whoever wrote the page.
_YAML_BOOL_WORDS = frozenset({
    "y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO",
    "true", "True", "TRUE", "false", "False", "FALSE",
    "on", "On", "ON", "off", "Off", "OFF",
})
_YAML_NULL_WORDS = frozenset({"~", "null", "Null", "NULL"})
# `12:34` -> 754 to PyYAML's sexagesimal (base-60) int resolver — any run of `\d+` segments
# joined by `:`.
_SEXAGESIMAL_RE = re.compile(r"^\d+(:\d+)+$")

# Fields the ingest pipeline legitimately writes as a YAML BOOLEAN, not a string — the "where a
# string is meant" qualifier in the fix instruction. `detail_in_source` is the one real case today
# (`pipeline/ingest/model/page.py`: `fm.append("detail_in_source: true")`, unquoted, on purpose —
# a spreadsheet-summary page's machine-readable signal to go to the live source for exact
# figures). Named explicitly, verified against the real producer, rather than exempted by a
# pattern ("looks like a flag field") — a NEW field that happens to read `yes`/`On` by accident
# must still be caught; only a field verified to be an intentional bool gets waived.
BOOLEAN_FIELDS = frozenset({"detail_in_source"})


def _is_ambiguous_scalar(key, value):
    """True when a BARE `value` on a field the contract expects to be a STRING is one of the
    words/patterns PyYAML's implicit resolver types as something other than a string. `key` in
    `BOOLEAN_FIELDS` is exempt — a string is not meant there, so PyYAML retyping it is correct,
    not a divergence."""
    if key in BOOLEAN_FIELDS:
        return False
    return (value in _YAML_BOOL_WORDS or value in _YAML_NULL_WORDS
           or bool(_SEXAGESIMAL_RE.match(value)))


def _ambiguous_scalar_error(key, value):
    return (f'{key}: {value!r} is an unquoted YAML 1.1 implicit value — a real YAML parser '
           f'(PyYAML, what the worker regenerates the registry with) reads it as a boolean, null, or '
           f'sexagesimal number, not literal text. Quote it (e.g. "{value}") if a plain string '
           f'is meant')


def _end_of_quoted(s, i):
    """Index just past the matching closing quote for the quoted scalar opening at `s[i]` (a `"`
    or `'`), honouring the one escape form each quote style uses: a backslash-escaped character
    inside a double-quoted scalar (`\\"` in particular) and a doubled `''` inside a single-quoted
    one are literal, not the close. Returns `len(s)` when the quote is never closed (malformed
    input; "everything after this point is still inside the string" is the safe fallback)."""
    quote = s[i]
    j, n = i + 1, len(s)
    while j < n:
        ch = s[j]
        if quote == '"' and ch == "\\" and j + 1 < n:
            j += 2
            continue
        if ch == quote:
            if quote == "'" and j + 1 < n and s[j + 1] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _has_escaped_quote(value):
    """True when a fully-quoted `value` (`value[0] == value[-1]`, a quote char) embeds an escaped
    quote — `\\"` inside a double-quoted scalar, `''` inside a single-quoted one. This
    line-oriented parser does not implement YAML's escape/unescape rules faithfully enough to
    resolve one without risking exactly the silent-mangling defect this exists to close (findings
    cycle 2, A2.4/A2.6: the old bare `.strip('"\\'')` dropped only the OUTER quote pair and left
    the interior backslashes as literal text). Refusing is the doctrine this cycle sets — not a
    weaker substitute for unescaping correctly."""
    if len(value) < 2 or value[0] != value[-1] or value[0] not in "\"'":
        return False
    inner = value[1:-1]
    return '\\"' in inner if value[0] == '"' else "''" in inner


def _scalar_error(key, value):
    """None when `value` — already an isolated scalar token (a top-level value, a block-list
    item, or one inline-list element), already comment-stripped — is safe to store as literal
    string content; otherwise the refusal message this parser prints instead of guessing. A fully
    quoted value is always safe UNLESS it embeds an escape this parser does not implement
    (`_has_escaped_quote`). An unquoted value is refused when it opens with a construct this
    parser does not implement at all (`_UNREPRESENTABLE_MARKERS`), or is a bare word/pattern
    PyYAML's implicit typing resolves to a non-string (`_is_ambiguous_scalar`)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        if _has_escaped_quote(value):
            return (f"{key}: {value} — this line-oriented parser does not implement quote-"
                   f"escaping inside a quoted scalar; rewrite it without an embedded quote "
                   f"character")
        return None
    if value and value[0] in _UNREPRESENTABLE_MARKERS:
        return _unrepresentable_error(key, value)
    if _is_ambiguous_scalar(key, value):
        return _ambiguous_scalar_error(key, value)
    return None


def _unrepresentable_error(key, value):
    return (f"{key}: value begins with {value[0]!r} — this page uses YAML the contract linter "
           f"does not implement (a folded/literal block scalar, an anchor, an alias reference, "
           f"or an explicit tag); simplify it to a plain scalar or a quoted string")


def _strip_comment(value):
    """Strip a YAML comment from `value`, quote-state-aware across the WHOLE string — not "a
    value opening with a quote is never followed by a real comment". A quoted value CAN still
    carry a genuine trailing comment after its closing quote (`"Acme" # the client` is `Acme` to
    PyYAML, with the tail as a comment); the old early-return kept that tail glued onto the
    value, which is exactly the M8a findings-cycle-2 A2.1 divergence. When `value` opens with a
    quote, this scans to the matching close (honouring escapes, `_end_of_quoted`) and only then
    looks for a comment in what follows; otherwise it behaves as before."""
    if value.startswith(("\"", "'")):
        end = _end_of_quoted(value, 0)
        quoted, remainder = value[:end], value[end:]
        m = _COMMENT_RE.search(remainder)
        if m:
            remainder = remainder[: m.start()]
        return (quoted + remainder).rstrip()
    m = _COMMENT_RE.search(value)
    return value[: m.start()].rstrip() if m else value


def _end_of_bracket(s, i):
    """Index just past the matching closing `]` for the inline list opening at `s[i] == '['`,
    skipping over quoted segments (`_end_of_quoted`) so a literal `]` or `#` inside a quoted
    element is never mistaken for the list's own close or a trailing comment — the M8a
    findings-cycle-2 A2.5/A2.6 divergence (`aliases: ["Acme #1 Holdings", "Beta"]` was corrupted
    by comment-stripping the RAW bracketed value before this boundary was ever found). Returns
    `len(s)` when the bracket is never closed."""
    depth, j, n = 0, i, len(s)
    while j < n:
        ch = s[j]
        if ch in "\"'":
            j = _end_of_quoted(s, j)
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _split_inline_list(inner):
    """Split an inline `[...]`'s inner text on commas, honouring quoted segments — a comma
    inside a quoted value (`aliases: ["Borealis Dynamics, S.L."]`, a legal company-name form and
    exactly the shape `_MATCH_SUFFIXES` exists to fold) must not be read as a second list
    element. Boundary-finding is delegated to `_end_of_quoted`, so an escaped quote inside a
    quoted element is skipped correctly too (M8a findings-cycle-2 A2.6) — this function only
    finds the split points; `_scalar_error` (applied by the caller, per element) is what refuses
    an element it still cannot represent faithfully."""
    values, i, n, start = [], 0, len(inner), 0
    while i < n:
        ch = inner[i]
        if ch in "\"'":
            i = _end_of_quoted(inner, i)
            continue
        if ch == ",":
            values.append(inner[start:i].strip())
            i += 1
            start = i
            continue
        i += 1
    values.append(inner[start:].strip())
    return [v for v in values if v]


def parse_frontmatter(text):
    """Minimal flat-YAML frontmatter parser. Returns (dict, error_string)."""
    if not text.startswith("---"):
        return None, "no frontmatter block"
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, "frontmatter block never closed"
    fm, current_key = {}, None
    for raw in lines[1:end]:
        # A tab ANYWHERE in a frontmatter line: refused before anything else. PyYAML's scanner
        # refuses a tab used as structural whitespace (`title:\tAcme`, a tab-indented line) with
        # a ScannerError; this line-oriented parser has no scanner to draw that same fine
        # distinction (a tab inside a comment or a quoted value is legal to PyYAML but vanishingly
        # unlikely to be intentional in this contract's frontmatter), so it refuses the whole line
        # rather than risk silently accepting a shape PyYAML would reject (M8a findings cycle 2,
        # A2.2) — the conservative superset, matching this cycle's "refuse loudly" doctrine.
        if "\t" in raw:
            return None, (
                f"frontmatter line contains a tab character — PyYAML's scanner refuses that "
                f"(ScannerError: found character '\\t' that cannot start any token); replace it "
                f"with spaces: {raw!r}")
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", line)
        if item and current_key:
            value = _strip_comment(item.group(1).strip())
            err = _scalar_error(current_key, value)
            if err:
                return None, err
            fm[current_key].append(value.strip("\"'"))
            continue
        kv = KEY_RE.match(line)
        if not kv:
            continue  # lenient: skip lines we don't understand
        # Whichever of the three spellings matched IS the key; downstream sees one name.
        key = next((g for g in kv.groups()[:3] if g is not None), "")
        value = kv.group(4).strip()
        if not key:
            continue

        # Bracket detection runs on the RAW value, BEFORE any comment-stripping — comment-
        # stripping the whole bracketed value first (the old order) is exactly what let a `#`
        # inside a quoted list element (`aliases: ["Acme #1 Holdings", "Beta"]`) truncate the
        # entire value at that `#`, well before the real closing `]` (M8a findings cycle 2, A2.5).
        if value.startswith("["):
            b_end = _end_of_bracket(value, 0)
            if b_end <= len(value) and value[b_end - 1] == "]":
                bracket_text, remainder = value[:b_end], value[b_end:]
                m = _COMMENT_RE.search(remainder)
                if m:
                    remainder = remainder[: m.start()]
                inner = bracket_text[1:-1].strip()
                items = _split_inline_list(inner) if inner else []
                for it in items:
                    err = _scalar_error(key, it)
                    if err:
                        return None, err
                fm[key] = [v.strip("\"'") for v in items]
                current_key = None
                continue
            # else: opens with `[` but never closes — falls through to plain-scalar handling
            # below, same lenient posture this parser already takes on lines it doesn't fully
            # understand.

        value = _strip_comment(value)
        if value == "":
            fm[key], current_key = [], key  # may become a block list
        else:
            err = _scalar_error(key, value)
            if err:
                return None, err
            fm[key], current_key = value.strip("\"'"), None
    return fm, None


FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")


def strip_code(text):
    """Blank out fenced blocks and inline code so literal `[[x]]` isn't a link."""
    text = FENCE_RE.sub("", text)
    return INLINE_CODE_RE.sub("", text)


def link_stem(target):
    """The page name a wikilink target resolves to: its last path segment, minus a trailing
    `.md`. A title keeps every dot it has."""
    name = target.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def link_targets(text):
    targets = []
    for m in WIKILINK_RE.finditer(strip_code(text)):
        t = m.group(1).split("|")[0].split("#")[0].strip()
        if t:
            targets.append(t)
    return targets


def body_of(text):
    if text.startswith("---"):
        parts = text.split("\n")
        for i in range(1, len(parts)):
            if parts[i].strip() == "---":
                return "\n".join(parts[i + 1:])
    return text


def body_line_count(text):
    """Content lines of the body, ignoring leading/trailing blank lines."""
    lines = body_of(text).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return len(lines)


def find_empty_sections(text):
    lines = body_of(text).split("\n")
    empty, current, has_content = [], None, True
    for line in lines:
        if HEADING_RE.match(line):
            if current is not None and not has_content:
                empty.append(current)
            level = len(line) - len(line.lstrip("#"))
            current = line.strip() if level >= 2 else None
            has_content = current is None
        elif line.strip():
            has_content = True
    if current is not None and not has_content:
        empty.append(current)
    return empty


def zone_key(rel_parts):
    """Map a page's path to its zone key (see ZONE_TYPES / CONTENT_ROOTS)."""
    if not rel_parts:
        return None
    if rel_parts[0] == "wiki" and len(rel_parts) > 1:
        return f"wiki/{rel_parts[1]}"
    if rel_parts[0] in CONTENT_ROOTS:
        return rel_parts[0]
    return None


def scan(root):
    findings = []

    def add(severity, check, path, message):
        findings.append({
            "severity": severity, "check": check,
            "file": str(path.relative_to(root)) if isinstance(path, Path) else path,
            "message": message,
        })

    pages = []
    for zone in CONTENT_ROOTS:
        d = root / zone
        if d.is_dir():
            pages.extend(p for p in d.rglob("*.md") if not p.name.startswith("."))
    pages.sort()

    by_name = defaultdict(list)
    for p in pages:
        by_name[p.stem].append(p)

    texts = {p: p.read_text(encoding="utf-8", errors="replace") for p in pages}

    # duplicate basenames (case-insensitive: macOS + Obsidian resolution).
    lower_names = defaultdict(list)
    for p in pages:
        lower_names[p.stem.lower()].append(p)
    for name, paths in sorted(lower_names.items()):
        if len(paths) > 1:
            listed = ", ".join(str(q.relative_to(root)) for q in paths)
            add("error", "duplicates", paths[0], f"duplicate basename breaks wikilinks: {listed}")

    inbound = defaultdict(set)
    page_aliases = []
    frontmatter = {}        # page -> parsed frontmatter, for the cross-page rules below
    # Built ONCE per scan (M8a criterion 3) — every page's `entity:` validation reads the SAME
    # registry snapshot, so a scan cannot disagree with itself about which ids exist.
    registry_index = _entity_registry_index(root)
    for p in pages:
        text = texts[p]
        rel = p.relative_to(root).parts
        zone = zone_key(rel)
        in_sources = rel and rel[0] == "sources"

        fm, err = parse_frontmatter(text)
        if err:
            add("error", "frontmatter", p, err)
            fm = {}
        else:
            for field in (MACHINE_REQUIRED_FIELDS if in_sources else REQUIRED_FIELDS):
                if field not in fm or fm[field] in ("", []):
                    add("error", "frontmatter", p, f"missing required field: {field}")
            if not in_sources:
                for field in RECOMMENDED_FIELDS:
                    if field not in fm:
                        add("warn", "frontmatter", p, f"missing recommended field: {field}")

            ftype = fm.get("type")
            if ftype and ftype not in VALID_TYPES:
                if in_sources:
                    add("warn", "frontmatter", p,
                        f"machine-page type outside contract enum: {ftype!r} "
                        "(pipeline dialect; mapped at M1b — SI-02)")
                else:
                    add("error", "frontmatter", p, f"invalid type: {ftype!r}")
            if fm.get("status") and fm["status"] not in VALID_STATUS:
                add("error", "frontmatter", p, f"invalid status: {fm['status']!r}")

            # enum checks on optional contract fields
            if fm.get("source_kind") and fm["source_kind"] not in VALID_SOURCE_KIND:
                add("error", "frontmatter", p, f"invalid source_kind: {fm['source_kind']!r}")
            if fm.get("tier") is not None and str(fm["tier"]) not in VALID_TIER:
                add("error", "frontmatter", p, f"invalid tier: {fm['tier']!r} (expected 1|2|3)")

            # list fields must be lists
            for field in LIST_FIELDS:
                if field in fm and not isinstance(fm[field], list):
                    add("error", "frontmatter", p, f"{field} must be a list, got: {fm[field]!r}")

            # entity: aboutness (M8a spec §4.1/§4.4, criteria 2/3). NOT in LIST_FIELDS above —
            # a bare string is this field's OWN dialect (the pipeline's `entity: initech`), not a
            # mistake, so `check_entity_field` normalizes it rather than erroring on its shape.
            check_entity_field(p, fm, registry_index, add)

            # calendar dates
            for field in ("created", "updated", "as_of", "started"):
                v = fm.get(field)
                # as_of may be a coarser granularity (YYYY or YYYY-MM); only
                # full dates are calendar-checked.
                if isinstance(v, str) and DATE_RE.match(v) and not valid_date(v):
                    add("error", "frontmatter", p, f"{field} is not a valid date: {v!r}")

            # zone <-> type
            allowed = ZONE_TYPES.get(zone)
            if allowed and ftype and ftype in VALID_TYPES and ftype not in allowed:
                add("error", "zones", p,
                    f"type {ftype!r} not allowed in zone {zone!r} (expected {sorted(allowed)})")

            # per-type required schema
            for field in TYPE_REQUIRED.get(ftype, []):
                if field not in fm or fm[field] in ("", []):
                    add("error", "schema", p, f"type {ftype!r} requires field: {field}")

            # M8a (spec §4.4, criterion 13): the `submitted_by`-keyed zone-ownership rule that used
            # to live here is REMOVED, not merely narrowed. It exempted any page carrying
            # `submitted_by` from the "machine-only field on an authored page" check — exactly the
            # loophole SI-07's own resolution named: a HAND-TYPED `submitted_by` on a
            # `wiki/entities/` page (a folder the fast lane may never write to at all) earned
            # a pass. This linter is a stateless scan of one working tree; it has no way to check
            # who committed a field, only whether its VALUE is well-formed — so legitimacy of a
            # trust field is now entirely the knowledge repo's CI author check's job (over commit
            # history), and this scan keeps only validity rules. A page carrying a hand-typed
            # `content_hash` produces no finding from THIS rule — correct and intentional, not a
            # gap: the CI check catches it from a different angle (who committed it), which is the
            # whole reason SI-07 deferred this exact rewrite here.

            # M8a findings cycle 1, group 3: provenance SHAPE — see MACHINE_ONLY_FIELDS's own
            # comment for why this is not the authorship rule removed above. A `wiki/**` page
            # has no legitimate way to carry machine provenance; `sources/` is exempt
            # as MACHINE_REQUIRED_FIELDS pages. (The v1 `datasets/`/`meta/` half of this check
            # died with those zones at P2 — findings cycle 2 A7's reasoning is at the tag.)
            if rel and rel[0] == "wiki":
                for field in MACHINE_ONLY_FIELDS:
                    if field in fm:
                        add("error", "zones", p,
                            f"{field!r} is machine provenance the librarian stamps "
                            f"onto sources/ pages — a {rel[0]}/ page has no legitimate "
                            f"way to declare it, by hand or otherwise; remove it")

            # sources/ pages need provenance
            if in_sources:
                for field in PROVENANCE_REQUIRED:
                    if field not in fm or fm[field] in ("", []):
                        add("error", "zones", p,
                            f"sources/ page missing provenance field: {field}")

            if isinstance(fm.get("aliases"), list):
                for alias in fm["aliases"]:
                    if isinstance(alias, str) and alias and alias.lower() != p.stem.lower():
                        page_aliases.append((p, alias))

        frontmatter[p] = fm

        # size rule (30-150 body lines): oversize errors, undersize warns. The
        # `representation: full` escape hatch went at P2 — nothing has written that field since
        # the pipeline that did was retired, and an oversize source page is SPLIT into cross-linked parts
        # by the librarian rather than kept whole, so the branch could never fire.
        n = body_line_count(text)
        if n > SIZE_MAX:
            add("error", "size", p, f"body is {n} lines (max {SIZE_MAX}); split and cross-link")
        elif n < SIZE_MIN and fm.get("type") not in ("entity", "source"):
            # M8a (spec §4.4, criterion 12): an entity page is a SPINE, not an essay — a
            # deliberately short stub should not warn. Not padded to satisfy the floor either
            # (the template stays unpadded): trading one warning (thin page) for another (an
            # empty section) would be worse than exempting the type outright.
            #
            # A `source` page is exempt for a stronger reason: its body is the captured material,
            # VERBATIM, and its length is the submitter's rather than an author's. Every capture
            # archives one now, so a short note would warn on every filing — and the only way to
            # answer such a warning would be to pad evidence, which is falsifying it.
            add("warn", "size", p, f"body is {n} lines (min {SIZE_MIN}); thin page")

        for section in find_empty_sections(text):
            add("warn", "empty_sections", p, f"empty section: {section!r}")

        # wikilink resolution
        for target in link_targets(text):
            # A wikilink target names a page by TITLE, optionally path-shaped
            # (`[[wiki/entities/X.md]]`): the last segment, with only a trailing `.md` taken off.
            # Never `Path(target).stem`, which amputates a dotted name — `[[Acme Inc. Invoices]]`
            # became `Acme Inc` and a live page was vetoed as a dead link.
            stem = link_stem(target)
            if stem not in by_name:
                add("error", "dead_links", p, f"dead link: [[{target}]]")
            else:
                for q in by_name[stem]:
                    inbound[q].add(p)

    # alias collisions
    seen_alias = {}
    for p, alias in page_aliases:
        al = alias.lower()
        others = [q for q in lower_names.get(al, []) if q != p]
        if others:
            add("error", "aliases", p,
                f"alias {alias!r} collides with page {others[0].relative_to(root)}")
        if al in seen_alias and seen_alias[al] != p:
            add("error", "aliases", p,
                f"alias {alias!r} already declared by {seen_alias[al].relative_to(root)}")
        else:
            seen_alias.setdefault(al, p)

    # the derived view: entity pages <-> ops/entity-registry.json
    check_registry(root, pages, texts, frontmatter, add)

    # orphans (generated meta pages are exempt)
    for p in pages:
        if not inbound.get(p):
            add("warn", "orphans", p, "no inbound links from any content page")

    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repo root (default: cwd)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any error-severity finding (CI gate)")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if not any((root / z).is_dir() for z in CONTENT_ROOTS):
        print(f"fatal: {root} has no content zones {CONTENT_ROOTS}", file=sys.stderr)
        return 2

    findings = scan(root)
    order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["check"], f["file"]))
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warn")

    if args.json:
        print(json.dumps({"summary": {"errors": errors, "warnings": warnings},
                          "findings": findings}, indent=2, ensure_ascii=False))
    else:
        print(f"{errors} errors, {warnings} warnings\n")
        for f in findings:
            print(f"[{f['severity'].upper():5}] {f['check']:14} {f['file']}: {f['message']}")
        if not findings:
            print("stigmergy is clean.")

    if args.strict and errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
