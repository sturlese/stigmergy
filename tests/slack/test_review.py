"""The Slack review surface — buttons on a doorbell card that call `review_decide`, and the merge
modal an identity proposal's "Merge into…" opens instead of firing directly. The bot enforces
nothing: it only resolves who is asking and calls `review_decide`.

Real git + real Postgres: a decision that lands is a real commit on a real bare remote, through
the same governed door every other transport uses.
"""
import asyncio
import json
import os

import pytest

from stigmergy.capture import schema as capture_schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.entities import generator as entities_generator
from stigmergy.entities import remote as entities_remote
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.librarian import gitcmd, githubapp
from stigmergy.review_kinds import KIND_ALIAS_PROPOSAL, KIND_IDENTITY_PROPOSAL
from stigmergy.server import entity_aliases
from stigmergy.server import review as server_review
from stigmergy.server.settings import Settings
from stigmergy.slack import copy, render, review
from stigmergy.slack.context import SlackContext
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.settings import SlackSettings
from tests import testdb
from tests.librarian import support

pytestmark = [pytest.mark.usefixtures("no_real_github_app"), pytest.mark.timeout(60)]

TEAM_ID = "T_STIGMERGY"
STEWARD = "steward@example.com"
STEWARD_SLACK_ID = "U_STEWARD"
ALICE = "alice@example.com"
ALICE_SLACK_ID = "U_ALICE"


@pytest.fixture(autouse=True)
def no_real_github_app(monkeypatch):
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV,
                githubapp.PRIVATE_KEY_ENV, githubapp.PRIVATE_KEY_FILE_ENV):
        monkeypatch.delenv(name, raising=False)


def connect_or_skip():
    conn = testdb.connect_or_skip("review")
    capture_schema.ensure_capture_schema(conn)
    server_review.ensure_review_schema(conn)
    return conn


@pytest.fixture()
def conn():
    c = connect_or_skip()
    with c.cursor() as cur:
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM review_decisions")
    index_store.clear_ops_file(c, index_store.ENTITY_REGISTRY_RELPATH)
    yield c
    index_store.clear_ops_file(c, index_store.ENTITY_REGISTRY_RELPATH)
    c.close()


@pytest.fixture()
def require_gitleaks():
    if support.gitleaks_available():
        return
    pytest.skip("gitleaks not on PATH (brew install gitleaks) — the repo fixtures need it")


def _seed_stewards(env, mapping: dict) -> None:
    path = os.path.join(env.repo, "ops", "stewards.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(mapping))
    support.commit_and_push(env.repo, "test: seed ops/stewards.json")


@pytest.fixture()
def env(tmp_path, require_gitleaks):
    repo_env = support.build_repo(str(tmp_path))
    _seed_stewards(repo_env, {"*": [STEWARD]})
    return repo_env


def _write_identities(env) -> str:
    path = os.path.join(env.repo, "ops", "identities.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({STEWARD: "*", ALICE: "*"}, f)
    return path


def make_ctx(env, conn, *, gateway=None, librarian_repo_url: str | None = None) -> SlackContext:
    """`librarian_repo_url` defaults to `env.bare` — the local bare remote `env.repo` is a clone
    of, not `https://`, so `entities.remote.decide_via_clone` needs no GitHub App credential. Pass
    `""` for a test that wants the capability refusal. The service reads the registry off the
    checkout's own file, the shape a local `--repo` server has; the doorbell reads the index
    snapshot, which `_propose` publishes too."""
    identities_path = _write_identities(env)
    server_settings = Settings(
        identity=STEWARD, knowledge_repo=env.repo, identities_path=identities_path,
        dsn=testdb.dsn(), embedder="fake", llm="fake",
        librarian_repo_url=env.bare if librarian_repo_url is None else librarian_repo_url,
        entity_registry_path=entity_aliases.default_path(env.repo))
    slack_settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                                   channels_path="", server=server_settings)
    gw = gateway or FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    gw.seed_user(STEWARD_SLACK_ID, STEWARD)
    gw.seed_email(ALICE, ALICE_SLACK_ID)
    gw.seed_user(ALICE_SLACK_ID, ALICE)
    return SlackContext(settings=slack_settings, gateway=gw, conn=conn,
                        embedder=build_embedder("fake"), evidence=MemoryEvidenceStore())


def _proposed_page(name: str, entity_type: str = "organization", aliases=()) -> str:
    listed = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    return (f'---\ntype: entity\ntitle: "{name}"\nentity_type: {entity_type}\nrole: ""\n'
            f'status: developing\naliases: {listed}\ncreated: 2026-08-20\nupdated: 2026-08-20\n'
            f'tags: [entity, {entity_type}]\n'
            f'entity: ["{entities_generator.canonical_id_for(name)}"]\n'
            f'related: []\nsources: []\napproved_by: ""\nproposed_aliases: []\n---\n\n'
            f"# {name}\n\n## What / Who\n\n{name} is a {entity_type} the librarian proposed.\n")


def _publish_registry(env, conn) -> None:
    with open(os.path.join(env.repo, "ops", "entity-registry.json"), encoding="utf-8") as f:
        index_store.write_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH, f.read(), "test")


def _propose(env, conn, name: str = "Globex Robotics", *, aliases=()) -> str:
    path = os.path.join(env.repo, "wiki", "entities", f"{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_proposed_page(name, aliases=aliases))
    entities_generator.regenerate(env.repo)
    support.commit_and_push(env.repo, f"feat(note): the librarian proposed {name}")
    _publish_registry(env, conn)
    return entities_generator.canonical_id_for(name)


def _propose_alias(env, conn, entity_id: str, alias: str) -> str:
    [page] = [e for e in entities_generator.read_entity_pages(env.repo)
              if e.canonical_id == entity_id]
    full = os.path.join(env.repo, *page.relpath.split("/"))
    with open(full, encoding="utf-8") as f:
        text = f.read()
    listed = ", ".join(f'"{a}"' for a in (*page.proposed_aliases, alias))
    text = text.replace("related:", f"proposed_aliases: [{listed}]\nrelated:", 1)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)
    entities_generator.regenerate(env.repo)
    support.commit_and_push(env.repo, f"feat(entity): propose {alias}")
    _publish_registry(env, conn)
    return f"{entity_id}:{alias}"


def _remote_registry(env) -> dict:
    return json.loads(gitcmd.run("show", "main:ops/entity-registry.json", cwd=env.bare).stdout)


def _ledger(conn, kind, item_id):
    with conn.cursor() as cur:
        cur.execute("SELECT verdict, actor, extra FROM review_decisions WHERE item_kind = %s AND "
                    "item_id = %s ORDER BY created_at DESC LIMIT 1", (kind, item_id))
        return cur.fetchone()


def _run(coro):
    return asyncio.run(coro)


async def _click(ctx, *, action_id: str, value: str, user: str = STEWARD_SLACK_ID,
                 channel: str = STEWARD_SLACK_ID) -> None:
    await review.handle_block_action(ctx, action_id=action_id, value=value, trigger_id="T1",
                                     channel_id=channel, slack_user_id=user,
                                     event_team_id=TEAM_ID)


class _RecordingReviewDecide:
    """A `review_decide_safe`-shaped double, for the tests that prove SLACK's OWN plumbing —
    which identity, which collected fields, how a given result renders — independently of the
    governed door. Every test that must prove a REAL decision landed is real: real git, real
    Postgres, no double at all."""

    def __init__(self, result: dict):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, service, **kwargs) -> dict:
        self.calls.append({"identity": service.identity, **kwargs})
        return self.result


# ── action_id parsing ─────────────────────────────────────────────────────────────────────────
def test_parse_action_id_direct_and_modal():
    assert review._parse_action_id("review:identity-proposal:approve") == (
        "direct", "identity-proposal", "approve")
    assert review._parse_action_id("review-modal:identity-proposal:merge") == (
        "modal", "identity-proposal", "merge")
    assert review._parse_action_id("slack_show_page") is None
    assert review._parse_action_id("review:no-verdict-here") is None


# ── the direct verdicts land for real ─────────────────────────────────────────────────────────
def test_approve_button_confirms_the_identity_on_the_remote_and_says_so_in_the_dm(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")

    _run(_click(ctx, action_id="review:identity-proposal:approve", value=entity_id))

    entry = _remote_registry(env)["entities"][entity_id]
    assert entry["proposed"] is False and entry["approved_by"] == STEWARD
    verdict, actor, extra = _ledger(conn, KIND_IDENTITY_PROPOSAL, entity_id)
    assert (verdict, actor, extra["source"]) == ("approve", STEWARD, "slack")
    assert len(gw.posted) == 1
    assert "recorded: approve" in gw.posted[0].text and "confirmed" in gw.posted[0].text


def test_decline_button_removes_the_page_and_records_the_reject_the_librarian_reads(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")

    _run(_click(ctx, action_id="review:identity-proposal:decline", value=entity_id))

    assert entity_id not in _remote_registry(env)["entities"]
    files = gitcmd.run("ls-tree", "-r", "--name-only", "main", cwd=env.bare).stdout
    assert "wiki/entities/Globex Robotics.md" not in files
    verdict, _actor, _extra = _ledger(conn, KIND_IDENTITY_PROPOSAL, entity_id)
    assert verdict == "reject"
    assert "recorded: reject" in gw.posted[0].text


def test_alias_buttons_decide_the_spelling_under_its_own_item_id(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _propose_alias(env, conn, "acme-corp", "ACME Industries")

    _run(_click(ctx, action_id="review:alias-proposal:approve", value=item_id))

    entry = _remote_registry(env)["entities"]["acme-corp"]
    assert "ACME Industries" in entry["aliases"] and entry["proposed_aliases"] == []
    assert _ledger(conn, KIND_ALIAS_PROPOSAL, item_id)[0] == "approve"
    assert "recorded: approve" in gw.posted[0].text


# ── merge: the one modal ──────────────────────────────────────────────────────────────────────
def test_merge_button_opens_the_modal_with_the_proposals_candidates(env, conn):
    """`Acme Corporation` shares a word with the registered `Acme Corp`, so the picker offers it
    first; the typed field is always there for anything else."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Acme Corporation")

    _run(_click(ctx, action_id="review-modal:identity-proposal:merge", value=entity_id))

    assert gw.posted == [], "nothing is decided by opening the modal"
    assert len(gw.opened_views) == 1
    view = gw.opened_views[0]["view"]
    assert view["callback_id"] == render.MERGE_MODAL_CALLBACK_ID
    assert json.loads(view["private_metadata"]) == {
        "item_kind": "identity-proposal", "item_id": entity_id, "channel_id": STEWARD_SLACK_ID}
    select = next(b for b in view["blocks"] if b.get("block_id") == render.MERGE_SELECT_BLOCK_ID)
    assert [o["value"] for o in select["element"]["options"]] == ["acme-corp"]
    assert any(b.get("block_id") == render.MERGE_TYPED_BLOCK_ID for b in view["blocks"])
    assert "Acme Corporation" in view["blocks"][0]["text"]["text"]


def test_merge_modal_submission_folds_the_proposal_into_the_selected_survivor(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Acme Corporation")
    metadata = json.dumps({"item_kind": "identity-proposal", "item_id": entity_id,
                           "channel_id": STEWARD_SLACK_ID})
    state = {render.MERGE_SELECT_BLOCK_ID: {render.MERGE_SELECT_ACTION_ID: {
        "selected_option": {"value": "acme-corp"}}}}

    _run(review.handle_merge_modal_submission(ctx, private_metadata=metadata, state_values=state,
                                              slack_user_id=STEWARD_SLACK_ID,
                                              event_team_id=TEAM_ID))

    registry = _remote_registry(env)["entities"]
    assert entity_id not in registry
    assert "Acme Corporation" in registry["acme-corp"]["aliases"]
    verdict, _actor, extra = _ledger(conn, KIND_IDENTITY_PROPOSAL, entity_id)
    assert verdict == "merge" and extra["into"] == "acme-corp"
    assert "recorded: merge" in gw.posted[0].text


def test_merge_modal_submission_accepts_a_typed_survivor_when_nothing_was_selected(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")
    metadata = json.dumps({"item_kind": "identity-proposal", "item_id": entity_id,
                           "channel_id": STEWARD_SLACK_ID})
    state = {render.MERGE_TYPED_BLOCK_ID: {render.MERGE_TYPED_ACTION_ID: {"value": " acme-corp "}}}

    _run(review.handle_merge_modal_submission(ctx, private_metadata=metadata, state_values=state,
                                              slack_user_id=STEWARD_SLACK_ID,
                                              event_team_id=TEAM_ID))

    assert "Globex Robotics" in _remote_registry(env)["entities"]["acme-corp"]["aliases"]


def test_merge_modal_submission_with_no_survivor_at_all_says_so_and_decides_nothing(env, conn,
                                                                                    monkeypatch):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")
    recorder = _RecordingReviewDecide({"message": "must not be called"})
    monkeypatch.setattr(server_review, "review_decide_safe", recorder)
    metadata = json.dumps({"item_kind": "identity-proposal", "item_id": entity_id,
                           "channel_id": STEWARD_SLACK_ID})

    _run(review.handle_merge_modal_submission(ctx, private_metadata=metadata, state_values={},
                                              slack_user_id=STEWARD_SLACK_ID,
                                              event_team_id=TEAM_ID))

    assert recorder.calls == []
    assert gw.posted[0].text == copy.MERGE_NEEDS_TARGET


def test_merge_modal_opens_with_the_typed_field_alone_when_the_item_is_no_longer_open(env, conn):
    """Decided between the DM and the click: the modal still opens (Slack's `trigger_id` is
    already spent), with no candidates, and `review_decide` refuses the stale merge on submit."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)

    _run(_click(ctx, action_id="review-modal:identity-proposal:merge", value="ghost"))

    view = gw.opened_views[0]["view"]
    assert not any(b.get("block_id") == render.MERGE_SELECT_BLOCK_ID for b in view["blocks"])
    assert any(b.get("block_id") == render.MERGE_TYPED_BLOCK_ID for b in view["blocks"])


# ── the surface forwards exactly what it collected, and names its own door ────────────────────
def test_a_direct_click_calls_review_decide_safe_with_the_slack_door_and_the_clickers_identity(
        env, conn, monkeypatch):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    recorder = _RecordingReviewDecide({"recorded": "approve", "message": "recorded: approve …"})
    monkeypatch.setattr(server_review, "review_decide_safe", recorder)

    _run(_click(ctx, action_id="review:identity-proposal:approve", value="globex-robotics",
                user=ALICE_SLACK_ID))

    assert recorder.calls == [{"identity": ALICE, "item_kind": "identity-proposal",
                               "item_id": "globex-robotics", "verdict": "approve",
                               "source": server_review.SOURCE_SLACK, "into": ""}]
    assert gw.posted[0].text == "recorded: approve …"


def test_the_submitting_identity_comes_from_the_caller_never_from_private_metadata(env, conn,
                                                                                   monkeypatch):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    recorder = _RecordingReviewDecide({"recorded": "merge", "message": "recorded: merge …"})
    monkeypatch.setattr(server_review, "review_decide_safe", recorder)
    metadata = json.dumps({"item_kind": "identity-proposal", "item_id": "globex-robotics",
                           "channel_id": "C_WHEREVER", "slack_user_id": STEWARD_SLACK_ID,
                           "email": STEWARD})
    state = {render.MERGE_TYPED_BLOCK_ID: {render.MERGE_TYPED_ACTION_ID: {"value": "acme-corp"}}}

    _run(review.handle_merge_modal_submission(ctx, private_metadata=metadata, state_values=state,
                                              slack_user_id=ALICE_SLACK_ID, event_team_id=TEAM_ID))

    assert recorder.calls[0]["identity"] == ALICE
    assert recorder.calls[0]["into"] == "acme-corp"
    assert gw.posted[0].channel_id == "C_WHEREVER"


# ── refusals reach the steward as a message, never a swallowed exception ──────────────────────
def test_a_non_steward_gets_the_anonymous_refusal_and_nothing_lands(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")

    _run(_click(ctx, action_id="review:identity-proposal:approve", value=entity_id,
                user=ALICE_SLACK_ID, channel=ALICE_SLACK_ID))

    assert gw.posted[0].text == server_review.NOT_YOURS_TO_DECIDE
    assert _remote_registry(env)["entities"][entity_id]["proposed"] is True
    assert _ledger(conn, KIND_IDENTITY_PROPOSAL, entity_id) is None


def test_a_missing_repo_url_names_the_capability_in_the_dm(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw, librarian_repo_url="")
    entity_id = _propose(env, conn, "Globex Robotics")

    _run(_click(ctx, action_id="review:identity-proposal:approve", value=entity_id))

    assert "STIGMERGY_LIBRARIAN_REPO_URL" in gw.posted[0].text


def test_a_stale_click_after_another_door_decided_names_the_decision_that_beat_it(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")
    _run(_click(ctx, action_id="review:identity-proposal:approve", value=entity_id))

    _run(_click(ctx, action_id="review:identity-proposal:decline", value=entity_id))

    text = gw.posted[1].text
    assert "confirmed entity, not a proposal" in text
    assert f"already decided: approve by {STEWARD} via slack" in text


def test_a_ledger_actor_that_looks_like_a_link_reaches_the_steward_as_literal_text(env, conn):
    """The confirmation is a bare `text=` message and Slack renders it as mrkdwn: an actor
    spelled `<https://evil.example|Approve>` in the ledger must arrive escaped."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")
    _run(_click(ctx, action_id="review:identity-proposal:approve", value=entity_id))
    server_review.record_decision(conn, item_kind=KIND_IDENTITY_PROPOSAL, item_id=entity_id,
                                  verdict="approve", actor="<https://evil.example|Approve>",
                                  source=server_review.SOURCE_ADMIN)

    _run(_click(ctx, action_id="review:identity-proposal:decline", value=entity_id))

    text = gw.posted[-1].text
    assert "<https://evil.example|Approve>" not in text
    assert "&lt;https://evil.example|Approve&gt;" in text


def test_an_unanticipated_fault_tells_the_steward_instead_of_going_silent(env, conn, monkeypatch):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)

    def _boom(*_a, **_k):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(server_review, "review_decide_safe", _boom)

    _run(_click(ctx, action_id="review:identity-proposal:approve", value="globex-robotics"))

    assert len(gw.posted) == 1
    assert gw.posted[0].text == copy.server_error()


# ── remaining branch coverage: unowned actions, identity failures, malformed input ────────────
def test_an_action_id_this_module_does_not_own_is_ignored(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    _run(_click(ctx, action_id="slack_show_page", value="whatever"))
    assert gw.posted == [] and gw.opened_views == []


def test_an_unresolvable_identity_is_silently_declined_on_a_block_action(env, conn, monkeypatch):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    recorder = _RecordingReviewDecide({"message": "must not be called"})
    monkeypatch.setattr(server_review, "review_decide_safe", recorder)

    _run(_click(ctx, action_id="review:identity-proposal:approve", value="globex-robotics",
                user="U_NOBODY"))

    assert recorder.calls == [] and gw.posted == []


def test_an_unresolvable_identity_is_silently_declined_on_a_merge_submission(env, conn,
                                                                            monkeypatch):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    recorder = _RecordingReviewDecide({"message": "must not be called"})
    monkeypatch.setattr(server_review, "review_decide_safe", recorder)
    metadata = json.dumps({"item_kind": "identity-proposal", "item_id": "x", "channel_id": "C"})
    state = {render.MERGE_TYPED_BLOCK_ID: {render.MERGE_TYPED_ACTION_ID: {"value": "acme-corp"}}}

    _run(review.handle_merge_modal_submission(ctx, private_metadata=metadata, state_values=state,
                                              slack_user_id="U_NOBODY", event_team_id=TEAM_ID))

    assert recorder.calls == [] and gw.posted == []


def test_a_malformed_modal_metadata_payload_is_logged_and_ignored(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    _run(review.handle_merge_modal_submission(ctx, private_metadata="{not json",
                                              state_values={}, slack_user_id=STEWARD_SLACK_ID,
                                              event_team_id=TEAM_ID))
    assert gw.posted == []


def test_a_failed_modal_open_is_logged_not_raised(env, conn):
    gw = FakeSlackGateway()
    gw.fail_views_open_count = 1
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")

    _run(_click(ctx, action_id="review-modal:identity-proposal:merge", value=entity_id))
    # no exception — and nothing decided
    assert _ledger(conn, KIND_IDENTITY_PROPOSAL, entity_id) is None


def test_a_stale_modal_action_from_an_older_deploy_is_declined_not_a_crash(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)

    _run(_click(ctx, action_id="review-modal:entity-proposal:approve", value="42"))

    assert gw.opened_views == []
    assert gw.posted[0].text == copy.STALE_REVIEW_ACTION


def test_the_door_is_monkeypatchable_where_the_server_reaches_it(env, conn, monkeypatch):
    """The Slack surface reaches git only through `entities.remote.decide_via_clone` (via the
    server); a test that wants no git at all can replace that one attribute."""
    def marker(*_a, **_k):
        raise AssertionError("decide_via_clone ran")
    monkeypatch.setattr(entities_remote, "decide_via_clone", marker)
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)

    _run(_click(ctx, action_id="review:identity-proposal:approve", value="ghost",
                user=ALICE_SLACK_ID))   # refused before the door, by authorization

    assert gw.posted[0].text == server_review.NOT_YOURS_TO_DECIDE
