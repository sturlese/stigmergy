"""The Slack review surface — buttons on a doorbell card that call `review_decide`, the short modal
for the one piece of free text some verdicts require, and the entity-mint modal (ADR 030 D5) an
entity-proposal Approve opens instead of firing directly. The bot enforces nothing: it only resolves
who is asking and calls `review_decide`.
"""
import asyncio
import json

import pytest
from psycopg.types.json import Jsonb

from stigmergy.capture import schema as capture_schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.entities import generator as entities_generator
from stigmergy.entities import remote as entities_remote
from stigmergy.entities.errors import EntityError
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.librarian import gitcmd, githubapp
from stigmergy.server import review as server_review
from stigmergy.server.settings import Settings
from stigmergy.slack import copy, render, review
from stigmergy.slack.context import SlackContext
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.settings import SlackSettings
from tests import testdb
from tests.librarian import support

pytestmark = [pytest.mark.usefixtures("no_real_github_app"), pytest.mark.timeout(30)]

TEAM_ID = "T_STIGMERGY"
STEWARD = "steward@example.com"
STEWARD_SLACK_ID = "U_STEWARD"
ALICE = "alice@example.com"


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
    yield c
    c.close()


@pytest.fixture()
def require_gitleaks():
    if support.gitleaks_available():
        return
    pytest.skip("gitleaks not on PATH (brew install gitleaks) — the repo fixtures need it")


def _seed_stewards(env, mapping: dict) -> None:
    """`review_decide`'s authorization resolves stewardship from `ops/stewards.json`, read fresh
    at the base commit — the Slack review surface calls the same `review_decide` every other
    transport does (the bot enforces nothing, it only resolves who is asking), so `STEWARD` has to
    actually BE one for these tests to exercise the buttons and modals rather than the refusal.
    Written out here rather than imported from `tests.server.conftest` for the same reason this
    file already duplicates `_park_capture`/`no_real_github_app`: a cross-file fixture-sharing
    import reads as a redefinition to the linter."""
    import json as _json
    import os as _os
    path = _os.path.join(env.repo, "ops", "stewards.json")
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_json.dumps(mapping))
    support.commit_and_push(env.repo, "test: seed ops/stewards.json")


@pytest.fixture()
def env(tmp_path, require_gitleaks):
    repo_env = support.build_repo(str(tmp_path))
    _seed_stewards(repo_env, {"*": [STEWARD]})
    return repo_env


def _fix_preexisting_fixture_drift(env) -> None:
    """`tests/server/test_review.py`'s function of the same name, duplicated rather than imported
    — a cross-file fixture-sharing import reads as a redefinition to the linter, the same reason
    this file already duplicates `_park_capture`/`no_real_github_app`. `tests/librarian/
    fixtures/repo/`'s `Acme Corp` entry predates the registry-consistency rule (its curated id
    `acme` disagrees with `slugify`'s own `acme-corp`), which `entities.mint._refuse_drift` catches
    on ANY mint attempt against this fixture, regardless of what is being minted — only a test that
    actually reaches `entities.mint.mint` (a REAL mint through the Slack modal, below) needs this
    fixed first."""
    outcome = entities_generator.regenerate(env.repo)
    assert outcome.changed, "the fixture's own legacy drift is gone — this shim is no longer needed"
    support.commit_and_push(env.repo, "test: regenerate the derived registry view")


@pytest.fixture()
def drift_free_env(env):
    _fix_preexisting_fixture_drift(env)
    return env


def _write_identities(env) -> str:
    import os
    path = os.path.join(env.repo, "ops", "identities.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({STEWARD: "*", ALICE: "*"}, f)
    return path


def make_ctx(env, conn, *, gateway=None, librarian_repo_url: str = "") -> SlackContext:
    """`librarian_repo_url` defaults to `""` (every existing caller's own behaviour: a mint refuses
    ADR 030 D3-style, naming the missing capability). A test that mints for real passes `env.bare`
    — the same local `git init --bare` remote `env.repo` is a clone of, and not `https://`, so
    `entities.remote.mint_via_clone` needs no GitHub App credential at all
    (`tests/server/conftest.py::make_review_service`'s own docstring states the same trick)."""
    identities_path = _write_identities(env)
    server_settings = Settings(identity=STEWARD, knowledge_repo=env.repo,
                              identities_path=identities_path, dsn=testdb.dsn(),
                              embedder="fake", llm="fake", librarian_repo_url=librarian_repo_url)
    slack_settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                                   channels_path="", server=server_settings)
    gw = gateway or FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    return SlackContext(settings=slack_settings, gateway=gw, conn=conn,
                        embedder=build_embedder("fake"), evidence=MemoryEvidenceStore())


def _mint_state_values(*, name: str = "", entity_type: str = "", aliases: str = "", role: str = "",
                       requeue: bool = True) -> dict:
    """The `state_values` shape `views_submission` hands `handle_entity_mint_modal_submission` for
    `render.render_entity_mint_modal`'s five fields — built once here rather than reconstructed
    per test, the same convenience `render.REVIEW_NOTE_MODAL_BLOCK_ID`'s single-field shape did not
    need but this one, with five, does."""
    requeue_option = {"text": {"type": "plain_text", "text": copy.ENTITY_MINT_REQUEUE_OPTION_LABEL},
                      "value": render.ENTITY_MINT_REQUEUE_OPTION_VALUE}
    return {
        render.ENTITY_MINT_NAME_BLOCK_ID: {
            render.ENTITY_MINT_NAME_ACTION_ID: {"value": name}},
        render.ENTITY_MINT_TYPE_BLOCK_ID: {
            render.ENTITY_MINT_TYPE_ACTION_ID: {
                "selected_option": {"value": entity_type,
                                    "text": {"type": "plain_text", "text": entity_type}}}},
        render.ENTITY_MINT_ALIASES_BLOCK_ID: {
            render.ENTITY_MINT_ALIASES_ACTION_ID: {"value": aliases}},
        render.ENTITY_MINT_ROLE_BLOCK_ID: {
            render.ENTITY_MINT_ROLE_ACTION_ID: {"value": role}},
        render.ENTITY_MINT_REQUEUE_BLOCK_ID: {
            render.ENTITY_MINT_REQUEUE_ACTION_ID: {
                "selected_options": [requeue_option] if requeue else []}},
    }


class _RecordingReviewDecide:
    """A `review_decide_safe`-shaped double, standing in for
    `stigmergy.server.review.review_decide_safe` in the tests below that must prove SLACK's OWN
    plumbing — which identity, which collected fields, how a given result renders — independently
    of that function's real signature.

    **Why a double here, when this repo's own doctrine is "never fake what you are claiming to
    prove: a faked git proves nothing about the property being claimed":** at the time of this
    change, `review_decide_safe(service, *, item_kind, item_id, verdict, notes="")` does not yet
    forward the ADR 030 D5 mint metadata (`name`/`entity_id`/`entity_type`/`aliases`/`role`/
    `requeue`) to `service.review_decide`, even though `BrainService.review_decide` and the MCP
    `review_decide` tool already do — a signature gap in `src/stigmergy/server/review.py` outside
    this change's own scope (see the delivery notes). Every test that only needs to prove SLACK's
    OWN forwarding/rendering logic uses this double so it is not blocked on that gap; every test
    that must prove a REAL mint (or a real refusal) happened is REAL — real git, real Postgres, no
    double at all — and was marked `xfail(strict=True)` until review_decide_safe
    forwarded the metadata (it does now), exactly the posture
    `tests/server/test_host_header.py`'s own note documents for a reproduce-before-fix test."""

    def __init__(self, result: dict):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, service, **kwargs) -> dict:
        self.calls.append({"identity": service.identity, **kwargs})
        return self.result


def _park_capture(conn, evidence, *, submitted_by=ALICE, situation=None) -> int:
    key = evidence.put(b"material")
    report = {"summary": "parked", "status": capture_schema.TRIAGE}
    if situation:
        report[capture_schema.SITUATION_KEY] = situation
        report[capture_schema.SITUATION_NAME_KEY] = "Globex Robotics"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capture_queue (kind, payload, blob_refs, submitted_by, status, report) "
            "VALUES ('raw', '{}', %s, %s, %s, %s) RETURNING id",
            ([key], submitted_by, capture_schema.TRIAGE, Jsonb(report)))
        return cur.fetchone()[0]


def _run(coro):
    return asyncio.run(coro)


# ── action_id parsing ─────────────────────────────────────────────────────────────────────────
def test_parse_action_id_direct_and_modal():
    assert review._parse_action_id("review:parked-capture:requeue") == (
        "direct", "parked-capture", "requeue")
    assert review._parse_action_id("review-modal:parked-capture:resolve") == (
        "modal", "parked-capture", "resolve")
    assert review._parse_action_id("some_other_action") is None
    # `entity-proposal:approve` is recognized under BOTH prefixes: the new one is what this build
    # renders (ADR 030 D5, below); the old (direct) one still parses — see
    # `test_a_stale_direct_approve_action_...` — because a card rendered by an OLDER deploy may
    # still carry it.
    assert review.is_review_action("review:entity-proposal:approve") is True
    assert review.is_review_action("review-modal:entity-proposal:approve") is True
    assert review.is_review_action("slack_show_page") is False


# ── direct-fire actions: requeue ─────────────────────────────────────────────────────────────
def test_requeue_button_fires_directly_and_confirms_in_the_dm(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore())

    _run(review.handle_block_action(
        ctx, action_id="review:parked-capture:requeue", value=str(item_id), trigger_id="T1",
        channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    assert "requeue" in gw.posted[0].text
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM review_decisions WHERE item_id = %s", (str(item_id),))
        assert cur.fetchone()[0] == "requeue"


# ── entity-proposal approve: ADR 030 D5 — a modal that mints on submit ──────────────────────────
# `test_entity_proposal_approve_returns_the_mint_command_in_the_dm` pinned the OLD contract
# (pre-#36 phase 2): Approve fired DIRECTLY (`review:entity-proposal:approve`, no modal) and the DM
# echoed the CLI's own `stigmergy-entities approve <slug> --type ...` command — `mint_command`,
# deleted server-side by this branch's phase 1 commit ("mint_command is gone; the docstring's
# 'never writes to git' is rewritten to the truth"). Approve now opens a metadata modal instead and
# mints on submit; see the tests below. `test_a_stale_direct_approve_action_...` keeps the OLD
# action_id's own coverage alive, retargeted at what it must do NOW: a graceful, actionable refusal
# for an older deploy's card, never a silent mint and never the deleted command text.
def test_a_stale_direct_approve_action_from_an_older_deploy_is_refused_not_a_silent_mint(
        env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)

    _run(review.handle_block_action(
        ctx, action_id="review:entity-proposal:approve", value=str(item_id), trigger_id="T1",
        channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert "stigmergy-entities approve" not in gw.posted[0].text   # mint_command is gone
    assert "missing name and entity_type" in gw.posted[0].text
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s", (str(item_id),))
        assert cur.fetchone()[0] == 0   # refused, never a silent mint


def test_entity_proposal_approve_button_opens_the_mint_modal_with_the_name_prefilled(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)

    _run(review.handle_block_action(
        ctx, action_id="review-modal:entity-proposal:approve", value=str(item_id),
        trigger_id="T1", channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID,
        event_team_id=TEAM_ID))

    assert gw.posted == []                # nothing decided yet — waiting on the modal
    assert len(gw.opened_views) == 1
    view = gw.opened_views[0]["view"]
    assert view["callback_id"] == render.ENTITY_MINT_MODAL_CALLBACK_ID
    # private_metadata carries only WHAT the decision is about — never WHO; there is no `verdict`
    # key at all (unlike the generic note modal) because this modal's own callback_id already says
    # the verdict is always approve.
    assert json.loads(view["private_metadata"]) == {
        "item_kind": "entity-proposal", "item_id": str(item_id), "channel_id": STEWARD_SLACK_ID}

    blocks_by_id = {b["block_id"]: b for b in view["blocks"]}
    name_element = blocks_by_id[render.ENTITY_MINT_NAME_BLOCK_ID]["element"]
    assert name_element["initial_value"] == "Globex Robotics"   # the proposal's own subject
    type_options = blocks_by_id[render.ENTITY_MINT_TYPE_BLOCK_ID]["element"]["options"]
    # the closed six `entities.mint` actually accepts — never a hand-copied list this test could
    # drift from independently of the real one.
    assert [o["value"] for o in type_options] == list(entities_generator.ENTITY_TYPES)
    aliases_block = blocks_by_id[render.ENTITY_MINT_ALIASES_BLOCK_ID]
    role_block = blocks_by_id[render.ENTITY_MINT_ROLE_BLOCK_ID]
    assert aliases_block.get("optional") is True
    assert role_block.get("optional") is True
    requeue_block = blocks_by_id[render.ENTITY_MINT_REQUEUE_BLOCK_ID]
    assert requeue_block.get("optional") is True   # so it CAN be submitted unchecked
    assert len(requeue_block["element"]["initial_options"]) == 1   # pre-checked


def test_entity_proposal_approve_modal_prefill_is_empty_when_the_item_is_no_longer_open(
        env, conn):
    """The doorbell-card scenario where the proposal was decided or disposed of between the DM and
    this click (see `_proposed_name_for`'s own docstring): the modal still opens, just with an
    empty name field a steward can fill by hand — never a crash, never a stale name."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)

    _run(review.handle_block_action(
        ctx, action_id="review-modal:entity-proposal:approve", value="999999",
        trigger_id="T1", channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID,
        event_team_id=TEAM_ID))

    assert len(gw.opened_views) == 1
    view = gw.opened_views[0]["view"]
    blocks_by_id = {b["block_id"]: b for b in view["blocks"]}
    assert "initial_value" not in blocks_by_id[render.ENTITY_MINT_NAME_BLOCK_ID]["element"]


def test_entity_mint_modal_submission_calls_review_decide_safe_with_the_collected_metadata(
        env, conn, monkeypatch):
    """Proves SLACK's OWN plumbing (see `_RecordingReviewDecide`'s docstring for why
    `review_decide_safe` is doubled here): every field the steward typed/selected/checked reaches
    `review_decide_safe`, under the RE-RESOLVED caller identity — never a value from
    `private_metadata`, which here carries an attacker-shaped extra `slack_user_id` key naming a
    DIFFERENT identity, the same probe `test_the_submitting_identity_comes_from_the_caller_never_
    from_private_metadata` already runs against the note modal."""
    fake = _RecordingReviewDecide({"recorded": "approve", "item_kind": "entity-proposal",
                                   "item_id": "1", "actor": STEWARD, "minted": True,
                                   "entity_id": "globex-robotics", "name": "Globex Robotics",
                                   "commit": "a" * 40, "requeued": True})
    monkeypatch.setattr(server_review, "review_decide_safe", fake)
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "slack_user_id": "U_SOMEONE_ELSE", "channel_id": STEWARD_SLACK_ID})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization",
                                      aliases="Globex, Globex Inc", role="a robotics maker",
                                      requeue=True)

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["identity"] == STEWARD          # the RE-RESOLVED caller, never private_metadata's
    assert call["item_kind"] == "entity-proposal"
    assert call["item_id"] == str(item_id)
    assert call["verdict"] == "approve"
    assert call["name"] == "Globex Robotics"
    assert call["entity_type"] == "organization"
    assert call["aliases"] == "Globex, Globex Inc"
    assert call["role"] == "a robotics maker"
    assert call["requeue"] is True


def test_entity_mint_modal_submission_requeue_checkbox_unchecked_passes_requeue_false(
        env, conn, monkeypatch):
    """The other half of the checkbox's own state — pre-checked by default (see the render test
    above), and a steward who unchecks it must have that reach `review_decide_safe` as `False`, not
    silently stay `True`."""
    fake = _RecordingReviewDecide({"recorded": "approve", "minted": True, "entity_id": "x",
                                   "name": "X", "commit": "a" * 40, "requeued": False})
    monkeypatch.setattr(server_review, "review_decide_safe", fake)
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": STEWARD_SLACK_ID})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization",
                                      requeue=False)

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert fake.calls[0]["requeue"] is False


def test_entity_mint_modal_submission_minted_confirmation_names_entity_and_commit(
        env, conn, monkeypatch):
    """`_confirmation_text`'s new branch (`result["minted"]`) — a rendering claim, proven against a
    hand-built result dict the same way `tests/slack/test_render.py` proves `render_answer` against
    a hand-built answer dict, rather than requiring a live mint for a claim that is purely about
    this package's own rendering."""
    commit = "abc1234567" + "0" * 30
    assert len(commit) == 40
    fake = _RecordingReviewDecide({"recorded": "approve", "minted": True,
                                   "entity_id": "globex-robotics", "name": "Globex Robotics",
                                   "commit": commit, "requeued": True})
    monkeypatch.setattr(server_review, "review_decide_safe", fake)
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": STEWARD_SLACK_ID})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization")

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    text = gw.posted[0].text
    assert "Globex Robotics" in text
    assert "globex-robotics" in text
    assert commit[:12] in text
    assert commit not in text   # the SHORT commit is named, never the full 40 characters


def test_entity_mint_modal_submission_mints_for_real_end_to_end(drift_free_env, conn):
    """The end-to-end proof, mirroring `tests/server/test_review.py::
    test_review_decide_entity_proposal_approve_mints_for_real` but driven through the Slack modal
    submission handler instead of calling `review.review_decide` directly: a real commit, on a real
    bare remote, with no double anywhere on the path."""
    env = drift_free_env
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw, librarian_repo_url=env.bare)
    item_id = _park_capture(conn, MemoryEvidenceStore(), submitted_by=ALICE,
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": STEWARD_SLACK_ID})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization",
                                      aliases="Globex, Globex Robotics Inc",
                                      role="a robotics manufacturer", requeue=True)

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    text = gw.posted[0].text
    assert "Globex Robotics" in text
    assert "globex-robotics" in text
    with conn.cursor() as cur:
        cur.execute("SELECT verdict, actor, extra FROM review_decisions WHERE item_id = %s",
                   (str(item_id),))
        verdict, actor, extra = cur.fetchone()
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (item_id,))
        (status,) = cur.fetchone()
    assert verdict == "approve"
    assert actor == STEWARD
    assert extra["entity_id"] == "globex-robotics"
    assert extra["commit"][:12] in text
    assert status == capture_schema.QUEUED   # requeue=True sent it back to the librarian

    # The same door-parity proof `tests/server/test_review.py`'s own mint-for-real test makes for
    # MCP (ADR 030 D1): the commit lands authored as the App, carrying an `Approved-by:` trailer
    # naming the STEWARD Slack re-resolved — not merely that review_decisions' `actor` column says
    # so, but that the value actually reached the governed door's git commit. Nothing before this
    # change checked it on THIS door; a bug specific to how Slack threads its resolved identity
    # into the mint call would have shipped invisibly.
    commit = extra["commit"]
    author = gitcmd.run("log", "-1", "--format=%an <%ae>", commit, cwd=env.bare).stdout.strip()
    assert author == "stigmergy-librarian <stigmergy-librarian@users.noreply.github.com>"
    message = gitcmd.run("log", "-1", "--format=%B", commit, cwd=env.bare).stdout
    assert f"Approved-by: {STEWARD}" in message


def test_a_refused_mint_reports_through_the_existing_error_shape_and_strands_nothing(
        env, conn, monkeypatch):
    """`entities.remote.mint_via_clone` is the seam `_mint_entity_proposal` calls as a MODULE
    ATTRIBUTE precisely so a test can patch it (that function's own docstring) — standing in for a
    real refusal (a collision, a credential outage; both already proven end to end in
    `tests/server/test_review.py`) without re-deriving one. What THIS test proves is Slack's own
    plumbing: the refusal reaches the steward as the SAME error-shape message `review_decide_safe`
    already returns for every other clean refusal on this surface, and nothing is half-written —
    a REAL claim (no review_decisions row), so this one stays real rather than doubling
    `review_decide_safe` itself."""
    def _refuse(*_a, **_kw):
        raise EntityError("a collision the registry already knows about")
    monkeypatch.setattr(entities_remote, "mint_via_clone", _refuse)

    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw, librarian_repo_url=env.bare)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": STEWARD_SLACK_ID})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization")

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    assert "collision the registry already knows about" in gw.posted[0].text
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s", (str(item_id),))
        assert cur.fetchone()[0] == 0   # a refused mint records nothing — nothing stranded


# ── the authorization guard, proven on THIS door too ─────────────────────────────────────────────
# `review_decide`'s own `_guard_governance_decision` is exhaustively proven in
# `tests/server/test_review.py` (non-steward, self-approval, both WITH valid metadata in hand).
# Everything through here calls the SAME shared function ("the bot enforces nothing" — this
# module's own docstring), but nothing before these two proved that Slack's own re-resolution of
# the acting identity actually reaches it unmolested on a REAL mint door: a doubled
# `review_decide_safe` (as `test_entity_mint_modal_submission_calls_review_decide_safe_with_the_
# collected_metadata` above uses) proves the CALL is shaped correctly, never that a real attempt
# through the real door mints nothing.
def test_entity_mint_modal_submission_non_steward_is_refused_and_mints_nothing(
        drift_free_env, conn):
    """The Slack-door twin of `tests/server/test_review.py::
    test_review_decide_entity_proposal_approve_non_steward_still_refused_byte_identically`: a
    resolvable but non-steward identity gets the exact same `NOT_YOURS_TO_DECIDE` sentence even
    WITH valid metadata typed into the modal, and git stays untouched."""
    env = drift_free_env
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw, librarian_repo_url=env.bare)
    gw.seed_email(ALICE, "U_ALICE")   # ALICE resolves, but ops/stewards.json names only STEWARD
    item_id = _park_capture(conn, MemoryEvidenceStore(), submitted_by=STEWARD,
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": "U_ALICE"})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization")
    before = gitcmd.run("rev-parse", "main", cwd=env.bare).stdout

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id="U_ALICE", event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    assert server_review.NOT_YOURS_TO_DECIDE in gw.posted[0].text
    after = gitcmd.run("rev-parse", "main", cwd=env.bare).stdout
    assert before == after
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s", (str(item_id),))
        assert cur.fetchone()[0] == 0   # metadata never buys past authorization


def test_entity_mint_modal_submission_self_approval_is_refused_and_mints_nothing(
        drift_free_env, conn):
    """The Slack-door twin of `tests/server/test_review.py::
    test_review_decide_entity_proposal_approve_self_approval_still_refused`: the steward who filed
    the proposal cannot approve their own, even from the modal, even with valid metadata."""
    env = drift_free_env
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw, librarian_repo_url=env.bare)
    item_id = _park_capture(conn, MemoryEvidenceStore(), submitted_by=STEWARD,
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": STEWARD_SLACK_ID})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization")
    before = gitcmd.run("rev-parse", "main", cwd=env.bare).stdout

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    assert server_review.SELF_APPROVAL_REFUSED in gw.posted[0].text
    after = gitcmd.run("rev-parse", "main", cwd=env.bare).stdout
    assert before == after
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s", (str(item_id),))
        assert cur.fetchone()[0] == 0


def test_entity_mint_modal_submission_missing_repo_url_names_the_capability(env, conn):
    """Missing URL/credential -> `CapabilityUnavailableError` posture (ADR 030 D3), Slack's own
    confirmation shape — the real refusal, no monkeypatch needed: `make_ctx`'s default
    `librarian_repo_url=""` (every OTHER test in this file that does not mint for real) is exactly
    the shape that hit production — the `slack` process group runs with no knowledge-repo
    checkout and no `$STIGMERGY_LIBRARIAN_REPO_URL`, so `_authenticated_url` refuses for real."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)   # no librarian_repo_url override — the missing capability
    item_id = _park_capture(conn, MemoryEvidenceStore(), submitted_by=ALICE,
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": STEWARD_SLACK_ID})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization")

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    assert "STIGMERGY_LIBRARIAN_REPO_URL" in gw.posted[0].text
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s", (str(item_id),))
        assert cur.fetchone()[0] == 0


def test_an_unresolvable_identity_is_silently_declined_on_the_entity_mint_modal_submission(
        env, conn):
    gw = FakeSlackGateway()   # STEWARD_SLACK_ID is never seeded with an email at all
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    ctx.gateway = gw
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "channel_id": "U_UNKNOWN"})
    state_values = _mint_state_values(name="Globex Robotics", entity_type="organization")

    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id="U_UNKNOWN", event_team_id=TEAM_ID))

    assert gw.posted == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0


def test_a_malformed_entity_mint_modal_metadata_payload_is_logged_and_ignored(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    _run(review.handle_entity_mint_modal_submission(
        ctx, private_metadata="{not valid json", state_values={},
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))
    assert gw.posted == []


# ── a clean refusal reaches the steward as a message, not a swallowed exception ─────────────────
def test_entity_proposal_reject_button_opens_a_modal_not_a_direct_call(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)

    _run(review.handle_block_action(
        ctx, action_id="review-modal:entity-proposal:reject", value=str(item_id),
        trigger_id="T1", channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID,
        event_team_id=TEAM_ID))

    assert gw.posted == []               # nothing recorded yet — waiting on the modal
    assert len(gw.opened_views) == 1
    metadata = json.loads(gw.opened_views[0]["view"]["private_metadata"])
    # private_metadata carries only WHAT the decision is about — never WHO is making it.
    # `slack_user_id`/`event_team_id` no longer round-trip through here at all.
    assert metadata == {"item_kind": "entity-proposal", "item_id": str(item_id),
                        "verdict": "reject", "field": "notes",
                        "channel_id": STEWARD_SLACK_ID}


def test_modal_submission_calls_review_decide_with_the_typed_note(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "verdict": "reject", "field": "notes",
                          "channel_id": STEWARD_SLACK_ID})
    state_values = {render.REVIEW_NOTE_MODAL_BLOCK_ID: {
        render.REVIEW_NOTE_MODAL_ACTION_ID: {"value": "not a real identity"}}}

    _run(review.handle_note_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    with conn.cursor() as cur:
        cur.execute("SELECT verdict, notes FROM review_decisions WHERE item_id = %s",
                   (str(item_id),))
        verdict, notes = cur.fetchone()
    assert verdict == "reject"
    assert notes == "not a real identity"
    assert len(gw.posted) == 1


def test_a_clean_refusal_is_shown_to_the_steward_not_swallowed(env, conn):
    """`resolve` on a parked capture with an empty note is a CLEAN refusal
    (`dispositions`/`review._decide_parked_capture`'s own validation) — `review_decide_safe`
    returns it as a message rather than raising, and the steward sees it, not a generic failure."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore())
    metadata = json.dumps({"item_kind": "parked-capture", "item_id": str(item_id),
                          "verdict": "resolve", "field": "notes",
                          "channel_id": STEWARD_SLACK_ID})
    state_values = {render.REVIEW_NOTE_MODAL_BLOCK_ID: {
        render.REVIEW_NOTE_MODAL_ACTION_ID: {"value": ""}}}

    _run(review.handle_note_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    assert len(gw.posted) == 1
    assert "resolve requires a note" in gw.posted[0].text
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions WHERE item_id = %s", (str(item_id),))
        assert cur.fetchone()[0] == 0   # nothing recorded — the refusal happened before any write





# ── remaining branch coverage: unowned actions, identity failures, malformed input ──────────────
def test_an_action_id_this_module_does_not_own_is_ignored(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    _run(review.handle_block_action(
        ctx, action_id="slack_show_page", value="1", trigger_id="T1",
        channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))
    assert gw.posted == []
    assert gw.opened_views == []


def test_an_unresolvable_identity_is_silently_declined_on_a_block_action(env, conn):
    gw = FakeSlackGateway()   # STEWARD_SLACK_ID is never seeded with an email at all
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    ctx.gateway = gw   # override make_ctx's own seeding
    item_id = _park_capture(conn, MemoryEvidenceStore())

    _run(review.handle_block_action(
        ctx, action_id="review:parked-capture:requeue", value=str(item_id), trigger_id="T1",
        channel_id="U_UNKNOWN", slack_user_id="U_UNKNOWN", event_team_id=TEAM_ID))

    assert gw.posted == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_decisions")
        assert cur.fetchone()[0] == 0


def test_an_unresolvable_identity_is_silently_declined_on_a_modal_submission(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": "1", "verdict": "reject",
                          "field": "notes", "channel_id": "U_UNKNOWN"})
    state_values = {render.REVIEW_NOTE_MODAL_BLOCK_ID: {
        render.REVIEW_NOTE_MODAL_ACTION_ID: {"value": "a reason"}}}

    _run(review.handle_note_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id="U_UNKNOWN", event_team_id=TEAM_ID))

    assert gw.posted == []


def test_the_submitting_identity_comes_from_the_caller_never_from_private_metadata(env, conn):
    """Even if a modal's `private_metadata` carried a `slack_user_id`-shaped key (an
    older client, a replayed payload), it must have NO effect — identity is resolved from this
    call's OWN `slack_user_id`/`event_team_id` arguments only. Simulated here by an attacker-
    controlled extra key in the metadata naming a DIFFERENT (unresolvable) identity than the real
    caller: the decision must still be attributed to the REAL caller (STEWARD), not silently
    declined because of the metadata's own claim."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    metadata = json.dumps({"item_kind": "entity-proposal", "item_id": str(item_id),
                          "verdict": "reject", "field": "notes",
                          "slack_user_id": "U_SOMEONE_ELSE", "channel_id": STEWARD_SLACK_ID})
    state_values = {render.REVIEW_NOTE_MODAL_BLOCK_ID: {
        render.REVIEW_NOTE_MODAL_ACTION_ID: {"value": "a real reason"}}}

    _run(review.handle_note_modal_submission(
        ctx, private_metadata=metadata, state_values=state_values,
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))

    with conn.cursor() as cur:
        cur.execute("SELECT actor FROM review_decisions WHERE item_id = %s", (str(item_id),))
        (actor,) = cur.fetchone()
    assert actor == STEWARD


def test_a_malformed_modal_metadata_payload_is_logged_and_ignored(env, conn):
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    _run(review.handle_note_modal_submission(
        ctx, private_metadata="{not valid json", state_values={},
        slack_user_id=STEWARD_SLACK_ID, event_team_id=TEAM_ID))
    assert gw.posted == []


def test_a_failed_modal_open_is_logged_not_raised(env, conn):
    gw = FakeSlackGateway()
    gw.fail_views_open_count = 1
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore())

    _run(review.handle_block_action(
        ctx, action_id="review-modal:parked-capture:resolve", value=str(item_id),
        trigger_id="T1", channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID,
        event_team_id=TEAM_ID))   # must not raise

    assert gw.opened_views == []







def test_a_stale_action_id_from_an_older_deploy_is_declined_not_a_crash(env, conn):
    """`_MODAL_FIELD` used to be indexed directly (`_MODAL_FIELD[(kind, verdict)]`), raising
    `KeyError` straight out of the handler for any (kind, verdict) this build no longer maps to a
    modal — reachable from a doorbell card rendered by an older deploy. Must decline gracefully
    and tell the steward, not raise."""
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)

    _run(review.handle_block_action(
        ctx, action_id="review-modal:parked-capture:some-retired-verdict", value="1",
        trigger_id="T1", channel_id=STEWARD_SLACK_ID, slack_user_id=STEWARD_SLACK_ID,
        event_team_id=TEAM_ID))   # must not raise

    assert gw.opened_views == []
    assert len(gw.posted) == 1
    assert "older version of this card" in gw.posted[0].text

