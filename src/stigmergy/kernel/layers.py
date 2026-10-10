"""Audience layers: names, the virtual/physical path mapping, and page-body fusion.

`public` is stored at the repository root. Every other layer stores the same layout under
`layers/<layer>/`. Readers and the librarian address pages by their virtual path, the path without
that prefix; the same virtual path may exist once in each layer.

A body is an H1, then sections separated by headings. Inside a section a unit is a list item with
its continuation lines, a paragraph, or a contradiction callout. Units are the grain at which the
writer separates a layer's additions from the public page and at which readers see pages fused.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

PUBLIC = "public"
LAYER_ROOT = "layers"
RESERVED_NAMES = frozenset({PUBLIC, "brain-admins", LAYER_ROOT, "wiki", "sources", "all"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
MARKER_RE = re.compile(r"\[layer:(?P<layer>[a-z][a-z0-9-]{1,31})\] ")
_ANY_MARKER_RE = re.compile(r"\\?" + MARKER_RE.pattern)


def escape_markers(text: str) -> str:
    """Stored text that already looks like a marker, escaped: only fusion says where a unit comes from."""
    return MARKER_RE.sub(lambda found: "\\" + found.group(0), text)


def strip_markers(text: str) -> str:
    """Text without layer markers, escaped or not."""
    return _ANY_MARKER_RE.sub("", text)


class LayerError(ValueError):
    pass


def is_layer_name(name: object) -> bool:
    """A name a restricted layer may take; `public` is the root and never a stored group."""
    return isinstance(name, str) and bool(_NAME_RE.fullmatch(name)) and name not in RESERVED_NAMES


def layer_of_acl(acl) -> str:
    """The layer a stored page or capture audience denotes."""
    if acl is None:
        return PUBLIC
    values = tuple(acl)
    if len(values) != 1 or not is_layer_name(values[0]):
        raise LayerError("an audience names exactly one layer")
    return values[0]


def acl_of_layer(layer: str) -> tuple[str, ...] | None:
    if layer == PUBLIC:
        return None
    if not is_layer_name(layer):
        raise LayerError("layer name is invalid")
    return (layer,)


def physical_path(layer: str, virtual: str) -> str:
    if layer == PUBLIC:
        return virtual
    if not is_layer_name(layer):
        raise LayerError("layer name is invalid")
    return f"{LAYER_ROOT}/{layer}/{virtual}"


def split_path(path: str) -> tuple[str, str]:
    """`(layer, virtual path)` of a repository path."""
    if not path.startswith(f"{LAYER_ROOT}/"):
        return PUBLIC, path
    parts = path.split("/", 2)
    if len(parts) != 3 or not is_layer_name(parts[1]) or not parts[2]:
        raise LayerError("layer path is invalid")
    return parts[1], parts[2]


def virtual_path(path: str) -> str:
    return split_path(path)[1]


def unit_key(value: str) -> str:
    """Whitespace- and punctuation-variant-insensitive identity of a prose unit."""
    value = unicodedata.normalize("NFKC", value).translate(_PUNCTUATION)
    return " ".join(value.split())


_PUNCTUATION = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
    }
)


# ── body model ────────────────────────────────────────────────────────────────────────────────
_HEADING_RE = re.compile(r"^#{2,6}\s+\S")
_ITEM_RE = re.compile(r"^(?:[-*+]|\d{1,9}[.)])\s+")
_CALLOUT_START = "<!-- stigmergy-contradiction-start:"
_CALLOUT_END = "<!-- stigmergy-contradiction-end:"
_DATE_RE = re.compile(r"^(?:[-*+]|\d{1,9}[.)])?\s*(?:\*\*)?(\d{4}-\d{2}-\d{2})\b")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)")


@dataclass
class Unit:
    kind: str  # "item", "paragraph", or "callout"
    text: str
    layer: str = PUBLIC

    @property
    def key(self) -> str:
        return unit_key(self.text)


@dataclass
class Section:
    heading: str | None
    units: list[Unit] = field(default_factory=list)

    @property
    def key(self) -> str | None:
        return None if self.heading is None else unit_key(self.heading).casefold()


@dataclass
class Body:
    title_line: str
    sections: list[Section]


def parse_body(body: str, *, layer: str = PUBLIC) -> Body:
    lines = body.strip("\n").split("\n")
    title_line = ""
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start].startswith("# "):
        title_line = lines[start].rstrip()
        start += 1
    sections = [Section(None)]
    current: list[str] = []
    kind = ""
    fenced = False

    def flush():
        nonlocal current, kind
        if current and any(line.strip() for line in current):
            sections[-1].units.append(Unit(kind or "paragraph", "\n".join(current).strip("\n"), layer))
        current, kind = [], ""

    index = start
    while index < len(lines):
        line = lines[index]
        if fenced:
            current.append(line)
            if line.strip().startswith("```"):
                fenced = False
            index += 1
            continue
        if line.startswith(_CALLOUT_START):
            flush()
            block = [line]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                index += 1
                if block[-1].startswith(_CALLOUT_END):
                    break
            sections[-1].units.append(Unit("callout", "\n".join(block), layer))
            continue
        if line.strip().startswith("```"):
            if kind != "item":
                flush()
                kind = "paragraph"
            current.append(line)
            fenced = True
        elif _HEADING_RE.match(line):
            flush()
            sections.append(Section(line.rstrip()))
        elif not line.strip():
            flush()
        elif _ITEM_RE.match(line):
            flush()
            kind = "item"
            current.append(line)
        else:
            if not current:
                kind = "paragraph"
            current.append(line)
        index += 1
    flush()
    return Body(title_line, [section for section in sections if section.heading is not None or section.units])


def render_body(body: Body, *, mark: bool = False) -> str:
    parts = [body.title_line] if body.title_line else []
    for section in body.sections:
        if not section.units and section.heading is not None:
            continue
        chunk = [section.heading] if section.heading is not None else []
        rendered = ""
        previous = None
        for unit in section.units:
            text = _marked(unit) if mark else unit.text
            if previous is None:
                rendered = text
            elif previous.kind == "item" and unit.kind == "item":
                rendered += "\n" + text
            else:
                rendered += "\n\n" + text
            previous = unit
        if rendered:
            chunk.append(rendered)
        if chunk:
            parts.append("\n".join(chunk))
    return "\n\n".join(parts).strip() + "\n" if parts else ""


def _marked(unit: Unit) -> str:
    text = escape_markers(unit.text)
    if unit.layer == PUBLIC or unit.kind == "callout":
        return text
    tag = f"[layer:{unit.layer}] "
    match = _ITEM_RE.match(text)
    if match:
        return text[: match.end()] + tag + text[match.end():]
    return tag + text


def merge_for_writing(public: str, layer_body: str, *, layer: str) -> str:
    """One body the librarian can extend: the public page, then the layer's own units."""
    base = parse_body(public)
    extra = parse_body(layer_body, layer=layer)
    merged = _combine([base, extra], date_order=False)
    return render_body(merged)


def split_from_public(body: str, public: str) -> str:
    """The units of `body` the public page does not already hold; empty when there are none."""
    parsed = parse_body(body)
    known = {unit.key for section in parse_body(public).sections for unit in section.units}
    kept = []
    for section in parsed.sections:
        units = [unit for unit in section.units if unit.key not in known]
        if units:
            kept.append(Section(section.heading, units))
    if not kept:
        return ""
    return render_body(Body(parsed.title_line, kept))


def reading_text(layer: str, body: str) -> str:
    """One stored row's text as a reader sees it: public text with look-alike markers escaped, a
    layer's units marked."""
    return escape_markers(body) if layer == PUBLIC else fuse_for_reading([(layer, body)])


def fuse_for_reading(parts: list[tuple[str, str]]) -> str:
    """One reader-facing body from `(layer, body)` parts, public first; non-public units marked."""
    bodies = [parse_body(body, layer=layer) for layer, body in parts]
    return render_body(_combine(bodies, date_order=True), mark=True)


def _combine(bodies: list[Body], *, date_order: bool) -> Body:
    title = next((body.title_line for body in bodies if body.title_line), "")
    order: list[str | None] = []
    headings: dict[str | None, str | None] = {}
    units: dict[str | None, list[Unit]] = {}
    newest_first: dict[str | None, bool] = {}
    for body in bodies:
        for section in body.sections:
            key = section.key
            if key not in units:
                order.append(key)
                headings[key] = section.heading
                units[key] = []
                # A section keeps the direction its first page wrote its dates in.
                dates = [_leading_date(unit) for unit in section.units]
                newest_first[key] = all(dates) and len(set(dates)) > 1 and dates == sorted(dates, reverse=True)
            units[key].extend(section.units)
    sections = []
    for key in order:
        sections.append(Section(headings[key], _dedupe(units[key], connections=key == "## connections")))
    if date_order:
        for key, section in zip(order, sections, strict=True):
            dates = [_leading_date(unit) for unit in section.units]
            if section.units and all(dates):
                ranked = sorted((date, index, unit)
                                for index, (date, unit) in enumerate(zip(dates, section.units, strict=True)))
                if newest_first[key]:
                    ranked = sorted(ranked, key=lambda item: (item[0], -item[1]), reverse=True)
                section.units = [unit for _date, _index, unit in ranked]
    return Body(title, sections)


def _dedupe(units: list[Unit], *, connections: bool) -> list[Unit]:
    seen: set[str] = set()
    targets: set[str] = set()
    kept = []
    for unit in units:
        if unit.key in seen:
            continue
        if connections and unit.kind == "item":
            match = _WIKILINK_RE.search(unit.text)
            target = match.group(1).strip().casefold() if match else None
            if target is not None and target in targets:
                continue
            if target is not None:
                targets.add(target)
        seen.add(unit.key)
        kept.append(unit)
    return kept


def _leading_date(unit: Unit) -> dt.date | None:
    match = _DATE_RE.match(unit.text)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def fuse_rows(rows: list[dict]) -> dict:
    """One reader-facing record from the visible index rows sharing a virtual path."""
    if not rows:
        raise LayerError("nothing to fuse")
    ordered = sorted(rows, key=lambda row: (row["layer"] != PUBLIC, row["layer"]))
    first = ordered[0]
    fused = dict(first)
    fused["path"] = first["vpath"]
    fused["layers"] = [row["layer"] for row in ordered]
    if len(ordered) == 1:
        if "body" in first:
            fused["body"] = reading_text(first["layer"], first["body"] or "")
        return fused
    if "aliases" in first:
        aliases: dict[str, str] = {}
        for row in ordered:
            for alias in row.get("aliases") or ():
                aliases.setdefault(unit_key(alias).casefold(), alias)
        fused["aliases"] = list(aliases.values())
    if "links" in first:
        fused["links"] = sorted({value for row in ordered for value in row.get("links") or ()})
    if "sources" in first:
        fused["sources"] = list(dict.fromkeys(value for row in ordered for value in row.get("sources") or ()))
    if "source_dates" in first:
        fused["source_dates"] = sorted({day for row in ordered for day in row.get("source_dates") or ()})
    if "updated" in first:
        fused["updated"] = max((row.get("updated") or "" for row in ordered), default="")
    if "entity_type" in first:
        fused["entity_type"] = next((row["entity_type"] for row in ordered if row.get("entity_type")), "")
    if "body" in first:
        fused["body"] = fuse_for_reading([(row["layer"], row.get("body") or "") for row in ordered])
    return fused
