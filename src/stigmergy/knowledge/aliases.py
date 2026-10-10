"""Record the other names an entity is linked by, from the links a filing plan adds.

A candidate is a `[[Entity|variant]]` link the plan added whose variant names no page yet. Each one
gets a single boolean question to the typed-decision classifier, within a time window; only an
answer at or above `THRESHOLD` writes an alias, and either every accepted alias lands or none does.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from stigmergy.index.corpus import WIKILINK_RE, strip_code
from stigmergy.index.rank import summary_of
from stigmergy.kernel import llm
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge.pages import FOLDERS, PageContractError, parse_page, render_like, title_stem

THRESHOLD = 0.75
MAX_CANDIDATES = 8
WINDOW_S = 10.0
SENTENCE_SIDE_CHARS = 150
ALIAS = "alias"
OTHER = "other"
OPTIONS = {
    ALIAS: ("The candidate is another name for this same entity: a spelling, an abbreviation, a short "
            "form, a first name, a full name, or a former name."),
    OTHER: ("The candidate is not another name for this entity: it is a description, a role, a title, a "
            "part of the entity, something it owns or makes, or the name of a different entity."),
}
INSTRUCTIONS = ("A wiki page links the entity below with the candidate text. Is the candidate another name "
                "for the entity?")


@dataclass(frozen=True)
class Candidate:
    entity: str          # the entity page's path in the writing view
    text: str            # the variant the link displays
    sentence: str        # the words around the link, links shown as their text


def _links(text: str) -> list[tuple[str, str, int, str]]:
    """`(target, shown text, offset, line)` for every wikilink outside code, as the writer reads them."""
    found = []
    for line in strip_code(text).splitlines():
        for match in WIKILINK_RE.finditer(line):
            target, separator, label = match.group(1).partition("|")
            name = target.split("#", 1)[0].strip().removesuffix(".md")
            found.append((name, label.strip() if separator else name, match.start(), line))
    return found


def _sentence(line: str, offset: int) -> str:
    """The words around a link, with every link shown as its text."""
    window = line[max(0, offset - SENTENCE_SIDE_CHARS): offset + 2 * SENTENCE_SIDE_CHARS]

    def shown(match):
        target, separator, label = match.group(1).partition("|")
        return label if separator else target

    return " ".join(WIKILINK_RE.sub(shown, window).split())


def _entity_pages(root: str) -> dict[str, object]:
    folder = Path(root, *FOLDERS["entity"].split("/"))
    pages = {}
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else ():
        relative = path.relative_to(root).as_posix()
        try:
            pages[relative] = parse_page(relative, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, PageContractError):
            continue
    return pages


def candidates(root: str, written: dict[str, str | None], *, link_names: dict[str, str | None],
               eligible) -> tuple[list[Candidate], dict[str, object]]:
    """The variants the plan added, from `written` (`{path: its text before the plan, or None}`), that
    name no page yet: not the entity's title or alias; not a name another page answers to
    (`link_names` maps casefolded link names to a page stem, `None` when ambiguous); not used by the
    plan for another entity; with letters or digits; not words all found in another entity's name (a
    first name two people share); and on an entity `eligible(path)` lets the capture name."""
    entities = _entity_pages(root)
    by_stem = {Path(path).stem.casefold(): path for path in entities}
    words = {path: [set(key.split()) for key in page.name_keys()] for path, page in entities.items()}
    added: list[tuple[str, str, str]] = []
    for relative, before in written.items():
        file = Path(root, *relative.split("/"))
        if not file.is_file():
            continue
        try:
            body = parse_page(relative, file.read_text(encoding="utf-8")).body
            old = parse_page(relative, before).body if before else ""
        except (OSError, UnicodeError, PageContractError):
            continue
        existing = Counter((name.casefold(), shown) for name, shown, _o, _l in _links(old))
        for name, shown, offset, line in _links(body):
            if existing[(name.casefold(), shown)]:
                existing[(name.casefold(), shown)] -= 1
                continue
            target = by_stem.get(title_stem(name).casefold())
            if target is not None:
                added.append((target, shown, _sentence(line, offset)))
    linked: dict[str, set[str]] = {}
    for target, shown, _sentence_text in added:
        linked.setdefault(resolution_key(shown), set()).add(target)
    found: dict[tuple[str, str], Candidate] = {}
    for target, shown, sentence in added:
        key = resolution_key(shown)
        if (not any(ch.isalnum() for ch in key) or key in entities[target].name_keys()
                or title_stem(shown).casefold() in link_names or linked[key] != {target}
                or not eligible(target)
                or any(set(key.split()) <= name for path, names in words.items() if path != target
                       for name in names)):
            continue
        found.setdefault((target, key), Candidate(target, shown, sentence))
    return list(found.values()), entities


def decide(candidate: Candidate, page, *, timeout_s: float = llm.DECISION_TIMEOUT_S) -> float:
    """The probability that the candidate is another name for the entity; raises
    `llm.DecisionFailed` when the classifier gives no usable answer within `timeout_s`."""
    state = {
        "entity": {"title": page.title, "type": page.entity_type or "",
                   "summary": summary_of(page.body), "aliases": list(page.aliases)},
        "candidate": candidate.text,
        "sentence": candidate.sentence,
    }
    answer = llm.choose(OPTIONS, state, instructions=INSTRUCTIONS, timeout_s=timeout_s)
    if set(answer) != set(OPTIONS):
        raise llm.DecisionFailed("invalid_response")
    return float(answer[ALIAS])


def record(root: str, written: dict[str, str | None], *, link_names: dict[str, str | None], eligible,
           at, window_s: float, path_of, max_candidates: int = MAX_CANDIDATES,
           start: int = 0) -> tuple[list[dict], dict[str, str]]:
    """Decide the candidates within `window_s` seconds, then write every accepted alias into its
    entity page (`path_of(relative)` is the guarded file path) or, on any failure, none. Returns the
    report entries and `{path: text before}` for the pages it changed."""
    found, entities = candidates(root, written, link_names=link_names, eligible=eligible)
    if found:
        found = found[start % len(found):] + found[:start % len(found)]
    report: list[dict] = []
    accepted: list[Candidate] = []
    started = time.monotonic()
    for index, candidate in enumerate(found):
        entry = {"entity": candidate.entity, "alias": candidate.text}
        remaining = window_s - (time.monotonic() - started)
        if index >= max_candidates or remaining <= 0:
            reason = "cap" if index >= max_candidates else "deadline"
            report.append({**entry, "decision": "skipped", "reason": reason})
            continue
        try:
            probability = decide(candidate, entities[candidate.entity],
                                 timeout_s=min(remaining, llm.DECISION_TIMEOUT_S))
        except llm.DecisionFailed as error:
            report.append({**entry, "decision": "skipped", "reason": error.reason})
            continue
        decision = "accepted" if probability >= THRESHOLD else "rejected"
        report.append({**entry, "decision": decision, "probability": round(probability, 4)})
        if decision == "accepted":
            accepted.append(candidate)
    before: dict[str, str] = {}
    try:
        for candidate in accepted:
            file = path_of(candidate.entity)
            text = file.read_text(encoding="utf-8")
            before.setdefault(candidate.entity, text)
            page = parse_page(candidate.entity, text)
            file.write_text(render_like(page, updated=at, aliases=(*page.aliases, candidate.text)),
                            encoding="utf-8")
    except BaseException:
        for relative, text in before.items():
            path_of(relative).write_text(text, encoding="utf-8")
        raise
    return report, before
