"""Entity presentation must not aggregate unbounded page metadata in Python."""

from stigmergy.index import search
from stigmergy.server.service import (
    ENTITY_RELATIONSHIP_CAP,
    ENTITY_SOURCE_CAP,
    BrainService,
)


class _CappedIterable:
    def __init__(self, prefix: str, limit: int):
        self.prefix = prefix
        self.limit = limit

    def __iter__(self):
        for index in range(self.limit):
            yield f"{self.prefix}{index}.md"
        raise AssertionError("entity evidence aggregation exceeded cap + 1")


def test_timeline_stops_source_and_link_aggregation_at_cap_plus_one(monkeypatch):
    page = {
        "path": "wiki/concepts/Evidence bounds.md",
        "title": "Evidence bounds",
        "type": "concept",
        "status": "developing",
        "updated": "2026-09-12",
        "body": "# Evidence bounds\n\nBounded evidence is intentional.",
        "sources": _CappedIterable(
            "sources/2026/09/00000000-0000-4000-8000-", ENTITY_SOURCE_CAP + 1
        ),
        "links": _CappedIterable("wiki/concepts/Linked ", ENTITY_RELATIONSHIP_CAP + 1),
    }
    observed = []

    monkeypatch.setattr(search, "entity_timeline", lambda *_args, **_kwargs: ([page], False))

    def fetch(_conn, paths, **_kwargs):
        observed.append(list(paths))
        return {}, False

    monkeypatch.setattr(search, "fetch_visible_pages_limited", fetch)
    service = object.__new__(BrainService)
    service.conn = object()
    service.audiences = None

    _items, _note, sources, _knowledge_truncated, sources_truncated = service._timeline_section("ent_test")

    assert len(sources) == ENTITY_SOURCE_CAP + 1
    assert sources_truncated is True
    assert observed[0] == [
        f"wiki/concepts/Linked {index}.md" for index in range(ENTITY_RELATIONSHIP_CAP + 1)
    ]


def test_entity_sources_preserve_prior_aggregation_truncation(monkeypatch):
    monkeypatch.setattr(search, "bounded_unique_paths", lambda *_args, **_kwargs: ([], False))
    monkeypatch.setattr(
        search, "fetch_visible_pages_limited", lambda *_args, **_kwargs: ({}, False)
    )
    service = object.__new__(BrainService)
    service.conn = object()
    service.audiences = None

    sources, truncated = service._entity_sources([], candidates_truncated=True)

    assert sources == []
    assert truncated is True
