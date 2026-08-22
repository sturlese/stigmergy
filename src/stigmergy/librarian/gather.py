"""The deterministic gatherer: what the filing agent is HANDED before it goes looking itself.

A SEED, not a boundary — the run can `search_pages` and `read_page` its way outward, and those
tools ARE this module's `load_corpus`/`search_candidates`/`confined_page`, so one lexical ranking
and one containment rule sit behind both roads. A pure function of `(worktree, registry,
material)`, reading the CHECKOUT and never `pages_index`: a write-path worker must not touch the
read path's ACL-governed table. Every page-derived string here is captured material on its way
back into a prompt, but nothing here builds a fence; `agent.py` decides the framing.
"""
import logging
import os
import re
import unicodedata
from dataclasses import dataclass

from stigmergy import text as textutil
from stigmergy.index import corpus
from stigmergy.librarian import edits, gates
from stigmergy.librarian import page as page_policy

log = logging.getLogger(__name__)

# The zone a CANDIDATE may come from. `wiki/` only: `views/` is regenerated and is never a
# wikilink target, and `sources/` is verbatim evidence — linkable, but not a knowledge
# destination worth spending the excerpt budget on.
CANDIDATE_ZONE = "wiki/"

# The link neighbourhood's ceiling — a hop is fanout-shaped. Not a `Settings` field: it bounds
# `path`/`title` pairs that cost almost nothing.
MAX_NEIGHBOURS = 40

# How many page NAMES the wikilink vocabulary carries before it is reported as a count instead: a
# truncated vocabulary reads as proof a name does not exist when it is merely unlisted.
MAX_LINK_NAMES = 400

# A page is line-bounded by the linter, so one pathological line can carry a whole body.
MAX_EXCERPT_LINE = 400

# How many registry entities one capture is handed. A bound is needed now that a NEAR miss can
# surface an entry the material never spells: without one, a registry of a thousand entities could
# put hundreds into a prompt on a single common token. `entities_total` beside the list is what
# keeps a cut list from reading as "the registry holds nothing else" — the same rule `link_names`
# has carried since ADR 033.
MAX_ENTITIES = 25

# The shortest token run that may surface an entry it is only PART of. `_TERM_RE` already floors a
# token at three characters; this floors the RUN, so `Co` or `SL` inside a registered name never
# drags that entity into a prompt on their own.
MIN_NEAR_RUN_CHARS = 4

# A registered spelling longer than this contributes no sub-runs: run enumeration is quadratic in
# the token count, and a name of nine words is not an abbreviation anybody types.
MAX_NEAR_SPELLING_TOKENS = 8

# How one entity reached the list. The agent needs the difference: `named` is a spelling the
# material actually carries, `near` is a candidate the material only PART of — a judgment call,
# never a resolution code has made.
MATCH_NAMED = "named"
MATCH_NEAR = "near"

# Below this, "a term most pages carry is noise" means nothing in a five-page repo.
MIN_CORPUS_FOR_TERM_FREQUENCY = 8

# Three characters minimum; digits kept, since a year or a version is often what two pages share.
_TERM_RE = re.compile(r"\w{3,}", re.UNICODE)

# Ordinal, not tuned: title beats link names beats body. Integers, so a tie is a real tie.
_TITLE_WEIGHT = 3
_RELATED_WEIGHT = 2
_BODY_WEIGHT = 1


@dataclass(frozen=True)
class GatheredEntity:
    """One registered entity this material names or nearly names. `page_path` is `""` when the
    registry knows the entity and the checkout carries no page for it — a legitimate state, not
    "does not exist". `match` says WHICH of the two it is (`MATCH_NAMED` / `MATCH_NEAR`), because
    only the agent may turn a near miss into an anchor and it cannot judge what it cannot see."""
    entity_id: str
    name: str
    aliases: tuple = ()
    page_path: str = ""
    match: str = MATCH_NAMED


@dataclass(frozen=True)
class GatheredPage:
    """One candidate page, with enough of it to judge overlap without reading the file."""
    path: str
    title: str
    page_type: str
    related: tuple = ()          # the names this page links out to, as a reader would write them
    excerpt: str = ""
    score: int = 0


@dataclass(frozen=True)
class GatheredLink:
    """One page a candidate or an entity page links to — the second hop, named but not excerpted."""
    path: str
    title: str


@dataclass(frozen=True)
class Gathered:
    """Everything code found in the checkout about one capture, frozen so a prompt builder cannot
    edit the context into agreement with the answer."""
    entities: tuple = ()
    entities_total: int = 0
    candidates: tuple = ()
    neighbours: tuple = ()
    link_names: tuple = ()
    link_names_total: int = 0
    corpus_pages: int = 0


@dataclass(frozen=True)
class Corpus:
    """The checkout's pages, PARSED AND TOKENIZED ONCE: `search_pages` asks its question an
    unbounded number of times per run. `rows` is already `_confined`; nothing downstream re-filters."""
    rows: tuple
    by_path: dict
    terms_by_path: dict


def load_corpus(worktree: str) -> Corpus:
    """Parse and tokenize the checkout once — one parse, one containment filter, one tokenization,
    whoever is asking."""
    rows = _confined(worktree, corpus.load_pages(worktree))
    return Corpus(rows=tuple(rows),
                  by_path={row.path: row for row in rows},
                  terms_by_path={row.path: _terms(f"{row.title}\n{row.body}") for row in rows})


def search_candidates(parsed: Corpus, query: str, *, top_k: int, excerpt_lines: int,
                      skip=()) -> list[GatheredPage]:
    """The SAME lexical ranking `gather` seeds its candidate list with — the `search_pages` tool's
    whole body. One scorer: a tool ranking differently would disagree with the seeded block."""
    return _candidates(parsed.rows, query, parsed.terms_by_path, top_k=top_k,
                       excerpt_lines=excerpt_lines, skip=set(skip))


# The per-type page TEMPLATES — the one readable thing outside the content zones. Spelled rather
# than imported: `stigmergy.entities` names the same directory and may never be imported here.
TEMPLATE_DIR = "ops/templates"


def confined_page(worktree: str, relpath: str) -> str:
    """The canonical repo-relative path of a page a READER may open, or `""` — the `read_page`
    tool's gate.

    Three questions, all yes: the LEAF is not a symlink (checked BEFORE resolving, since a link
    back inside the worktree is contained and still not the bytes git tracks); the RESOLVED path is
    contained; the resolved relpath is on the allow-list — the content zones plus exactly
    `ops/templates/<name>.md`, matched as THREE segments so `ops/templates/../identities.json` fails.
    The SHAPE test runs on the RESOLVED path, or a symlinked directory component reads any `.md`
    in the repo. Returns the resolved relpath, so the caller reads the file it was judged on.
    """
    rel = (relpath or "").strip().lstrip("/")
    if not rel:
        return ""
    root = os.path.realpath(worktree)
    asked = os.path.join(root, *rel.split("/"))
    if os.path.islink(asked):
        return ""
    try:
        resolved = os.path.realpath(asked)
    except (OSError, ValueError):
        return ""
    if resolved != root and not resolved.startswith(root + os.sep):
        return ""
    resolved_rel = os.path.relpath(resolved, root)
    if os.sep != "/":
        resolved_rel = resolved_rel.replace(os.sep, "/")
    parts = resolved_rel.split("/")
    if not parts[-1].endswith(".md") or parts[-1].startswith("."):
        return ""
    in_zone = parts[0] in corpus.ZONES
    is_template = len(parts) == 3 and "/".join(parts[:2]) == TEMPLATE_DIR
    if not (in_zone or is_template):
        return ""
    if not os.path.isfile(resolved):
        return ""
    return resolved_rel


def gather(worktree: str, registry, material: str, *, top_k: int,
           excerpt_lines: int) -> Gathered:
    """The whole gather, in one pass over the checkout. `top_k` and `excerpt_lines` are the
    caller's, never defaulted here: a bound with a default at the point of use is one two places
    can disagree about. Deterministic end to end — every ranking breaks ties by path."""
    parsed = load_corpus(worktree)
    rows = parsed.rows
    entities, entities_total = _entities(rows, registry, material)
    entity_paths = {e.page_path for e in entities if e.page_path}

    candidates = search_candidates(parsed, material, top_k=top_k, excerpt_lines=excerpt_lines,
                                   skip=entity_paths)
    neighbours = _neighbours(parsed.by_path,
                             [*(c.path for c in candidates), *sorted(entity_paths)],
                             skip=entity_paths | {c.path for c in candidates})

    # The SAME function `edits.validate` answers "does this link resolve" with: a gatherer that
    # offered a name the validator refuses costs a full corrective retry.
    names = sorted(edits.page_names(worktree, confined=True))
    log.info("gathered for one capture: %d of %d entities (%d a near miss), %d candidate(s) of "
             "%d page(s), %d neighbour(s)",
             len(entities), entities_total,
             sum(1 for e in entities if e.match == MATCH_NEAR),
             len(candidates), len(rows), len(neighbours))
    return Gathered(
        entities=tuple(entities),
        entities_total=entities_total,
        candidates=tuple(candidates),
        neighbours=tuple(neighbours),
        link_names=tuple(names[:MAX_LINK_NAMES]),
        link_names_total=len(names),
        corpus_pages=len(rows),
    )


def _confined(worktree: str, rows: list) -> list:
    """Every row whose bytes really came from INSIDE this capture's own checkout; otherwise a body
    could reach a model prompt from outside the commit being filed against.

    Both halves are needed: `page.is_inside` resolves the whole path, since a symlinked DIRECTORY
    component is invisible to a leaf `islink` test, and the leaf `islink` catches a link pointing
    back INSIDE the worktree — contained, and still not the bytes git tracks. Fixed HERE, never in
    `corpus.py`: one package's threat model must not become another's default.
    """
    kept, dropped = [], []
    for row in rows:
        full = os.path.join(worktree, row.path)
        if page_policy.is_inside(worktree, row.path) and not os.path.islink(full):
            kept.append(row)
        else:
            dropped.append(row.path)
    if dropped:
        log.warning("the gatherer dropped %d page(s) that do not resolve inside this capture's "
                    "checkout: %s — a symlinked page in a knowledge repo has no legitimate "
                    "producer in this system", len(dropped), ", ".join(sorted(dropped)))
    return kept


# ── which entities the material names, and which it nearly names ──────────────────────────────
def _entities(rows, registry, material: str) -> tuple[list[GatheredEntity], int]:
    """The registered entities this material names or NEARLY names, and how many there were before
    the bound cut the list.

    Two rules, in that priority order, because they answer two different questions:

    * **named** — a registry spelling appears in the material as a contiguous run of whole tokens.
      Unchanged, and it already covers a qualifier the registry does not carry: `Cofers Holdings`
      contains ` cofers `, so the registered `Cofers` is surfaced.
    * **near** — a DISTINCTIVE contiguous sub-run of a registry spelling appears in the material.
      This is the direction whole-token containment cannot reach and no skill wording can fix:
      material saying `Nexus` never contains ` ferrovial nexus `, so the registered `Ferrovial
      Nexus` never reached the agent at all. Distinctive means the run names exactly ONE registry
      entry (`_run_owners`), so a token two entities share drags in neither of them.

    Surfacing is not resolving. Both kinds are handed over labelled; which one this capture is
    about — and whether it is any of them — is the agent's judgment, fenced by
    `gates.resolve_entity_ids` (the id it declares must exist) and by the park (uncertainty asks).
    """
    resolve = getattr(registry, "canonical_id", None)
    matches, total = match_registry(registry, material)
    return [GatheredEntity(entity_id=entity_id, name=name, aliases=aliases,
                           page_path=entity_page(rows, entity_id, resolve), match=kind)
            for entity_id, name, aliases, kind in matches], total


def match_registry(registry, text: str, *, limit: int = MAX_ENTITIES) -> tuple[list[tuple], int]:
    """`([(id, name, aliases, match)], total_before_the_bound)` for one text — THE near-miss rule,
    with no checkout involved.

    Public and shared: the seeded context (`_entities`) and the agent's own `resolve_entities` tool
    both ask this, so the block a run is handed and the answer it gets when it asks again cannot
    disagree about what counts as a near miss. `_entities` adds the one thing that needs the
    checkout — where each entity's page is.
    """
    hay = f" {' '.join(_tokens(text))} "
    resolve = getattr(registry, "canonical_id", None)
    if not callable(resolve):
        return [], 0
    entries = _registry_entries(registry, resolve)
    owners = _run_owners(entries)

    named: dict[str, tuple] = {}
    near: dict[str, tuple] = {}
    for entity_id, name, aliases in entries:
        if entity_id in named or entity_id in near:
            continue
        spellings = [s for s in (name, *aliases) if s]
        if any(_mentions(hay, spelling) for spelling in spellings):
            named[entity_id] = (entity_id, name, aliases, MATCH_NAMED)
        elif any(_near_mentions(hay, spelling, owners, entity_id) for spelling in spellings):
            near[entity_id] = (entity_id, name, aliases, MATCH_NEAR)

    # NAMED first: a near miss must never displace an entity the material actually spells.
    ordered = [named[key] for key in sorted(named)] + [near[key] for key in sorted(near)]
    return ordered[:max(int(limit), 0)], len(ordered)


def _registry_entries(registry, resolve) -> list[tuple[str, str, tuple]]:
    """`(id, name, aliases)` for every entity the registry would resolve, read through
    `gates.registry_candidates` — THE one reading of "which entities exist" — and keyed by the id
    the registry itself gives one of its own spellings. An entry none of whose spellings resolves is
    dropped rather than guessed at: it is a registry the loader and the gate would disagree about."""
    out = []
    for entry in gates.registry_candidates(registry):
        name = str(entry.get("name", ""))
        aliases = tuple(str(alias) for alias in (entry.get("aliases") or ()))
        for spelling in (name, *aliases):
            entity_id = str(resolve(spelling) or "")
            if entity_id:
                out.append((entity_id, name, aliases))
                break
    return out


def _mentions(haystack: str, spelling: str) -> bool:
    """Does the tokenized material carry this spelling as a contiguous run of whole tokens?"""
    tokens = _tokens(spelling)
    return bool(tokens) and f" {' '.join(tokens)} " in haystack


def mentions(text: str, spelling: str) -> bool:
    """`_mentions` over raw text — THE one reading of "does the material name this". Shared with
    `librarian.identity`, which refuses a proposed entity the material never spells: a second
    tokenizer there would let a name pass one rule and fail the other."""
    return _mentions(f" {' '.join(_tokens(text))} ", spelling)


def _token_runs(spelling: str, *, proper_only: bool = False) -> list[str]:
    """Every contiguous whole-token run of one spelling, space-joined. `proper_only` drops the run
    that IS the whole spelling, which the named rule already owns. Ordered and deduplicated, so two
    registries with the same entities produce the same index."""
    tokens = _tokens(spelling)[:MAX_NEAR_SPELLING_TOKENS]
    runs = {" ".join(tokens[start:stop])
            for start in range(len(tokens))
            for stop in range(start + 1, len(tokens) + 1)}
    if proper_only:
        runs.discard(" ".join(tokens))
    return sorted(run for run in runs if run)


def _run_owners(entries) -> dict:
    """`run -> the set of entity ids whose spellings contain it`. The distinctiveness index: a run
    owned by two entities identifies neither, and surfacing both on it would hand the agent a
    coin-flip dressed as a candidate list. Full runs count too, so a registered `Cofers` stops
    `Cofers Legal` from being offered as the near miss of a bare "Cofers"."""
    owners: dict = {}
    for entity_id, name, aliases in entries:
        for spelling in (name, *aliases):
            for run in _token_runs(spelling):
                owners.setdefault(run, set()).add(entity_id)
    return owners


def _near_mentions(haystack: str, spelling: str, owners: dict, entity_id: str) -> bool:
    """Does the material carry a distinctive PART of this spelling? See `_entities` for the rule and
    `MIN_NEAR_RUN_CHARS` for why a short run does not count."""
    for run in _token_runs(spelling, proper_only=True):
        if len(run) < MIN_NEAR_RUN_CHARS:
            continue
        if owners.get(run) != {entity_id}:
            continue
        if f" {run} " in haystack:
            return True
    return False


def entity_page(rows, entity_id: str, resolve) -> str:
    """This entity's own page in the checkout, or `""`; PUBLIC because `resolve_entities` asks it
    mid-run. The server-stamped `entity:` frontmatter is the fact, the title only a fallback."""
    for row in rows:
        if str(row.type or "").lower() == "entity" and entity_id in (row.entity or []):
            return row.path
    if callable(resolve):
        for row in rows:
            if str(row.type or "").lower() != "entity":
                continue
            if str(resolve(row.title or "") or "") == entity_id:
                return row.path
    return ""


# ── the ranked candidates ─────────────────────────────────────────────────────────────────────
def _candidates(rows, material: str, terms_by_path: dict, *, top_k: int, excerpt_lines: int,
                skip: set) -> list[GatheredPage]:
    """The top-K pages this material overlaps with, lexically and deterministically: a weighted
    count of DISTINCT material terms, `3 x title + 2 x outbound link names + 1 x body`, ties broken
    by path. The CORPUS decides what a stopword is, so no word list is maintained for a corpus that
    may not be in English, and a score of zero is dropped rather than ranked."""
    pages = [row for row in rows
             if row.path.startswith(CANDIDATE_ZONE)
             and str(row.type or "").lower() != "entity"
             and row.path not in skip]
    material_terms = _terms(material) - _corpus_stopwords(rows, terms_by_path)
    if not material_terms:
        return []

    scored = []
    for row in pages:
        related = _related_names(row)
        # `terms_by_path` covers title + body; double-counting a title term is fine, the weights
        # are ordinal.
        page_terms = terms_by_path.get(row.path) or set()
        score = (_TITLE_WEIGHT * len(material_terms & _terms(row.title))
                 + _RELATED_WEIGHT * len(material_terms & _terms(" ".join(related)))
                 + _BODY_WEIGHT * len(material_terms & page_terms))
        if score > 0:
            scored.append((-score, row.path, row, related, score))
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [GatheredPage(path=row.path, title=str(row.title or ""),
                         page_type=str(row.type or ""), related=tuple(related),
                         excerpt=_excerpt(row.body, excerpt_lines), score=score)
            for _neg, _path, row, related, score in scored[:max(int(top_k), 0)]]


def _corpus_stopwords(rows, terms_by_path: dict) -> set:
    """Terms carried by more than half this corpus's pages, from the tokenization already done."""
    if len(rows) < MIN_CORPUS_FOR_TERM_FREQUENCY:
        return set()
    counts: dict[str, int] = {}
    for row in rows:
        for term in terms_by_path.get(row.path) or ():
            counts[term] = counts.get(term, 0) + 1
    ceiling = len(rows) // 2
    return {term for term, seen in counts.items() if seen > ceiling}


def _related_names(row) -> list[str]:
    """The page names this page links out to, spelled the way a wikilink is written: `page_row`
    resolves `links` to PATHS, and a wikilink names a BASENAME. Sorted and deduplicated."""
    names = {path.rsplit("/", 1)[-1].removesuffix(".md") for path in (row.links or [])}
    return sorted(name for name in names if name)


def _excerpt(body: str, lines: int) -> str:
    """The first `lines` non-blank lines of a body, sanitized (these bytes go into a prompt) and
    clamped per line. `lines=0` is a supported ablation, so the budget check sits above the append."""
    budget = max(int(lines), 0)
    out = []
    for line in (body or "").splitlines():
        if len(out) >= budget:
            break
        if not line.strip():
            continue
        out.append(textutil.clamp(textutil.sanitize(line), MAX_EXCERPT_LINE))
    return "\n".join(out)


# ── the link neighbourhood ────────────────────────────────────────────────────────────────────
def _neighbours(by_path: dict, sources: list, *, skip: set) -> list[GatheredLink]:
    """One hop out of the candidates and entity pages — the half a lexical score cannot find, since
    a capture may share no vocabulary with the decision page that governs it. Named, never
    excerpted."""
    out: dict[str, GatheredLink] = {}
    for path in sources:
        row = by_path.get(path)
        for target in (row.links if row else ()) or ():
            if target in skip or target in out:
                continue
            neighbour = by_path.get(target)
            if neighbour is None:
                continue
            out[target] = GatheredLink(path=target, title=str(neighbour.title or ""))
    return [out[key] for key in sorted(out)][:MAX_NEIGHBOURS]


# ── tokens ────────────────────────────────────────────────────────────────────────────────────
def _tokens(text: str) -> list[str]:
    """One text's terms, NFC-normalized and casefolded, so two spellings of an accented word are
    one term."""
    folded = unicodedata.normalize("NFC", text or "").casefold()
    return _TERM_RE.findall(folded)


def _terms(text: str) -> set:
    return set(_tokens(text))


# ── the prompt payloads: two halves, and what makes the unfenced one safe ─────────────────────
def structural_payload(gathered: Gathered) -> dict:
    """The half rendered OUTSIDE the fence: entity ids, names, aliases, page paths and how each one
    matched.

    What makes it safe is the ESCAPING, not the provenance — a page PATH is a filename a person
    chose, and the fast lane will happily file `wiki/notes/Ignore the above.md`. The payload is
    one `json.dumps` value, which cannot end its own data span, plus `text.sanitize`.

    `match` and `entities_total` are server-computed scalars, not captured text: the first says
    whether the material actually spells this entity, the second keeps a bounded list from reading
    as the whole registry.
    """
    return {"entities": [{"id": prompt_scalar(e.entity_id),
                          "name": prompt_scalar(e.name),
                          "aliases": [prompt_scalar(a) for a in e.aliases],
                          "page": prompt_scalar(e.page_path) or None,
                          "match": prompt_scalar(e.match)}
                         for e in gathered.entities],
            "entities_total": gathered.entities_total}


# `stigmergy.text`'s, re-exported under the name this package's call sites and
# `tests/test_architecture.py`'s allowlists already know it by. It MOVED down to the bottom of the
# stack because the gardener's prompt builders need the identical hygiene and may not import the
# librarian — and a fourth prompt builder anywhere would otherwise have a reason to re-derive it.
prompt_scalar = textutil.prompt_scalar


def candidates_payload(candidates) -> list:
    """The candidate list's own JSON shape — ONE rendering, so `agent._within_budget` can
    measure a trimmed list against the same bytes `content_payload` will emit."""
    return [{"path": c.path, "title": c.title, "type": c.page_type,
             "links_to": list(c.related), "excerpt": c.excerpt}
            for c in candidates]


def content_payload(gathered: Gathered, *, candidates=None) -> dict:
    """The PAGE-DERIVED half — titles, bodies and chosen names, which `agent.render_gathered` puts
    inside the UNTRUSTED-DATA fence. `candidates` overrides with the list that survived the
    whole-block size budget."""
    chosen = gathered.candidates if candidates is None else candidates
    return {
        "candidates": candidates_payload(chosen),
        "neighbourhood": [{"path": n.path, "title": n.title} for n in gathered.neighbours],
        "link_names": list(gathered.link_names),
        "link_names_total": gathered.link_names_total,
        "corpus_pages": gathered.corpus_pages,
    }
