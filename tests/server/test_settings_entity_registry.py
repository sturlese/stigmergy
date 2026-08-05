"""`Settings.from_args`'s `entity_registry_path`: an EXPLICIT `--entity-registry` path
wins over the `--repo` convention — the same precedence `identities_path` already has,
and load-bearing for the same reason: the deployed server passes NO `--repo` at all (`fly.toml`'s
`[processes].app`/`slack` commands), so `--repo`-derivation alone would leave entity-first
resolution permanently inert in production.
"""
import argparse

from stigmergy.server.settings import Settings


def _args(**overrides) -> argparse.Namespace:
    base = {"identity": None, "identities": None, "repo": None,
           "entity_registry": None, "dsn": None, "embedder": None, "answer_llm": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_explicit_entity_registry_flag_wins_over_repo_derivation():
    settings = Settings.from_args(_args(repo="../stigmergy-brain", entity_registry="/app/entity-registry.json"))
    assert settings.entity_registry_path == "/app/entity-registry.json"


def test_falls_back_to_repo_derivation_when_no_explicit_flag_given():
    settings = Settings.from_args(_args(repo="../stigmergy-brain"))
    assert settings.entity_registry_path == "../stigmergy-brain/ops/entity-registry.json"


def test_empty_string_when_neither_flag_nor_repo_is_given():
    """The production deployment shape: no `--repo`, no `--entity-registry` yet — entity-first
    resolution is inert (`entity_aliases.load_aliases("")` returns `{}`), never a crash."""
    settings = Settings.from_args(_args())
    assert settings.entity_registry_path == ""
