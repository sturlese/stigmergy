import json

from evals import run_routing
from stigmergy.kernel import llm

GOLDEN = json.loads(run_routing.GOLDEN.read_text(encoding="utf-8"))


def test_the_calibration_set_is_large_enough_and_only_offers_layers_a_sender_could_name():
    cases = GOLDEN["cases"]
    assert len(cases) >= 30
    assert sum(case["expect"] == "default" for case in cases) >= 8
    assert len({case["id"] for case in cases}) == len(cases)
    assert "public" not in GOLDEN["layers"]
    for case in cases:
        assert len(case["candidates"]) >= 2, case["id"]
        assert set(case["candidates"]) <= set(GOLDEN["layers"]), case["id"]
        assert case["expect"] == "default" or case["expect"] in case["candidates"], case["id"]
        assert case.get("title") or case.get("text") or case.get("files"), case["id"]


def test_the_bar_counts_expected_layers_and_defaults_but_never_a_classifier_failure():
    def oracle(options, state):
        case = next(c for c in GOLDEN["cases"] if routing_state(c) == state)
        if case["expect"] == "default":
            return {name: 1.0 if name == run_routing.routing.NO_SINGLE_LAYER else 0.0 for name in options}
        return {name: 0.9 if name == case["expect"] else 0.1 / (len(options) - 1) for name in options}

    with llm.decision_override(oracle):
        report = run_routing.run(GOLDEN)
    assert report["bar_passed"] and report["complete"]

    def failing(options, state):
        raise llm.DecisionFailed("timeout")

    with llm.decision_override(failing):
        report = run_routing.run(GOLDEN)
    assert not report["bar_passed"]
    assert report["ambiguous_passed"] == 0
    assert report["classifier_failures"] == len(GOLDEN["cases"])


def routing_state(case):
    return run_routing.routing.capture_state(
        title=case.get("title"), files=case.get("files", ()), text=case.get("text", ""))
