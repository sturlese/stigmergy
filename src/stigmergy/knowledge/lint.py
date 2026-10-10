from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from stigmergy.index.corpus import link_targets, split_frontmatter_checked
from stigmergy.kernel import layers
from stigmergy.kernel.acl import flows_into
from stigmergy.knowledge.contradictions import ContradictionContractError, parse_all
from stigmergy.knowledge.pages import PageContractError, parse_page, path_role
from stigmergy.knowledge.sources import SourceContractError, parse_source


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class CorpusPage:
    path: str
    page_type: str
    title: str
    acl: tuple[str, ...] | None
    sources: tuple[str, ...]
    text: str
    name_keys: tuple[str, ...] = ()
    layer: str = layers.PUBLIC
    vpath: str = ""


def check(root: str, *, editorial_paths: frozenset[str] = frozenset()) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    pages: dict[str, CorpusPage] = {}
    ids: dict[str, str] = {}

    for path in _markdown_paths(root):
        text = Path(root, *path.split("/")).read_text(encoding="utf-8")
        try:
            page = _parse_corpus_page(path, text)
        except (PageContractError, SourceContractError, ValueError) as error:
            violations.append(Violation(path, "page-contract", str(error)))
            continue
        pages[path] = page
        metadata, _body, _malformed = split_frontmatter_checked(text)
        page_id = str(metadata.get("id") or "")
        if page_id:
            if page_id in ids:
                violations.append(Violation(path, "duplicate-id", f"page id is also used by {ids[page_id]}"))
            ids[page_id] = path

    violations.extend(_name_collisions(pages))
    violations.extend(_relationship_violations(pages))
    if editorial_paths:
        violations.extend(_editorial_violations(pages, editorial_paths))
    return tuple(sorted(set(violations)))


def _markdown_paths(root: str) -> list[str]:
    paths = []
    folders = [Path(root, zone) for zone in ("wiki", "sources")]
    layer_root = Path(root, layers.LAYER_ROOT)
    if layer_root.is_dir():
        for layer_folder in sorted(layer_root.iterdir()):
            folders.extend(layer_folder / zone for zone in ("wiki", "sources"))
    for folder in folders:
        if not folder.is_dir():
            continue
        paths.extend(path.relative_to(root).as_posix() for path in folder.rglob("*.md"))
    return sorted(paths)


def _parse_corpus_page(path: str, text: str) -> CorpusPage:
    try:
        layer, vpath = layers.split_path(path)
    except layers.LayerError as error:
        raise PageContractError("page lives outside a valid layer folder") from error
    if path_role(vpath):
        page = parse_page(vpath, text)
        corpus_page = CorpusPage(
            path=path,
            page_type=page.role,
            title=page.title,
            acl=page.acl,
            sources=page.sources,
            text=text,
            name_keys=page.name_keys() if page.role == "entity" else (),
            layer=layer,
            vpath=vpath,
        )
    elif vpath.startswith("sources/"):
        source = parse_source(vpath, text)
        corpus_page = CorpusPage(path, "source", "", source.acl, (), source.text, layer=layer, vpath=vpath)
    else:
        raise PageContractError("Markdown is outside an allowed corpus folder")
    if corpus_page.acl != layers.acl_of_layer(layer):
        raise PageContractError("page audience does not match its layer folder")
    return corpus_page


def _name_collisions(pages) -> list[Violation]:
    """Two entity pages of one layer may never answer to the same normalized title or alias."""
    claimed: dict[tuple[str, str], list[str]] = {}
    for path, page in pages.items():
        for key in page.name_keys:
            claimed.setdefault((page.layer, key), []).append(path)
    return [
        Violation(path, "alias-collision", f"entity name {key!r} is also claimed by {other}")
        for (_layer, key), paths in claimed.items()
        if len(paths) > 1
        for path in paths
        for other in paths
        if other != path
    ]


def _relationship_violations(pages) -> list[Violation]:
    """A page resolves links and sources among its own layer and `public`; one virtual path
    present in both is one target."""
    violations = []
    contradiction_ids: dict[str, str] = {}
    by_stem: dict[tuple[str, str], list[CorpusPage]] = {}
    by_vpath: dict[tuple[str, str], CorpusPage] = {}
    for page in pages.values():
        by_stem.setdefault((page.layer, Path(page.vpath).stem.casefold()), []).append(page)
        by_vpath[(page.layer, page.vpath)] = page

    def scope(page):
        return dict.fromkeys((page.layer, layers.PUBLIC))

    def resolve_source(page, vpath):
        return next((by_vpath[(layer, vpath)] for layer in scope(page) if (layer, vpath) in by_vpath), None)

    for path, page in pages.items():
        if page.page_type == "source":
            continue
        for target in link_targets(page.text):
            per_layer = [by_stem.get((layer, target.casefold()), []) for layer in scope(page)]
            matches = [match for found in per_layer for match in found]
            if not matches or any(len({match.vpath for match in found}) > 1 for found in per_layer):
                code = "dead-link" if not matches else "ambiguous-link"
                violations.append(Violation(path, code, f"wikilink {target!r} resolves {len(matches)} times"))
                continue
            if len({match.vpath for match in matches}) > 1:
                # Two layers use the name for different pages: the link reads as plain text there.
                continue
            for target_page in matches:
                if not flows_into(_list(target_page.acl), _list(page.acl)):
                    violations.append(
                        Violation(path, "acl-link-leak", f"link to {target_page.path} crosses visibility"))
        for source in page.sources:
            target = resolve_source(page, source)
            if target is None or target.page_type != "source":
                violations.append(Violation(path, "missing-source", f"source {source!r} does not exist"))
            elif not flows_into(_list(target.acl), _list(page.acl)):
                violations.append(Violation(path, "acl-source-leak", f"source {source} crosses visibility"))
        try:
            contradictions = parse_all(page.text)
        except ContradictionContractError as error:
            violations.append(Violation(path, "contradiction-contract", str(error)))
            continue
        for located in contradictions:
            previous = contradiction_ids.get(located.record.contradiction_id)
            if previous is not None:
                violations.append(
                    Violation(
                        path,
                        "duplicate-contradiction",
                        f"contradiction id is also used by {previous}",
                    )
                )
            else:
                contradiction_ids[located.record.contradiction_id] = path
            for claim in located.record.claims:
                source = resolve_source(page, claim.source)
                if source is None or source.page_type != "source":
                    violations.append(
                        Violation(path, "contradiction-source", f"contradiction source {claim.source} does not exist")
                    )
                elif not flows_into(_list(source.acl), _list(page.acl)):
                    violations.append(
                        Violation(
                            path,
                            "acl-contradiction-leak",
                            f"contradiction source {claim.source} crosses visibility",
                        )
                    )
    return violations


_SOURCE_REFERENCE = re.compile(r"sources/\d{4}/\d{2}/[^\s`\])>]+\.md")
_PLACEHOLDER = re.compile(r"\b(?:todo|tbd|placeholder|to be written|coming soon)\b", re.I)


def _editorial_violations(pages, editorial_paths: frozenset[str]) -> list[Violation]:
    """Check objective reader-facing minimums only for pages changed by this candidate write."""
    violations: list[Violation] = []
    for path in sorted(editorial_paths):
        page = pages.get(path)
        if page is None or page.page_type == "source":
            continue
        metadata, body, malformed = split_frontmatter_checked(page.text)
        if malformed or not metadata:
            continue
        body_lines = [line.strip() for line in body.splitlines() if line.strip()]
        heading = f"# {page.title}"
        if not body_lines or body_lines[0] != heading:
            violations.append(Violation(path, "heading-title", "knowledge H1 must match page title"))
            continue
        prose = " ".join(body_lines[1:]).strip()
        normalized = re.sub(r"\s+", " ", prose).strip()
        if not normalized:
            violations.append(Violation(path, "empty-knowledge-body", "knowledge body has no prose after its H1"))
        elif _PLACEHOLDER.search(normalized) or len(re.sub(r"[^A-Za-z0-9]", "", normalized)) < 32:
            violations.append(Violation(path, "placeholder-knowledge-body", "knowledge body is effectively empty"))

        citations = set(_SOURCE_REFERENCE.findall(body))
        if page.sources and not citations.intersection(page.sources):
            violations.append(Violation(path, "missing-local-source-attribution",
                                        "knowledge body must cite a declared source locally"))
        for citation in sorted(citations - set(page.sources)):
            violations.append(Violation(path, "invalid-local-source-reference",
                                        f"local source reference {citation!r} is not declared"))

    return violations


def _list(value: tuple[str, ...] | None) -> list[str] | None:
    return None if value is None else list(value)


def main(argv=None) -> int:
    import argparse
    import json
    from dataclasses import asdict

    parser = argparse.ArgumentParser(prog="python -m stigmergy.knowledge.lint")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    violations = check(args.repo)
    if args.json:
        print(json.dumps([asdict(item) for item in violations], indent=2, sort_keys=True))
    else:
        for item in violations:
            print(f"{item.path}: {item.code}: {item.message}")
    return 1 if args.strict and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
