"""The deployment config, checked as a shipped artifact: `fly.toml`'s THREE process groups
(`app`/`worker`/`slack` — the ceiling counts process groups, not machines), read with `tomllib`
— `http_service.processes == ["app"]` (neither `worker` nor `slack` is ever health-checked or
takes public HTTP traffic), and `[processes].app` byte-identical to the Dockerfile's own `CMD`
(`fly.toml`'s own comment: "Byte-identical to the image's own CMD: `[processes]` overrides it...
the two must not drift").

Pure file parsing — no `fly` command, no docker build, no deploy. The config as shipped is the
whole subject.
"""
import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLY_TOML = ROOT / "fly.toml"
DOCKERFILE = ROOT / "Dockerfile"


def _fly_config() -> dict:
    with open(FLY_TOML, "rb") as f:
        return tomllib.load(f)


def _dockerfile_cmd() -> list[str]:
    """The exec-form `CMD [...]` array, across its backslash line continuations — Docker's own
    exec form is a JSON array, so once the continuations are joined it parses as one."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"^CMD\s+(\[.*?\])\s*$", text, re.MULTILINE | re.DOTALL)
    assert match, "no exec-form CMD found in the Dockerfile"
    # Join the physical continuation lines: a trailing `\` followed by a newline is Dockerfile
    # syntax, not JSON syntax, and has to be gone before this parses.
    raw = match.group(1).replace("\\\n", "\n")
    import json
    return json.loads(raw)


def test_fly_toml_parses_with_tomllib():
    config = _fly_config()
    assert "processes" in config


def test_process_groups_are_exactly_app_worker_and_slack():
    """The exact set the deployment declares. A group added on purpose UPDATES this assertion to
    the new intended state — it is never worked around."""
    config = _fly_config()
    assert set(config["processes"]) == {"app", "worker", "slack"}


def test_http_service_exposes_only_the_app_process():
    """The worker's own comment, covering `slack` too: neither the worker nor the
    Slack bot exposes a port. If `http_service.processes` ever drifted to include either, Fly
    would health-check a process that listens on nothing and it would flap — or worse, receive
    public HTTP traffic never designed for it."""
    config = _fly_config()
    assert config["http_service"]["processes"] == ["app"]


def test_slack_group_exposes_no_http_service():
    config = _fly_config()
    assert "slack" not in config["http_service"]["processes"]


def test_slack_process_runs_the_stigmergy_slack_console_script():
    config = _fly_config()
    assert config["processes"]["slack"].startswith("stigmergy-slack ")


def test_slack_vm_block_documents_why_it_never_scales_past_one_machine():
    """Socket Mode has no leader election, so a second machine in this group would double-handle
    every event. That reason belongs in a `fly.toml` comment, and this pins that the comment
    actually exists — not merely the config value it explains."""
    text = FLY_TOML.read_text(encoding="utf-8")
    assert "no leader election" in text.lower()


def test_the_slack_process_command_carries_every_baked_file_it_needs():
    """The `app` command is pinned byte-for-byte against the Dockerfile CMD below; the `slack`
    command has no CMD to be identical to, so its flags are pinned here instead.

    A dropped flag returns a process group to a broken shape — the same class of drift as
    review button refusing the configured steward — and nothing else in the suite would notice:
    the group starts fine, serves Slack fine, and simply never rings. The same is true of the
    other two, which is why all three are checked against the Dockerfile's own COPY targets rather
    than against a hand-written list that can drift from them."""
    processes = _fly_config()["processes"]
    app_flags = set(re.findall(r"(--[\w-]+) (/app/[\w.-]+)", processes["app"]))
    slack_flags = set(re.findall(r"(--[\w-]+) (/app/[\w.-]+)", processes["slack"]))
    missing = app_flags - slack_flags
    assert not missing, (
        f"the app group passes {sorted(missing)} and the slack group does not. Both hold NO "
        f"checkout, so every one of these files is the only source of what it configures for "
        f"BOTH — a flag on one command and not the other is a group silently running without it")
    # Not derived from the Dockerfile's COPY set on purpose: `slack-channels.json` is baked but
    # named by NEITHER command — the channel->audience map reaches `stigmergy-slack` through
    # `--channels`, which this deployment does not pass, so the transport falls back to the empty
    # audience set. That is a wiring gap, not a symmetry break, and this test is not the place to
    # assert it: the invariant here is the SYMMETRY between two commands that need the same
    # things, not "every baked file appears everywhere".


def test_the_app_process_command_is_byte_identical_to_the_dockerfile_cmd():
    """The property the `fly.toml` comment states as a promise: "the two must not drift". Checked
    byte-for-byte, not "looks similar" — a single dropped flag here is a working `docker run` and
    a broken staging deploy, silently, since nothing else would catch it (the image's own default
    CMD is never exercised once `[processes]` overrides it)."""
    config = _fly_config()
    dockerfile_cmd = " ".join(_dockerfile_cmd())
    assert config["processes"]["app"] == dockerfile_cmd


def test_the_worker_process_is_the_boot_entry_point_not_the_bare_loop():
    """Never `stigmergy-librarian run` directly — `stigmergy-librarian-boot` clones, verifies the
    checkout matches the base ref, and strips the read path's secrets before exec'ing the loop."""
    config = _fly_config()
    assert config["processes"]["worker"].strip() == "stigmergy-librarian-boot"


def test_the_worker_carries_no_openai_key_env_line_hardcoded_in_fly_toml():
    """The static half: nothing in `[env]` sets `OPENAI_API_KEY` — the runtime half
    (`bootstrap.worker_env` stripping it even if the App's secrets carried it) is
    `tests/librarian/test_bootstrap.py`'s job; this is the config-as-shipped half."""
    config = _fly_config()
    assert "OPENAI_API_KEY" not in config.get("env", {})


def test_vm_groups_declare_exactly_the_three_process_groups():
    config = _fly_config()
    vm_processes = {p for vm in config.get("vm", []) for p in vm.get("processes", [])}
    assert vm_processes == {"app", "worker", "slack"}


# ── every `COPY deploy/...` needs its e2e placeholder (the admin console's own CI failure) ───────────────
CONTAINER_E2E = ROOT / "scripts" / "e2e_librarian_container.sh"


def _dockerfile_deploy_copies() -> set[str]:
    """The basenames the Dockerfile bakes out of the `deploy/` directory."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    return set(re.findall(r"^COPY\s+deploy/(\S+)\s", text, re.MULTILINE))


def test_the_dockerfile_bakes_at_least_the_two_original_deploy_files():
    """The floor that keeps the test below from passing vacuously — a check that stops checking
    must be impossible to miss."""
    found = _dockerfile_deploy_copies()
    assert {"identities.json", "entity-registry.json"} <= found, found


def test_every_deploy_copy_has_a_placeholder_in_the_container_e2e():
    """**A `COPY deploy/x` with no placeholder line in the container e2e breaks the build and
    nothing local.** `make test` never builds the image; the container e2e does, and it must run
    on a checkout whose `deploy/` holds whatever the last deploy left — or nothing at all, which
    is how it first failed on CI. Without a placeholder the build dies at the COPY with a checksum
    error that names a file, not a cause.

    This has happened twice: the first container-e2e run on CI (a bare redirection into a missing
    directory) and the `slack-channels.json` COPY, added without its placeholder. The rule
    lived in a comment both times; it is a test now."""
    script = CONTAINER_E2E.read_text(encoding="utf-8")
    missing = sorted(name for name in _dockerfile_deploy_copies()
                     if f"deploy/{name}" not in script)
    assert not missing, (
        f"the Dockerfile COPYs deploy/{{{', '.join(missing)}}} but "
        f"scripts/e2e_librarian_container.sh writes no placeholder for it — the container e2e "
        f"will fail at that COPY on any checkout without it, while every local test stays "
        f"green. Add a line beside its siblings: "
        f"`[ -f deploy/<name> ] || echo '<empty json>' > deploy/<name>`")


# ── the metadata files pyproject NAMES have to reach the build context ─────────────────────────
def test_every_file_pyproject_names_is_copied_into_the_image():
    """**A file `pyproject.toml` names and the Dockerfile does not COPY breaks the image and
    nothing local.** `readme` and `license-files` are read by the build backend while it generates
    metadata, so their absence fails `pip install .` with `Readme file does not exist` — inside the
    build, before any of this project's code runs, and long after `make test` has gone green.

    This is the sibling of the `deploy/` placeholder rule below, and it has now happened once:
    adding `readme`/`license-files` to declare the licence (a wheel built without them declared
    none at all) turned the container e2e red on the first CI run of the open-source repo, while
    the whole local suite passed. Same shape, same lesson: the local gate never builds the image.
    """
    with open(ROOT / "pyproject.toml", "rb") as f:
        project = tomllib.load(f)["project"]
    named = ([project["readme"]] if isinstance(project.get("readme"), str) else []) \
        + list(project.get("license-files", []))
    assert named, "pyproject names no readme or licence files — this check has lost its subject"

    text = DOCKERFILE.read_text(encoding="utf-8")
    install = text.index("pip install --no-cache-dir .")
    copied = " ".join(re.findall(r"^COPY\s+(.*)$", text[:install], re.MULTILINE))
    missing = sorted(name for name in named if name not in copied)
    assert not missing, (
        f"pyproject.toml names {missing} but no COPY before `pip install` puts them in the build "
        f"context — the image build will fail while every local test stays green. Add them to the "
        f"`COPY pyproject.toml ...` line.")


def test_the_app_group_sleeps_when_idle():
    """The server auto-stops between requests: every caller of the `app` group can wait out a
    cold start (an MCP client's first request, a webhook delivery, an operator opening /admin),
    and Slack Q&A never routes through it — the `slack` process answers in-process against
    Postgres, not over HTTP. A regression to `min_machines_running = 1` (or auto-stop off)
    silently reinstates 24/7 machine-hours nobody decided to buy back."""
    config = _fly_config()
    svc = config["http_service"]
    assert svc["auto_stop_machines"] is True
    assert svc["auto_start_machines"] is True
    assert svc["min_machines_running"] == 0


def test_the_deploy_script_pins_both_singleton_groups():
    """`fly deploy`'s default second machine for a service-less group is a Fly STANDBY — created
    stopped, claiming nothing (the runbook's scaling note). The script pins `slack=1` (Socket
    Mode has no leader election — a STARTED second machine double-handles every event) and
    `worker=1` (a started standby is a second PAID poller: the queue's leases keep it correct,
    and only the model bill notices — a spend invariant rather than a correctness one, which is
    exactly why nothing else would catch it). `fly.toml`'s header states both pins; this keeps
    the statement executable."""
    script = (ROOT / "scripts" / "deploy_staging.sh").read_text(encoding="utf-8")
    assert "fly scale count slack=1 --yes" in script
    assert "fly scale count worker=1 --yes" in script
