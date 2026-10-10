import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals" / "filing"))

import calibrate_editorial  # noqa: E402

FACTLESS = ("# Codex\n\n## What / Who\nCodex is a coding agent, cited as an example.\n\n"
            "## Connections\n- [[Agent Harness]]")
FACTFUL = ("# Robin Hale\n\n## What / Who\nRobin Hale writes about agent harnesses.\n\n"
           "## Facts\n- 2026-09-12: Explained harnesses.")


def _record(tmp_path, name, mutations, gates, upstream=None):
    record = {"case_result": {"evidence": {"plans": {"effective": {"mutations": mutations}},
                                           "result": {"raw_gates": gates}}}}
    if upstream:
        record["telemetry"] = {"planning": {"responses": [{"upstream": upstream}]}, "correction": None}
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


def _entity(title, body):
    return {"action": "create", "role": "entity", "title": title, "body": body}


def test_checks_are_scored_against_the_evaluation_gates(tmp_path):
    paths = [
        _record(tmp_path, "caught", [_entity("Robin Hale", FACTLESS)], {"entity_editorial_quality": False}, "Together"),
        _record(tmp_path, "extra", [_entity("Codex", FACTLESS)], {"entity_editorial_quality": True}, "Together"),
        _record(tmp_path, "clean", [_entity("Robin Hale", FACTFUL)], {"entity_editorial_quality": True}),
        _record(tmp_path, "empty", [], {"entity_editorial_quality": False, "mutations": False}),
    ]

    report = calibrate_editorial.score_records(paths)

    assert report["checks"]["entity_without_facts"] == {"flagged": 2, "true": 1, "false": 1, "missed": 1}
    assert report["checks"]["empty_plan"] == {"flagged": 1, "true": 1, "false": 0, "missed": 0}
    assert report["hosts"] == {"Together": {"runs": 2, "failing": 1}, "unrecorded": {"runs": 2, "failing": 1}}


def test_titles_that_contain_each_other_are_paired():
    mutations = [{"action": "create", "role": "note", "title": "Harness Engineering Explained"},
                 {"action": "create", "role": "concept", "title": "Harness Engineering"},
                 {"action": "create", "role": "concept", "title": "Agent Harness"}]

    assert calibrate_editorial.restated_titles(mutations) == [("harness engineering explained", "harness engineering")]
