"""Writer CLI and autonomous loop contract."""

import datetime as dt
from types import SimpleNamespace

import pytest

from stigmergy.capture import schema
from stigmergy.kernel.llm import LIBRARIAN_MODEL, OCR_MODEL
from stigmergy.librarian import cli, config, worker
from stigmergy.librarian.errors import LibrarianConfigError


def test_cli_exposes_only_the_long_running_writer():
    assert cli.build_parser().parse_args(["run"]).command == "run"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["once"])


def test_cli_bounds_database_statements_before_schema_startup(monkeypatch):
    events = []
    connection = object()
    settings = SimpleNamespace(dsn="postgresql://fixture")
    base = SimpleNamespace(describe=lambda: "origin/main@fixture")

    monkeypatch.setattr(cli.config.Settings, "from_args", lambda _args: settings)
    monkeypatch.setattr(cli.store, "connect", lambda _dsn: connection)
    monkeypatch.setattr(
        cli.worker,
        "configure_connection",
        lambda conn: events.append(("configure", conn)),
    )
    monkeypatch.setattr(
        cli.schema,
        "ensure_capture_schema",
        lambda conn: events.append(("capture", conn)),
    )
    monkeypatch.setattr(
        cli,
        "ensure_upload_schema",
        lambda conn: events.append(("uploads", conn)),
    )
    monkeypatch.setattr(
        cli,
        "ensure_change_schema",
        lambda conn: events.append(("changes", conn)),
    )
    monkeypatch.setattr(cli.worker, "startup_checks", lambda _settings: {"base": base})
    monkeypatch.setattr(cli.worker, "build_deps", lambda *_args: object())
    monkeypatch.setattr(cli.evidence_plane, "store_from_env", lambda: object())

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
        ("capture", connection),
        ("uploads", connection),
        ("changes", connection),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    (("poll_interval_s", 0), ("max_attempts", 0), ("visibility_timeout_s", 1)),
)
def test_worker_limits_fail_closed(field, value):
    settings = config.Settings(backend="scripted", **{field: value})
    with pytest.raises(LibrarianConfigError):
        settings.check_domains()


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
    )
    loop = worker.Worker(object(), SimpleNamespace(settings=settings, evidence=object()))
    output = []
    loop.on_output = output.append

    monkeypatch.setattr(worker.ops, "heartbeat", lambda *args: None)
    monkeypatch.setattr(worker.queue, "release_expired", lambda *args, **kwargs: None)

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

    def enqueue(_conn, request):
        captured.append(request)
        return {"id": "00000000-0000-4000-8000-000000000002", "created": True}

    monkeypatch.setattr(worker.queue, "enqueue_garden", enqueue)

    assert loop._maybe_garden() is True
    assert captured[0].actor.subject == "system:garden"
    assert captured[0].idempotency_key == "garden:scheduled:2026-08-24"
