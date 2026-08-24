import json
from pathlib import Path


def write_controls(repo: Path) -> None:
    values = {
        "entity-registry.json": {"version": 1, "entities": {}, "redirects": {}},
        "identities.json": {
            "test-master": {
                "display_name": "Test Master",
                "groups": ["brain-admins"],
                "default_audience": None,
            }
        },
        "slack-channels.json": {},
    }
    ops = repo / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        path = ops / name
        if not path.exists():
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
