"""`evals/run_qa.py`'s Settings construction.

The defect this pins: the runner built `Settings` with no `entity_registry_path`, so every golden
QA run measured a server WITHOUT entity-first resolution while the deployed server has it — the
instrument was structurally blind to a retrieval mechanism it is supposed to guard. Same posture
as `test_run_qa_scoring.py`: the runner has no unit tests, but the small pure pieces where a
yardstick can lie do.
"""
import types

from evals import run_qa


def _args(**over):
    base = {"identities": "evals/qa_identities.json", "entity_registry": None, "repo": None,
            "llm": "fake"}
    base.update(over)
    return types.SimpleNamespace(**base)


def test_the_registry_path_derives_from_repo_like_the_deployed_server():
    settings = run_qa._settings_for(_args(repo="/srv/knowledge"), "steward")
    assert settings.entity_registry_path.replace("\\", "/") == \
        "/srv/knowledge/ops/entity-registry.json"


def test_an_explicit_registry_flag_wins_over_the_repo_convention():
    settings = run_qa._settings_for(
        _args(repo="/srv/knowledge", entity_registry="/tmp/reg.json"), "steward")
    assert settings.entity_registry_path == "/tmp/reg.json"


def test_no_repo_still_means_fail_open_not_a_crash():
    """The frozen corpus ships no `ops/` — a repo-less invocation keeps the loader's documented
    fail-open (empty path -> no aliases) rather than crashing."""
    settings = run_qa._settings_for(_args(), "steward")
    assert settings.entity_registry_path == ""


def test_identity_and_backend_still_travel():
    settings = run_qa._settings_for(_args(llm="openrouter"), "ana")
    assert settings.identity == "ana"
    assert settings.llm == "openrouter"
    assert settings.identities_path == "evals/qa_identities.json"
