import json

from evals import run_aliases
from stigmergy.kernel import llm
from stigmergy.knowledge import aliases

GOLDEN = json.loads(run_aliases.GOLDEN.read_text(encoding="utf-8"))


def test_the_calibration_set_holds_both_classes_with_their_context():
    cases = GOLDEN["cases"]
    assert len(cases) >= 30
    assert sum(case["expect"] == "alias" for case in cases) >= 12
    assert sum(case["expect"] == "other" for case in cases) >= 12
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert case["expect"] in ("alias", "other"), case["id"]
        assert case["entity"]["title"] and case["entity"]["type"] and case["entity"]["summary"], case["id"]
        assert case["candidate"] and case["candidate"].casefold() in case["sentence"].casefold(), case["id"]


def _by_candidate(state):
    return next(case for case in GOLDEN["cases"]
                if case["candidate"] == state["candidate"] and case["sentence"] == state["sentence"])


def test_the_bar_fails_on_one_accepted_non_alias_and_on_any_classifier_failure():
    def oracle(options, state):
        alias = 0.95 if _by_candidate(state)["expect"] == "alias" else 0.05
        return {aliases.ALIAS: alias, aliases.OTHER: 1 - alias}

    with llm.decision_override(oracle):
        report = run_aliases.run(GOLDEN)
    assert report["bar_passed"] and report["by_threshold"][str(aliases.THRESHOLD)]["recall"] == 1.0

    def credulous(options, state):
        return {aliases.ALIAS: 0.99, aliases.OTHER: 0.01}

    with llm.decision_override(credulous):
        report = run_aliases.run(GOLDEN)
    assert not report["bar_passed"]
    assert len(report["by_threshold"][str(aliases.THRESHOLD)]["non_aliases_accepted"]) >= 12

    def failing(options, state):
        raise llm.DecisionFailed("timeout")

    with llm.decision_override(failing):
        report = run_aliases.run(GOLDEN)
    assert not report["bar_passed"] and len(report["classifier_failures"]) == len(GOLDEN["cases"])
