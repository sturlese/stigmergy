from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from stigmergy.capture.schema import AcquisitionProvenance
from stigmergy.entities.model import (
    REGISTRY_PATH,
    EntityContractError,
    load_entities,
    parse_entity,
    registry_bytes,
)
from stigmergy.index.corpus import link_targets, split_frontmatter_checked
from stigmergy.kernel.acl import flows_into
from stigmergy.knowledge.contradictions import ContradictionContractError, parse_all
from stigmergy.knowledge.pages import PageContractError, parse_page
from stigmergy.server.controls import PATHS as CONTROL_PATHS
from stigmergy.server.controls import ControlError, validate_root

_SOURCE_PATH_RE = re.compile(
    r"^sources/(?P<year>\d{4})/(?P<month>0[1-9]|1[0-2])/"
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.md$"
)


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class CorpusPage:
    path: str
    page_type: str
    acl: tuple[str, ...] | None
    entities: tuple[str, ...]
    sources: tuple[str, ...]
    text: str


def check(root: str) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    pages: dict[str, CorpusPage] = {}
    ids: dict[str, str] = {}
    records = {}

    for path in _markdown_paths(root):
        text = Path(root, *path.split("/")).read_text(encoding="utf-8")
        try:
            page = _parse_corpus_page(path, text)
        except (PageContractError, EntityContractError, ValueError) as error:
            violations.append(Violation(path, "page-contract", str(error)))
            continue
        pages[path] = page
        metadata, _body, _malformed = split_frontmatter_checked(text)
        page_id = str(metadata.get("id") or "")
        if page_id:
            if page_id in ids:
                violations.append(Violation(path, "duplicate-id", f"page id is also used by {ids[page_id]}"))
            ids[page_id] = path

    try:
        records = load_entities(root)
    except EntityContractError as error:
        violations.append(Violation("wiki/entities", "entity-contract", str(error)))

    violations.extend(_registry_violations(root, records))
    violations.extend(_relationship_violations(pages, records))
    if any(
        Path(root, *path.split("/")).exists()
        for path in CONTROL_PATHS
        if path != REGISTRY_PATH
    ):
        try:
            validate_root(root)
        except ControlError as error:
            violations.append(Violation("ops", "control-contract", str(error)))
    return tuple(sorted(set(violations)))


def _markdown_paths(root: str) -> list[str]:
    paths = []
    for zone in ("wiki", "sources"):
        folder = Path(root, zone)
        if not folder.is_dir():
            continue
        paths.extend(path.relative_to(root).as_posix() for path in folder.rglob("*.md"))
    return sorted(paths)


def _parse_corpus_page(path: str, text: str) -> CorpusPage:
    if path.startswith(("wiki/notes/", "wiki/concepts/")):
        page = parse_page(path, text)
        return CorpusPage(
            path=path,
            page_type=page.role,
            acl=page.acl,
            entities=page.entities,
            sources=page.sources,
            text=text,
        )
    if path.startswith("wiki/entities/"):
        parse_entity(path, text)
        return CorpusPage(path, "entity", None, (), (), text)
    if path.startswith("sources/"):
        return _parse_source(path, text)
    raise PageContractError("Markdown is outside an allowed corpus folder")


def _parse_source(path: str, text: str) -> CorpusPage:
    match = _SOURCE_PATH_RE.fullmatch(path)
    metadata, body, malformed = split_frontmatter_checked(text)
    if not match or malformed or metadata.get("type") != "source":
        raise PageContractError("source path or frontmatter is invalid")
    if str(metadata.get("id") or "") != match.group("id") or not body.strip():
        raise PageContractError("source id or body is invalid")
    captured_at = dt.datetime.fromisoformat(str(metadata.get("captured_at")).replace("Z", "+00:00"))
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise PageContractError("source captured_at must include a timezone")
    captured_at = captured_at.astimezone(dt.UTC)
    if captured_at.year != int(match.group("year")) or captured_at.month != int(match.group("month")):
        raise PageContractError("source path does not match captured_at")
    artifacts = metadata.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise PageContractError("source artifacts must be a non-empty list")
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not {
            "sha256",
            "bytes",
            "media_type",
            "readable_sha256",
            "extractor",
            "extractor_version",
        } <= set(artifact):
            raise PageContractError("source artifact metadata is incomplete")
    acquisition = metadata.get("acquisition")
    if acquisition is not None:
        try:
            AcquisitionProvenance.model_validate(acquisition)
        except ValueError as error:
            raise PageContractError("source acquisition provenance is invalid") from error
    acl = _acl(metadata.get("acl"))
    return CorpusPage(path, "source", acl, (), (), text)


def _registry_violations(root: str, records) -> list[Violation]:
    path = Path(root, *REGISTRY_PATH.split("/"))
    expected = registry_bytes(records)
    if not path.is_file():
        return [Violation(REGISTRY_PATH, "registry-missing", "entity registry is missing")]
    try:
        actual = path.read_bytes()
    except OSError as error:
        return [Violation(REGISTRY_PATH, "registry-unreadable", error.__class__.__name__)]
    if actual != expected:
        return [
            Violation(
                REGISTRY_PATH,
                "registry-drift",
                "entity registry is not the deterministic projection of entity pages",
            )
        ]
    return []


def _relationship_violations(pages, records) -> list[Violation]:
    violations = []
    contradiction_ids: dict[str, str] = {}
    by_stem: dict[str, list[str]] = {}
    for path in pages:
        by_stem.setdefault(Path(path).stem.casefold(), []).append(path)

    for record in records.values():
        for claim in (*record.claims, *record.external_ids):
            source = pages.get(claim.source)
            if source is None or source.page_type != "source":
                violations.append(
                    Violation(
                        record.path,
                        "entity-claim-source",
                        f"entity claim source {claim.source} does not exist",
                    )
                )
            elif not flows_into(_list(source.acl), _list(claim.acl)):
                violations.append(
                    Violation(
                        record.path,
                        "acl-entity-claim-leak",
                        f"entity claim source {claim.source} crosses visibility",
                    )
                )

    for path, page in pages.items():
        if page.page_type in {"entity", "source"}:
            continue
        for target in link_targets(page.text):
            matches = by_stem.get(target.casefold(), [])
            if len(matches) != 1:
                code = "dead-link" if not matches else "ambiguous-link"
                violations.append(Violation(path, code, f"wikilink {target!r} resolves {len(matches)} times"))
                continue
            target_page = pages[matches[0]]
            if target_page.page_type != "entity" and not flows_into(_list(target_page.acl), _list(page.acl)):
                violations.append(Violation(path, "acl-link-leak", f"link to {target_page.path} crosses visibility"))
        for source in page.sources:
            target = pages.get(source)
            if target is None or target.page_type != "source":
                violations.append(Violation(path, "missing-source", f"source {source!r} does not exist"))
            elif not flows_into(_list(target.acl), _list(page.acl)):
                violations.append(Violation(path, "acl-source-leak", f"source {source} crosses visibility"))
        for entity_id in page.entities:
            record = records.get(entity_id)
            if record is None:
                canonical = next(
                    (candidate for candidate, item in records.items() if entity_id in item.absorbed_ids),
                    None,
                )
                code = "absorbed-anchor" if canonical else "unknown-entity"
                message = (
                    f"entity anchor {entity_id} must be {canonical}"
                    if canonical
                    else f"entity {entity_id} does not exist"
                )
                violations.append(Violation(path, code, message))
                continue
            if not any(flows_into(_list(claim.acl), _list(page.acl)) for claim in record.claims):
                violations.append(
                    Violation(
                        path,
                        "invisible-entity-name",
                        f"entity {entity_id} has no name visible to every page reader",
                    )
                )
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
                source = pages.get(claim.source)
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


def _acl(value) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise PageContractError("acl is invalid")
    if len(value) != len(set(value)):
        raise PageContractError("acl contains duplicates")
    return tuple(value)


def _list(value: tuple[str, ...] | None) -> list[str] | None:
    return None if value is None else list(value)
