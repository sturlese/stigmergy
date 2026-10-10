"""`evals/run_qa.py` registers the layers its identities name in the rebuilt database's roster."""
import json

from evals import run_qa
from stigmergy.server import roster
from stigmergy.server.identity import UNRESTRICTED_GROUP
from tests.roster import reset_roster
from tests.server.conftest import connect_or_skip


def test_the_identities_layers_are_registered_once_per_workspace(tmp_path):
    identities = tmp_path / "identities.json"
    reader = {"display_name": "Reader", "groups": ["finance"], "default_audience": ["finance"]}
    identities.write_text(json.dumps({"reader": reader}), encoding="utf-8")
    conn = connect_or_skip()
    try:
        reset_roster(conn)
        run_qa._ensure_layers(conn, str(identities))
        run_qa._ensure_layers(conn, str(identities))
        assert [group["name"] for group in roster.list_groups(conn)
                if group["name"] != UNRESTRICTED_GROUP] == ["finance"]
    finally:
        reset_roster(conn)
        conn.close()
