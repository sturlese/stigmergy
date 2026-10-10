"""`evals/run_qa.py`'s Settings construction: one site, so what the instrument measures is inspectable."""
import types

from evals import run_qa


def _args(**over):
    base = {"identities": "evals/qa_identities.json", "repo": None, "llm": "fake"}
    base.update(over)
    return types.SimpleNamespace(**base)


def test_the_measured_server_uses_the_golden_identity_and_llm():
    settings = run_qa._settings_for(_args(), "steward")
    assert settings.identity == "steward"
    assert settings.llm == "fake"
