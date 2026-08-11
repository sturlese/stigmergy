"""The deterministic gatherer: what the filing agent is HANDED before it goes looking itself.

A model grepping for "what does this brain already know about Northwind" spends turns, spends
tokens, and answers a question code can answer exactly. So code answers it, once, before the model
call, and the answer travels in the prompt (ADR 033).

**It is a SEED, not a boundary, and that is ADR 034's correction.** For one milestone the gathered
block was the whole of the ordinary agent's context — it held no tool and could not look further.
The exploring agent came back on this project's own harness, so the block is now the starting point
of a run that can also `search_pages` and `read_page` its way outward. The tools are implemented by
the functions in THIS module (`load_corpus`, `search_candidates`, `confined_page`), which is what
keeps one lexical ranking and one containment rule behind both roads instead of two.

**A pure function of `(worktree, registry, material)`.** No database, no clock, no network, no
model. That is what makes it unit-testable without a key and what makes the same capture gather
the same context twice — the property a golden run depends on, since a gatherer that reorders its
own output makes two runs of one model incomparable.

**It reads the CHECKOUT, never `pages_index`.** The worktree is the knowledge repo at this item's
base commit, which is the same data the retired exploring agent's own `Glob`/`Read` reached — so the data ORIGIN
is unchanged and only the READER moved from the model to code. Reading the index instead would put
a write-path worker on the read path's ACL-governed table and would need an exception entry it has
no business needing (`server.acl.visible()` is where read access is decided, and nothing here
serves a reader). Recorded in ADR 033; reopening it needs a design, not a patch.

**That "same data" claim is TRUE BECAUSE OF `_confined`, and false without it.** The retired
harness's reads were bounded by a permission hook that resolved `realpath` first;
`corpus.load_pages` is the INDEX's parser and has no such notion, so every row it hands back is
filtered here before anything else looks at one. Without that filter this module would read
strictly MORE of the filesystem than the exploring shape it replaced, which is a regression wearing
a refactor's clothes. **Those same two halves are what `confined_page` below asks for the
`read_page` tool**, under an allow-list of its own (the content zones, plus the per-type page
templates a run that writes its own container needs) — confinement lives INSIDE the tool now rather
than in a permission hook, and it is the same containment code in the same module rather than a
second opinion about it.

**One cross-package reach, declared.** `stigmergy.index.corpus` is imported for its parser —
`load_pages`/`ZONES`, pure code over a directory, no database and no ACL surface — the same edge
`librarian.edits` already declares for `ZONES` and `views.skeleton`/`staleness` declare for the
same reason. It is a LIBRARY reach, not a layer: nothing here touches `pages_index`, and a change
that made this import need one would be a design change, not a wider import.

**What it does NOT reuse, and why.** `dedup.py`'s two levels are DB-backed (`find_retry`,
`find_already_filed`) and have ALREADY run by the time this is called — `processing._pre_agent`
collapses a retry and refuses an exact re-file before the worktree even exists. There is no pure
overlap signal left in that module to share, so this one does not fabricate a second: the ranked
`candidates` list below IS the overlap signal the brief asks the agent to judge against, and
saying so here is more honest than an import that would look like reuse.

**Every page-derived string in here is captured material on the way back into a prompt** — titles
somebody wrote, bodies somebody wrote — so `agent.render_gathered` fences the whole content half.
The structural half (entity ids and names, and each entity's page path) is rendered unfenced, and
what makes THAT inert is JSON escaping plus `text.sanitize`, not its provenance — a page path is a
filename a person chose. `structural_payload`'s own docstring carries the argument. Nothing in this
module builds a fence itself (`tests/test_architecture.py` keeps the fence in one place); this
module produces plain data and `agent.py` decides how it is framed.
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

# The zone a CANDIDATE may come from. `wiki/` only, and the two exclusions are decisions rather
# than an oversight:
#
#  * `views/` is regenerated from an entity's members and is never a wikilink TARGET at all
#    (`corpus.by_stem_index` drops the zone for exactly that reason) — handing one to the agent as
#    a page to overlap with or link to would invite a link that resolves to nothing;
#  * `sources/` is verbatim captured evidence. It is a legitimate link target (it stays in
#    `link_names` below), but it is not a knowledge destination: a transcript never "covers the
#    same ground" as a synthesis in the sense the overlap judgment means, and excerpting one would
#    spend the excerpt budget re-showing the agent raw material.
CANDIDATE_ZONE = "wiki/"

# The link neighbourhood's own ceiling — one hop out of the candidates and the entity pages, and
# a hop is fanout-shaped. A number rather than a `Settings` field on purpose: `top_k` and the
# excerpt height are the two dials an operator would ever tune (they trade prompt cost against
# recall), and this one only bounds a list of `path`/`title` pairs that costs almost nothing.
MAX_NEIGHBOURS = 40

# How many page NAMES the wikilink vocabulary may carry before it is reported as a count instead.
# A truncated vocabulary is worse than an honest count for the same reason
# `gates.MAX_BRIEF_REGISTRY_NAMES` says it is: "not in the list" would read as proof that a name
# does not exist when it is merely unlisted, and the agent would then decline a link it should have
# made — or, worse, make one it should not.
MAX_LINK_NAMES = 400

# One excerpt line's own ceiling. A page is line-bounded by the contract linter, not
# CHARACTER-bounded, so a single pathological line can carry a page's whole body.
MAX_EXCERPT_LINE = 400

# Below this many pages, the corpus is too small for "a term most pages carry is noise" to mean
# anything — in a five-page repo the most common term may be the one the material is about. The
# fixture repos this suite runs against sit under it, which is deliberate: their scoring is the
# plain overlap count, with nothing filtered.
MIN_CORPUS_FOR_TERM_FREQUENCY = 8

# A term is a word of at least three characters. Two-character words carry almost no signal and
# every language is full of them; digits are kept (a year, a version, an amount is often exactly
# the thing two pages share).
_TERM_RE = re.compile(r"\w{3,}", re.UNICODE)

# The weights, and they are ordinal rather than tuned: a term shared with a page's TITLE is
# stronger evidence than one shared with the names it links, which is stronger than one shared
# with its body. Integers, so a score is exactly reproducible and a tie is a real tie.
_TITLE_WEIGHT = 3
_RELATED_WEIGHT = 2
_BODY_WEIGHT = 1


@dataclass(frozen=True)
class GatheredEntity:
    """One registered entity the MATERIAL names, resolved through the registry's own alias map.

    `page_path` is `""` when the registry knows the entity and this checkout carries no page for
    it — a real and legitimate state (an entity is minted in `ops/entity-registry.json`, and its
    page is written by the steward flow, not by the fast lane), and one the agent must be able to
    tell apart from "this entity does not exist".
    """
    entity_id: str
    name: str
    aliases: tuple = ()
    page_path: str = ""


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
    """Everything code found in the checkout about one capture, frozen.

    Frozen for the reason `agent.Outcome` is: it is evidence about what the model was shown, and
    a prompt builder must not be able to edit the context into agreement with the answer.
    """
    entities: tuple = ()
    candidates: tuple = ()
    neighbours: tuple = ()
    link_names: tuple = ()
    link_names_total: int = 0
    corpus_pages: int = 0


@dataclass(frozen=True)
class Corpus:
    """The checkout's pages, PARSED AND TOKENIZED ONCE — the input every ranking below reads.

    Split out of `gather` when the `search_pages` tool arrived, because that tool asks the same
    question the gather asks (rank these pages against this text) an unbounded number of times per
    run. Parsing the whole checkout per tool call would make the model's own curiosity quadratic in
    the size of the knowledge repo, on the one step whose cost already scales with it
    (`config.GATE_BUDGET_S`'s own comment carries that argument).

    `rows` is already `_confined` — nothing downstream re-filters, and nothing downstream should
    have to know it must.
    """
    rows: tuple
    by_path: dict
    terms_by_path: dict


def load_corpus(worktree: str) -> Corpus:
    """Parse and tokenize the checkout once. `gather` calls it, and so does a run that holds the
    search tool — one parse, one containment filter, one tokenization, whoever is asking."""
    rows = _confined(worktree, corpus.load_pages(worktree))
    # Tokenized ONCE per row and threaded through every reader. `_corpus_stopwords` and
    # `_candidates` each used to tokenize the whole corpus themselves, and `_candidates` tokenized
    # every body a second time inside its own loop — four full passes over every byte of the
    # checkout, per agent pass, on a step that runs twice per capture. One pass, one dict.
    return Corpus(rows=tuple(rows),
                  by_path={row.path: row for row in rows},
                  terms_by_path={row.path: _terms(f"{row.title}\n{row.body}") for row in rows})


def search_candidates(parsed: Corpus, query: str, *, top_k: int, excerpt_lines: int,
                      skip=()) -> list[GatheredPage]:
    """The SAME lexical ranking `gather` builds its candidate list with, over an arbitrary query.

    The `search_pages` tool's whole body. It is a public name rather than a second scorer for the
    reason this module has one scorer at all: a tool that ranked pages differently from the seeded
    block would hand the model two disagreeing answers to "what does this brain already hold about
    X" inside one run, and neither could be trusted to mean what the other means.

    Deterministic, like everything else here: the same `(corpus, query, bounds)` returns the same
    list, ties broken by path.
    """
    return _candidates(parsed.rows, query, parsed.terms_by_path, top_k=top_k,
                       excerpt_lines=excerpt_lines, skip=set(skip))


# The per-type page TEMPLATES, which are readable and are the one thing outside the content zones
# that is (see `confined_page`). Spelled here rather than imported: `stigmergy.entities` names the
# same directory (`mint.TEMPLATE_RELPATH`) and this package may never import that one — the edge
# runs the other way — so the duplication is DECLARED, exactly as this package's own code map says
# to handle a fact both sides need.
TEMPLATE_DIR = "ops/templates"


def confined_page(worktree: str, relpath: str) -> str:
    """The canonical repo-relative path of a page a READER may open, or `""` if it may not.

    **`_confined`'s rule, asked of one path instead of a corpus** — the `read_page` tool's gate,
    and the reason confinement lives inside the tool rather than in a permission hook: a hook is a
    second implementation of a rule that has been wrong three times in this repo, and the shape of
    that rule does not change just because a different harness asks it.

    **The shape test judges the RESOLVED path, not the asked string** — the same order
    `agent.confined_write` resolves in, and for the same reason. An earlier version split the asked
    string lexically, ran the zone/template test on THAT, and then opened the resolved path: a
    directory component symlinked to a non-zone directory INSIDE the worktree
    (`wiki/mirror -> .claude`) passed every check — containment proved it was inside,
    `os.path.islink` saw an ordinary file at the leaf, and the shape test saw a first segment of
    `wiki` — and read any `.md` in the repo, the agent's own brief included. Resolving first is what
    makes the shape test judge where the path REALLY lands.

    Three questions, and all three have to be `yes`:

      * the LEAF is not itself a symlink (`os.path.islink` on the asked path). A link that points
        back INSIDE the worktree is contained and still not the bytes git tracks at that name, so
        containment alone never catches it — and it is checked BEFORE resolving, because resolving
        is exactly what would hide it;
      * the RESOLVED path is contained (`realpath` then a prefix test) — so `../../ops/acl.json`,
        an absolute path outside the worktree, and a directory component symlinked OUT are all
        refused;
      * the resolved repo-relative path is on the READ ALLOW-LIST below. Containment alone would
        admit `.git/config`, every dotfile and `ops/acl.json` itself — the same argument
        `agent.confined_write`'s allow-list makes about writes, and the reason this is an allow-list
        rather than "is it inside".

    **The allow-list is the content zones plus `ops/templates/*.md`, and the second half is not a
    convenience.** A run that writes its own page writes the page's CONTAINER too, and the
    per-type template is the structural source of truth for what that container owes — the
    knowledge repo's own contract linter says so in its header, and the harness this milestone
    restores read exactly these files before drafting. "Copy the shape from an existing page of the
    same type" is not a substitute: a young brain has no page of that type to copy, and the fast
    lane's own fixture has no `wiki/concepts` page at all.

    It is exactly `ops/templates/<name>.md`, at that directory's top level. Not `ops/` (that is
    where `acl.json` and the entity registry live — the two files whose reading this rule exists to
    refuse), not a subdirectory, and not a traversal that resolves back out: the RESOLVED path is
    matched as THREE segments, so `ops/templates/../acl.json` fails the shape test even though it
    resolves inside the worktree.

    Returns the canonical RESOLVED relpath so the caller reads (and echoes) the file it was judged
    on rather than re-resolving the asked string and possibly getting another.
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
    """The whole gather, in one pass over the checkout.

    `top_k` and `excerpt_lines` are the caller's (`config.Settings.gather_top_k` /
    `gather_excerpt_lines`) rather than defaults here, for this package's standing reason: a bound
    with a default at the point of use is a bound two places can disagree about.

    Deterministic end to end — `corpus.load_pages` returns rows sorted by path, every ranking below
    breaks its ties by path, and every list is materialized in a stated order. Two calls with the
    same three arguments return equal objects.
    """
    parsed = load_corpus(worktree)
    rows = parsed.rows
    entities = _entities(rows, registry, material)
    entity_paths = {e.page_path for e in entities if e.page_path}

    candidates = search_candidates(parsed, material, top_k=top_k, excerpt_lines=excerpt_lines,
                                   skip=entity_paths)
    neighbours = _neighbours(parsed.by_path,
                             [*(c.path for c in candidates), *sorted(entity_paths)],
                             skip=entity_paths | {c.path for c in candidates})

    # The wikilink vocabulary, read through the SAME function `edits.validate` answers "does this
    # link resolve" with. A second walk of the checkout is the price of not having a second answer:
    # a gatherer that offered the agent a name the edit validator would then refuse is precisely
    # the drift this repo pays a full corrective retry for. `confined=True` applies the containment
    # filter below to that walk too — see `_confined`.
    names = sorted(edits.page_names(worktree, confined=True))
    log.info("gathered for one capture: %d entities, %d candidate(s) of %d page(s), %d neighbour(s)",
             len(entities), len(candidates), len(rows), len(neighbours))
    return Gathered(
        entities=tuple(entities),
        candidates=tuple(candidates),
        neighbours=tuple(neighbours),
        link_names=tuple(names[:MAX_LINK_NAMES]),
        link_names_total=len(names),
        corpus_pages=len(rows),
    )


def _confined(worktree: str, rows: list) -> list:
    """Every row whose bytes really came from INSIDE this capture's own checkout.

    **This is what makes "the reader moved, the data origin did not" true rather than merely
    intended** (ADR 033 D1). The exploring agent's reads were confined by a `PreToolUse` hook that
    resolved `realpath` before allowing one (`agent.confine_reads` -> `page.is_inside`), so a
    `wiki/notes/x.md` symlinked at `/etc/passwd`, or a `wiki/playbooks` directory component
    symlinked out of the worktree, was denied. `corpus.load_pages` has no such notion — it is the
    INDEX's parser, walking `rglob("*.md")` and `read_text`-ing whatever it finds — so without this
    filter the structured shape would read strictly MORE than the shape it replaces, and a page's
    body would reach a model prompt from outside the commit being filed against.

    Fixed HERE and never in `corpus.py`: that module belongs to the index, whose own callers walk a
    checkout they cloned themselves, and pushing a librarian confinement rule into it would make one
    package's threat model another package's default.

    Both halves are needed and neither implies the other: `page.is_inside` resolves the whole path
    (so a symlinked DIRECTORY component is caught, which an `islink` test on the leaf never sees),
    and `os.path.islink` on the leaf catches a symlink pointing back INSIDE the worktree — legal by
    containment, and still a file whose bytes are not the ones git tracks at that path.

    Logged at WARNING rather than INFO: a symlinked page inside a knowledge repo has no legitimate
    producer in this system, so it is an indicator, not housekeeping.
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


# ── which entities the material names ─────────────────────────────────────────────────────────
def _entities(rows, registry, material: str) -> list[GatheredEntity]:
    """The registered entities this material mentions, by the registry's OWN spellings.

    Matching is never re-implemented here: the candidate set comes from `gates.registry_candidates`
    (THE one reading of "which entities exist" — see its docstring for why a second one is worse
    than none), and a matched spelling is turned into an id by `Registry.canonical_id`, which is
    the same normalization `gates.resolve_entity_ids` resolves a DECLARED anchor with. So an
    entity the gatherer surfaces is an entity the anchoring gate would resolve, by construction.

    Whole-TOKEN containment, not substring: `Marlowe` must not match inside `marlowepublishing`,
    and a substring test over a normalized haystack is exactly how a gatherer starts handing an
    agent entities the material never mentioned.
    """
    hay = f" {' '.join(_tokens(material))} "
    resolve = getattr(registry, "canonical_id", None)
    found: dict[str, GatheredEntity] = {}
    for entry in gates.registry_candidates(registry):
        aliases = tuple(entry.get("aliases") or ())
        for spelling in (entry.get("name", ""), *aliases):
            if not _mentions(hay, spelling):
                continue
            entity_id = str(resolve(spelling) or "") if callable(resolve) else ""
            if not entity_id or entity_id in found:
                continue
            found[entity_id] = GatheredEntity(
                entity_id=entity_id, name=str(entry.get("name", "")), aliases=aliases,
                page_path=entity_page(rows, entity_id, resolve))
            break
    return [found[key] for key in sorted(found)]


def _mentions(haystack: str, spelling: str) -> bool:
    """Does the tokenized material carry this spelling as a contiguous run of whole tokens?"""
    tokens = _tokens(spelling)
    return bool(tokens) and f" {' '.join(tokens)} " in haystack


def entity_page(rows, entity_id: str, resolve) -> str:
    """This entity's own page in the checkout, or `""`.

    PUBLIC because the `resolve_entities` tool answers the same question for a name the model asks
    about mid-run, and the docstring below is the whole argument for not letting it have its own
    idea of where entity pages live.

    Asked of the page's own `entity:` frontmatter FIRST — that field is server-stamped from a
    resolved id, so it is the fact — and of the title through the registry only as the fallback,
    for an entity page written before anything stamped one. Never by a filename convention: a
    `wiki/entities/<Name>.md` rule would be a fourth place that knows where entity pages live.
    """
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
    """The top-K pages this material overlaps with, lexically, deterministically.

    The score is a weighted count of DISTINCT material terms the page carries, by field:
    `3 x title + 2 x its outbound link names + 1 x body`. Integer arithmetic, so a tie is a real
    tie and is broken by path — the same "ties break by path" rule `corpus.load_pages` sorts under,
    which is what makes two gathers of one capture byte-identical.

    **The corpus decides what a stopword is.** A term carried by more than half the pages is
    dropped rather than counted, so "the", "page" and this brain's own house vocabulary stop
    dominating every score without anybody maintaining a word list — which would be one more thing
    to keep in step with a corpus that is not necessarily in English. Under
    `MIN_CORPUS_FOR_TERM_FREQUENCY` pages the filter is skipped entirely: in a five-page repo the
    commonest term may be exactly what the material is about.

    Pages with a score of zero are dropped: a candidate list padded to `top_k` with pages that
    share nothing is a list the agent has to disbelieve, and an empty list is the honest answer for
    material about something this brain has never seen.
    """
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
        # The BODY half reads the precomputed set (`terms_by_path` covers title + body, which is a
        # superset of the body alone — a title term counted twice is a title term the page really
        # carries, and the weights are ordinal rather than calibrated). Title and link names are
        # tokenized here because they are a handful of words each and no precomputation pays for
        # itself on them.
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
    """Terms carried by more than half this corpus's pages — see `_candidates` for the argument.

    Reads the tokenization `gather` already did rather than repeating it: this used to walk every
    body itself, which made the corpus-wide pass happen twice per agent pass and four times per
    capture, on the one step whose cost scales with the whole knowledge repo.
    """
    if len(rows) < MIN_CORPUS_FOR_TERM_FREQUENCY:
        return set()
    counts: dict[str, int] = {}
    for row in rows:
        for term in terms_by_path.get(row.path) or ():
            counts[term] = counts.get(term, 0) + 1
    ceiling = len(rows) // 2
    return {term for term, seen in counts.items() if seen > ceiling}


def _related_names(row) -> list[str]:
    """The page names this page links out to, as a reader would write them in a wikilink.

    `corpus.page_row` resolves `links` to repo-relative PATHS (the index's currency); a wikilink is
    written by BASENAME, so this is the same set spelled the way the agent has to spell it. Sorted,
    for determinism, and deduplicated — two links to one page are one name.
    """
    names = {path.rsplit("/", 1)[-1].removesuffix(".md") for path in (row.links or [])}
    return sorted(name for name in names if name)


def _excerpt(body: str, lines: int) -> str:
    """The first `lines` non-blank lines of a body, each sanitized and clamped.

    Non-blank rather than raw: a page that opens with its own H1 and two blank lines would
    otherwise spend a third of the budget on whitespace. Sanitized because these bytes go into a
    prompt (`text.sanitize` is the seam every other echoed value in this repo goes through) and
    clamped per line because a page is bounded by LINE count, not by characters, so one
    pathological line can carry the whole body.

    **`lines=0` means NO excerpts, and it is a supported setting rather than an accident.** The
    budget used to be checked AFTER the append, so a zero produced one line — the ablation an
    operator would reach for (`STIGMERGY_LIBRARIAN_GATHER_EXCERPT_LINES=0`: hand the model the
    candidate PATHS and titles and nothing of their content, to measure what the excerpts are
    worth) silently measured something else. The check moved above the append.
    """
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
    """One hop out of the candidates and the entity pages: what they link to.

    This is the half a lexical score cannot find. A capture about a renewal may share no
    vocabulary with the decision page that governs it, and still belong one link away from it —
    the graph knows something the words do not. Seeding one hop is what makes the second hop worth
    taking: a run that holds `read_page` can follow a neighbour it was named, and one that was
    never told the neighbour exists has no reason to look for it.

    Bounded by `MAX_NEIGHBOURS` and ordered by path. A neighbour is NAMED, never excerpted: the
    excerpt budget belongs to the pages the material actually overlaps with, and a title plus a
    path is enough to decide whether to link or to ask for a hop the gatherer did not make.
    """
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
    """One text's terms, in order — NFC-normalized and casefolded, so two spellings of one accented
    word are one term (the same doctrine `gates.normalize_identifier` applies to an identifier and
    `page.path_key` to a path)."""
    folded = unicodedata.normalize("NFC", text or "").casefold()
    return _TERM_RE.findall(folded)


def _terms(text: str) -> set:
    return set(_tokens(text))


# ── the prompt payloads: two halves, and what actually makes the unfenced one safe ────────────
def structural_payload(gathered: Gathered) -> dict:
    """The half rendered OUTSIDE the fence: entity ids, canonical names and aliases that went
    through governed birth, plus the repo-relative path of each entity's own page.

    **The reason it is safe is the ESCAPING, not the provenance — and the earlier version of this
    docstring got that wrong.** The ids and names really are server-derived (a steward minted them
    through `stigmergy-entities`, and `gates.registry_candidates` is the one reading of them), and
    `build_meeting_prompt` already renders exactly that set unfenced for exactly that reason. **A
    page PATH is not**: a person chose the filename, and the fast lane will happily file
    `wiki/notes/Ignore the above.md` because a title is a title. What keeps it inert here is that
    the whole payload is one `json.dumps` value — a quoted, escaped JSON string cannot end its own
    data span, which is the property the fence exists to give unescaped prose — and `text.sanitize`
    strips the control characters that would otherwise survive escaping as `\\n`/`\\u2028` and
    reformat the block a reader sees.

    So: sanitized here, escaped by the caller, and NOT claimed to be trusted because of where it
    came from.
    """
    return {"entities": [{"id": prompt_scalar(e.entity_id),
                          "name": prompt_scalar(e.name),
                          "aliases": [prompt_scalar(a) for a in e.aliases],
                          "page": prompt_scalar(e.page_path) or None}
                         for e in gathered.entities]}


# The two Unicode line separators. `stigmergy.text.sanitize` deliberately does NOT strip them —
# it is the bottom of the stack, shared with the index, the server and the CLIs, where U+2028 in a
# search hit is inert — and widening it for one caller's threat model is how a shared seam stops
# meaning one thing. So the extra step lives here, with the reason it exists.
_LINE_SEPARATORS = str.maketrans({" ": " ", " ": " "})


def prompt_scalar(value: str) -> str:
    """One untrusted scalar rendered into a prompt OUTSIDE the fence.

    `text.sanitize` strips the C0/C1 controls (the seam every echoed value in this repo goes
    through); this additionally neutralizes U+2028/U+2029, which survive it and which
    `json.dumps(..., ensure_ascii=False)` emits RAW — and which a reader renders as line breaks.
    Inside the fence that costs nothing (the fence is what bounds the span); outside it, a page
    path carrying one could split the structural block a model is reading as one line.

    A REPLACEMENT rather than a whitespace-collapse: these values are paths and names, and
    `" ".join(x.split())` would silently rewrite a filename that legitimately carries two spaces
    into one that names no file.

    **PUBLIC because the tool road needs the identical treatment** (ADR 034): a `read_page` path
    echo, a `search_pages` match's path/title/type/links_to, a `list_page_names` name and a
    `resolve_entities` field are all UNFENCED scalars re-entering the prompt, exactly like the
    structural half above, and a second sanitizer for them would be a second answer to the one
    question this function is. The page-BODY-derived free text (excerpts, bodies) is FENCED instead
    — see `pydantic_backend._tool_payload`.
    """
    return textutil.sanitize(str(value or "")).translate(_LINE_SEPARATORS)


def candidates_payload(candidates) -> list:
    """The candidate list's own JSON shape — ONE rendering, so `agent._within_budget` can measure a
    trimmed list against the same bytes `content_payload` will emit rather than approximating it."""
    return [{"path": c.path, "title": c.title, "type": c.page_type,
             "links_to": list(c.related), "excerpt": c.excerpt}
            for c in candidates]


def content_payload(gathered: Gathered, *, candidates=None) -> dict:
    """The half that is PAGE-DERIVED: titles, bodies and the names somebody chose for their own
    pages. Every string in here was written by a person or by a model reading captured material, so
    `agent.render_gathered` puts the whole of it inside the UNTRUSTED-DATA fence — a page excerpt
    re-entering a prompt is captured content on the way back in, and the fence is the only thing
    that stops one closing the data span early.

    `candidates` overrides the gathered list with the one that survived the whole-block size budget
    (`agent.MAX_GATHERED_CHARS`). Defaulting to `gathered.candidates` keeps every other caller —
    and every test that builds a `Gathered` by hand — reading exactly what it gathered.
    """
    chosen = gathered.candidates if candidates is None else candidates
    return {
        "candidates": candidates_payload(chosen),
        "neighbourhood": [{"path": n.path, "title": n.title} for n in gathered.neighbours],
        "link_names": list(gathered.link_names),
        "link_names_total": gathered.link_names_total,
        "corpus_pages": gathered.corpus_pages,
    }
