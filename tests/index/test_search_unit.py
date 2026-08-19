"""`search._filter_clause`, offline: no database, no embedder — the SQL fragment and its params
only. `test_pg_search_edges.py`/`test_pg_integration.py` prove the same filters work end to end
against real Postgres; this file targets the pure clause-building logic directly."""
import pytest

from stigmergy.index import search, store


def test_an_empty_query_is_a_repairable_refusal_not_a_provider_crash():
    """OLD BEHAVIOUR: `search_arms(conn, "")` sent the empty string to the embedding PROVIDER,
    whose 400 (OpenAI and OpenRouter both refuse empty input) crashed the whole ask — surfaced
    by the qa golden's first DeepSeek run, where the model called search(""). A ValueError is
    the ask tool's repair channel: the model reads an error string and tries a real query. The
    guard sits before the meta read, so this needs no database and reaches every caller."""
    for empty in ("", "   ", None):
        with pytest.raises(ValueError, match="empty query matches nothing"):
            search.search_arms(None, empty)


def test_no_filters_is_the_empty_clause():
    clause, params = search._filter_clause(None)
    assert clause == "" and params == {}
    clause, params = search._filter_clause({})
    assert clause == "" and params == {}


def test_an_ordinary_scalar_column_is_plain_equality():
    clause, params = search._filter_clause({"zone": "wiki"})
    assert clause == " AND zone = %(filter_zone)s"
    assert params == {"filter_zone": "wiki"}


def test_entity_is_membership_not_equality():
    """`entity` is `text[]` — the ONE column that filters by membership. The public contract is
    unchanged by that: the caller still passes ONE value."""
    clause, params = search._filter_clause({"entity": "stigmergy"})
    assert clause == " AND %(filter_entity)s = ANY(entity)"
    assert params == {"filter_entity": "stigmergy"}


def test_entity_combined_with_an_ordinary_filter_mixes_both_shapes():
    clause, params = search._filter_clause({"entity": "stigmergy", "status": "canonical"})
    assert clause == " AND %(filter_entity)s = ANY(entity) AND status = %(filter_status)s"
    assert params == {"filter_entity": "stigmergy", "filter_status": "canonical"}


def test_an_unknown_filter_column_raises():
    with pytest.raises(ValueError, match="unknown filter column"):
        search._filter_clause({"body": "nope"})


# ── a DSN's host, and NEVER its password ───────────────────────────────────────────────────────
# The keyword form (`host=h port=5432 password=…`) is a DSN too, and string surgery on it returns
# the whole connstring. That mattered the moment a refusal started PRINTING the queue's location:
# this repo has legislated the rule twice already (`tests/testdb.describe`,
# `mcp_server._dsn_location`) and a third hand-rolled parser is how a credential reaches a
# terminal. libpq's own parser is the one that cannot get this wrong.
@pytest.mark.parametrize("conninfo,expected", [
    ("postgresql://u:p@db.abcdef.supabase.co:5432/postgres", "db.abcdef.supabase.co"),
    ("host=db.abcdef.supabase.co port=5432 user=u password=SUPERSECRET dbname=p",
     "db.abcdef.supabase.co"),
    ("postgresql://u:p@localhost:54321/stigmergy?sslmode=require", "localhost"),
    ("postgresql://u:p@h1.example.com:5432,h2.example.com:5432/db", "h1.example.com"),
    ("postgresql:///stigmergy?host=/var/run/postgresql", "/var/run/postgresql"),
    ("", ""),
    ("not a dsn", ""),
    (None, ""),
])
def test_host_of_dsn_reads_every_shape_libpq_accepts(conninfo, expected):
    assert store.host_of_dsn(conninfo) == expected


def test_host_of_dsn_never_returns_a_password():
    """The property, stated as one: whatever the shape, the result may not carry a credential."""
    for conninfo in ("host=h.example.com password=SUPERSECRET dbname=p",
                     "postgresql://user:SUPERSECRET@h.example.com/db",
                     "postgresql://user:SUPER@SECRET@h.example.com/db"):
        assert "supersecret" not in store.host_of_dsn(conninfo).lower()
