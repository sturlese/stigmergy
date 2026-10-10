#!/usr/bin/env python3
"""Measure case-independent editorial checks against the case-bound evaluation gates.

The evaluation's editorial gates know each case's required entities and redundant titles. A check
the writer could run in production knows neither, so this tool scores candidate checks on recorded
production-equivalent runs, with the evaluation gates as ground truth, and optionally counts how
many existing entity pages of a knowledge tree each check would flag.

  python evals/filing/calibrate_editorial.py --records '<dir>/**/run*/*.json' [--tree <knowledge checkout>]
"""
import argparse
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stigmergy.index import corpus  # noqa: E402
from stigmergy.kernel.normalize import resolution_key  # noqa: E402

_WORD = re.compile(r"\b[\w'-]+\b")
_BULLET = re.compile(r"^[-*+]\s+")


def entity_sections(body: str) -> tuple[str, tuple[str, ...]]:
    """The first `What / Who` paragraph and the `Facts` bullets of an entity page body."""
    description, facts, section = "", [], ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().casefold()
            section = ("facts" if heading == "facts"
                       else "description" if heading in {"what / who", "who / what"} else "")
            continue
        cleaned = re.sub(r"\(Source:\s*`[^`]+`\)", "", stripped).strip()
        if not cleaned:
            continue
        if section == "description" and not description and not _BULLET.match(cleaned):
            description = cleaned
        elif section == "facts" and _BULLET.match(cleaned):
            facts.append(_BULLET.sub("", cleaned))
    return description, tuple(facts)


def entity_without_facts(body: str) -> bool:
    return not entity_sections(body)[1]


def entity_without_identity(body: str, min_words: int = 3) -> bool:
    return len(_WORD.findall(entity_sections(body)[0])) < min_words


def restated_titles(mutations: list[dict]) -> list[tuple[str, str]]:
    """Pairs of notes or concepts created together where one title contains the other."""
    created = [resolution_key(m.get("title") or "") for m in mutations
               if m.get("action") == "create" and m.get("role") in {"note", "concept"}]
    return [(a, b) for i, a in enumerate(created) for b in created[i + 1:]
            if a and b and (f" {a} " in f" {b} " or f" {b} " in f" {a} ")]


def checks(plan: dict) -> dict[str, bool]:
    mutations = plan.get("mutations") or []
    created_entities = [m for m in mutations if m.get("action") == "create" and m.get("role") == "entity"]
    return {
        "entity_without_facts": any(entity_without_facts(m.get("body") or "") for m in created_entities),
        "entity_without_identity": any(entity_without_identity(m.get("body") or "") for m in created_entities),
        "restated_titles": bool(restated_titles(mutations)),
        "empty_plan": not mutations,
    }


# Which evaluation gate each check stands in for.
GROUND_TRUTH = {
    "entity_without_facts": "entity_editorial_quality",
    "entity_without_identity": "entity_editorial_quality",
    "restated_titles": "anti_fragmentation",
    "empty_plan": "mutations",
}


def score_records(paths: list[str]) -> dict:
    table = {name: {"flagged": 0, "true": 0, "false": 0, "missed": 0} for name in GROUND_TRUTH}
    hosts: dict[str, dict[str, int]] = {}
    runs = failing = 0
    for path in paths:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        evidence = record["case_result"]["evidence"]
        gates = evidence["result"]["raw_gates"]
        runs += 1
        failing += not all(gates.values())
        # Records made before the host was recorded count under "unrecorded".
        telemetry = record.get("telemetry") or {}
        served = {response.get("upstream") or "unrecorded"
                  for phase in telemetry.values() if phase for response in phase.get("responses", ())}
        for host in served or {"unrecorded"}:
            row = hosts.setdefault(host, {"runs": 0, "failing": 0})
            row["runs"] += 1
            row["failing"] += not all(gates.values())
        for name, flagged in checks(evidence["plans"]["effective"]).items():
            failed = gates.get(GROUND_TRUTH[name]) is False
            row = table[name]
            row["flagged"] += flagged
            row["true"] += flagged and failed
            row["false"] += flagged and not failed
            row["missed"] += failed and not flagged
    return {"runs": runs, "failing_runs": failing, "checks": table, "hosts": hosts}


def score_tree(tree: str) -> dict:
    entities = [row for row in corpus.load_pages(tree) if row.zone == "wiki" and row.type == "entity"]
    return {
        "entity_pages": len(entities),
        "entity_without_facts": sum(entity_without_facts(row.body) for row in entities),
        "entity_without_identity": sum(entity_without_identity(row.body) for row in entities),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", required=True, help="glob of recorded run_planner case records")
    parser.add_argument("--tree", help="knowledge checkout whose existing entity pages are also counted")
    args = parser.parse_args()
    report = score_records(sorted(glob.glob(args.records, recursive=True)))
    if args.tree:
        report["tree"] = score_tree(args.tree)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
