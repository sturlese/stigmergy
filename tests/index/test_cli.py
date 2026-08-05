"""CLI argument handling — the offline half (end-to-end CLI runs live in the pg suite)."""
import pytest

from stigmergy.index import cli


def test_parse_filters_key_value_pairs():
    assert cli._parse_filters(["entity=globex", "type=report"]) == \
        {"entity": "globex", "type": "report"}
    assert cli._parse_filters(None) == {}


def test_parse_filters_rejects_malformed_pairs():
    with pytest.raises(SystemExit):
        cli._parse_filters(["entityglobex"])


def test_index_main_requires_rebuild_and_repo(capsys):
    with pytest.raises(SystemExit):
        cli.index_main(["--repo", "somewhere"])          # missing --rebuild
    with pytest.raises(SystemExit):
        cli.index_main(["--rebuild"])                    # missing --repo


def test_render_hit_shows_factors_and_arms():
    out = cli._render_hit(1, {"title": "T", "path": "a.md", "zone": "sources",
                              "score": 0.0123, "arms": ["fts", "vec"],
                              "factors": ["superseded", "entity:globex"],
                              "snippet": "the snippet"})
    assert "superseded, entity:globex" in out
    assert "fts+vec" in out
    assert "the snippet" in out


def test_render_hit_without_factors_says_none():
    out = cli._render_hit(2, {"title": "T", "path": "a.md", "zone": "wiki",
                              "score": 1.0, "arms": ["vec"], "factors": [], "snippet": ""})
    assert "factors: none" in out
