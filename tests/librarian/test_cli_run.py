"""Writer CLI and autonomous loop contract."""

import contextlib
import datetime as dt
from types import SimpleNamespace

import pytest

from stigmergy.capture import schema
from stigmergy.kernel.llm import LIBRARIAN_MODEL, OCR_MODEL
from stigmergy.librarian import cli, config, gitcmd, worker, workspaces
from stigmergy.librarian.errors import LibrarianConfigError


def test_cli_exposes_only_the_long_running_writer():
    assert cli.build_parser().parse_args(["run"]).command == "run"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["once"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--max-attempts", "3"])


def _stub_startup(monkeypatch, events, connection, settings):
    monkeypatch.setattr(cli.config.Settings, "from_args", lambda _args: settings)
    monkeypatch.setattr(cli.store, "connect", lambda _dsn: connection)
    monkeypatch.setattr(cli.worker, "configure_connection",
                        lambda conn: events.append(("configure", conn)))
    monkeypatch.setattr(cli.platform, "ensure_platform_schema",
                        lambda conn: events.append(("platform", conn)))
    monkeypatch.setattr(cli.roster, "ensure_roster_schema",
                        lambda conn: events.append(("roster", conn)))
    monkeypatch.setattr(cli.schema, "ensure_capture_schema",
                        lambda conn: events.append(("capture", conn)))
    monkeypatch.setattr(cli, "ensure_upload_schema", lambda conn: events.append(("uploads", conn)))
    monkeypatch.setattr(cli, "ensure_change_schema", lambda conn: events.append(("changes", conn)))
    monkeypatch.setattr(cli.usage, "ensure_schema", lambda conn: events.append(("usage", conn)))
    monkeypatch.setattr(cli.evidence_plane, "store_from_env", lambda: object())
    monkeypatch.setattr(cli.platform, "list_tenants", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli.provision, "provisioner_from_env", lambda: None)


def test_cli_bounds_database_statements_before_schema_startup(monkeypatch):
    events = []
    connection = object()
    settings = SimpleNamespace(dsn="postgresql://fixture", backend="scripted", repo="/checkouts")
    _stub_startup(monkeypatch, events, connection, settings)
    monkeypatch.setattr(cli.worker, "check_settings", lambda _settings: None)

    class Loop:
        def __init__(self, *_args, **_kwargs):
            pass

        def install_signal_handlers(self):
            pass

        def run(self):
            return 0

    monkeypatch.setattr(cli.worker, "Worker", Loop)

    assert cli.main(["run"]) == 0
    assert events == [
        ("configure", connection),
        ("platform", connection),
        ("roster", connection),
        ("capture", connection),
        ("uploads", connection),
        ("changes", connection),
        ("usage", connection),
    ]


def test_cli_maps_a_startup_configuration_error_to_a_bounded_safe_exit(monkeypatch, capsys):
    """The worker command never turns a configuration failure into a traceback."""
    settings = SimpleNamespace(dsn="postgresql://fixture", backend="pydantic", repo="/checkouts")
    _stub_startup(monkeypatch, [], object(), settings)
    monkeypatch.setattr(cli.worker, "check_settings", lambda _settings: (_ for _ in ()).throw(
        LibrarianConfigError("OPENROUTER_API_KEY is required by the writer")))

    assert cli.main(["run"]) == 2

    stderr = capsys.readouterr().err
    assert stderr.startswith("stigmergy-librarian:")
    assert "Traceback" not in stderr
    assert len(stderr) <= 800


def test_a_tenant_checkout_that_cannot_be_prepared_is_a_retryable_scrubbed_failure(monkeypatch,
                                                                                    tmp_path):
    """A clone or fetch failure for one tenant retries that job later; its message names no
    credential, query secret, or local path."""
    private_path = "/Users/tester/private-knowledge-repo"
    raw_url = ("https://x-access-token:url-userinfo-secret@github.invalid/team/wiki.git?"
               "ToKeN=query-token-secret&password=password-secret")
    raw_stderr = f"fatal: fetch {raw_url} from {private_path}: unrelated-secret-marker"
    monkeypatch.setattr(
        gitcmd.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=128, stdout="", stderr=raw_stderr),
    )
    tenant = SimpleNamespace(id="00000000-0000-4000-8000-0000000000aa", repo="team/wiki")
    places = workspaces.Workspaces(object(), config.Settings(repo=str(tmp_path), backend="scripted"),
                                   object(), url_for=lambda _tenant: raw_url)
    monkeypatch.setattr(places, "tenant", lambda _tenant_id: tenant)

    with pytest.raises(workspaces.CheckoutUnavailable) as raised:
        places.deps_for(tenant.id)

    assert worker._retryable(raised.value) is True
    message = worker._safe_error(raised.value) + str(raised.value)
    assert not any(value in message for value in (
        "url-userinfo-secret", "query-token-secret", "password-secret", private_path,
        "unrelated-secret-marker"))


@pytest.mark.parametrize(
    ("field", "value"),
    (("poll_interval_s", 0), ("visibility_timeout_s", 1)),
)
def test_worker_limits_fail_closed(field, value):
    settings = config.Settings(backend="scripted", **{field: value})
    with pytest.raises(LibrarianConfigError):
        settings.check_domains()


@pytest.mark.parametrize("max_turns", (1, 2, 14))
def test_librarian_rejects_a_nonproduction_request_budget(max_turns):
    with pytest.raises(LibrarianConfigError, match="request budget"):
        config.Settings(max_turns=max_turns).check_domains()


def test_librarian_request_budget_allows_one_filing_and_one_correction():
    assert config.DEFAULT_MAX_TURNS == 3
    config.Settings(max_turns=3).check_domains()


def test_visibility_budget_covers_extraction_model_and_gates():
    operation = config.operation_budget_s(timeout_s=180)
    minimum = config.minimum_visibility_timeout_s(timeout_s=180)

    assert operation == config.CAPTURE_TIMEOUT_S + 180 + config.GATE_BUDGET_S
    assert minimum == operation + config.VISIBILITY_HEADROOM_S
    with pytest.raises(LibrarianConfigError, match="visibility timeout"):
        config.Settings(timeout_s=180, visibility_timeout_s=minimum - 1).check_domains()
    config.Settings(timeout_s=180, visibility_timeout_s=minimum).check_domains()


@pytest.mark.parametrize(
    "model",
    [
        "anthropic:claude-sonnet-5",
        "openrouter:anthropic/claude-sonnet-5",
        "openai:gpt-5.6-terra",
    ],
)
def test_librarian_rejects_unapproved_models(model):
    with pytest.raises(LibrarianConfigError, match="librarian model"):
        config.Settings(model=model).check_domains()


def test_librarian_accepts_only_the_approved_filing_and_ocr_models(monkeypatch):
    config.Settings(model=LIBRARIAN_MODEL, ocr_model=OCR_MODEL).check_domains()
    with pytest.raises(LibrarianConfigError, match="OCR model"):
        config.Settings(model=LIBRARIAN_MODEL, ocr_model="google-gla:gemini-3-flash").check_domains()
    with pytest.raises(LibrarianConfigError, match="OCR model"):
        config.Settings(model=LIBRARIAN_MODEL, ocr_model=None).check_domains()

    monkeypatch.delenv("STIGMERGY_OCR_MODEL", raising=False)
    assert config.Settings.from_args(SimpleNamespace()).ocr_model == OCR_MODEL
    monkeypatch.setenv("STIGMERGY_OCR_MODEL", "")
    with pytest.raises(LibrarianConfigError, match="OCR model"):
        config.Settings.from_args(SimpleNamespace())


def test_worker_reports_each_operation_and_stops_after_the_active_one(monkeypatch):
    settings = SimpleNamespace(
        visibility_timeout_s=600,
        max_attempts=3,
        poll_interval_s=0.01,
        garden_at="off",
        rebuild_at="off",
    )
    loop = worker.Worker(object(), SimpleNamespace(settings=settings, evidence=object()))
    monkeypatch.setattr(worker.tenancy, "platform_scope", lambda _conn: contextlib.nullcontext())
    output = []
    loop.on_output = output.append

    monkeypatch.setattr(worker.ops, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker.queue, "release_expired", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.erasure, "pending", lambda _conn: False)
    monkeypatch.setattr(worker.erase_workspace, "pending", lambda _conn: False)

    def process_once(*args):
        loop.stopping = True
        return (
            {"id": "00000000-0000-4000-8000-000000000001"},
            worker.ProcessOutcome(status=schema.LANDED, report={}),
        )

    monkeypatch.setattr(worker, "process_next", process_once)

    assert loop.run() == 1
    assert output == ["#00000000-0000-4000-8000-000000000001 -> landed"]


def test_daily_garden_enters_the_same_queue_once(monkeypatch):
    now = dt.datetime(2026, 8, 24, 5, 8, tzinfo=dt.UTC)
    settings = SimpleNamespace(garden_at="05:07")
    loop = worker.Worker(
        object(),
        SimpleNamespace(settings=settings),
        utcnow=lambda: now,
        on_output=lambda line: None,
    )
    captured = []

    monkeypatch.setattr(worker.schedule, "last_run_at", lambda *args: None)
    monkeypatch.setattr(worker.platform, "list_tenants",
                        lambda *_args, **_kwargs: [SimpleNamespace(id="00000000-0000-4000-8000-0000000000aa")])
    monkeypatch.setattr(worker.tenancy, "tenant_scope", lambda _conn, _tenant: contextlib.nullcontext())

    def enqueue(_conn, request):
        captured.append(request)
        return {"id": "00000000-0000-4000-8000-000000000002", "created": True}

    monkeypatch.setattr(worker.queue, "enqueue_garden", enqueue)

    assert loop._maybe_garden() is True
    assert captured[0].actor.subject == "system:garden"
    assert captured[0].idempotency_key == "garden:scheduled:2026-08-24"


def _idle_worker(monkeypatch, *, pending):
    settings = SimpleNamespace(visibility_timeout_s=600, max_attempts=3, poll_interval_s=0.01,
                               garden_at="off", rebuild_at="off", idle_exit_s=600)
    clock = iter(range(0, 100_000, 400))
    loop = worker.Worker(object(), SimpleNamespace(settings=settings, evidence=None),
                         monotonic=lambda: next(clock), on_output=lambda _line: None)
    beats = []
    monkeypatch.setattr(worker.ops, "heartbeat", lambda _conn, state: beats.append(state))
    monkeypatch.setattr(worker.erasure, "pending", lambda _conn: False)
    monkeypatch.setattr(worker.erase_workspace, "pending", lambda _conn: False)
    monkeypatch.setattr(worker.tenancy, "platform_scope", lambda _conn: contextlib.nullcontext())
    monkeypatch.setattr(worker, "process_next", lambda *args: None)
    monkeypatch.setattr(worker, "_pending_work", lambda _conn: next(pending))
    monkeypatch.setattr(loop, "_sleep", lambda _seconds: None)
    return loop, beats


def test_an_idle_writer_announces_it_is_sleeping_and_stops(monkeypatch):
    loop, beats = _idle_worker(monkeypatch, pending=iter([False]))
    assert loop.run() == 0
    assert beats[-1] == "sleeping"


def test_a_writer_with_work_pending_later_keeps_running_until_nothing_is_left(monkeypatch):
    loop, beats = _idle_worker(monkeypatch, pending=iter([True, False]))
    assert loop.run() == 0
    assert beats.count("sleeping") == 2
    assert beats[beats.index("sleeping") + 1] == "idle"


def test_a_writer_without_an_idle_stop_never_stops_on_its_own(monkeypatch):
    loop, _beats = _idle_worker(monkeypatch, pending=iter([]))
    loop.settings.idle_exit_s = 0
    turns = []

    def sleep(_seconds):
        turns.append(1)
        if len(turns) == 3:
            loop.stopping = True

    monkeypatch.setattr(loop, "_sleep", sleep)
    assert loop.run() == 0 and len(turns) == 3


def test_a_writer_that_slept_through_the_garden_hour_gardens_when_it_wakes(monkeypatch):
    """The writer stops after ten idle minutes and nothing wakes it at 05:07; the daily garden and
    rebuild were once due only within three hours of their time, so a writer woken at noon skipped
    them until it happened to be awake at the right hour."""
    now = dt.datetime(2026, 8, 24, 12, 0, tzinfo=dt.UTC)
    yesterday = dt.datetime(2026, 8, 23, 5, 8, tzinfo=dt.UTC)
    assert worker.schedule.daily_due(now, yesterday, (5, 7)) is True
    assert worker.schedule.daily_due(now, now - dt.timedelta(hours=1), (5, 7)) is False


def test_before_todays_hour_a_missed_run_from_yesterday_is_still_due():
    now = dt.datetime(2026, 8, 24, 2, 0, tzinfo=dt.UTC)
    two_days_ago = dt.datetime(2026, 8, 22, 5, 8, tzinfo=dt.UTC)
    yesterdays_run = dt.datetime(2026, 8, 23, 5, 8, tzinfo=dt.UTC)
    assert worker.schedule.daily_due(now, two_days_ago, (5, 7)) is True
    assert worker.schedule.daily_due(now, yesterdays_run, (5, 7)) is False
