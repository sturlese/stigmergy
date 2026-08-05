"""Runtime configuration for `stigmergy-slack`, read from the environment ONLY — a process never
loads configuration from the knowledge repo it operates on. The three Slack secrets, plus the same
`stigmergy.server.settings.Settings` every other transport builds from
`--repo`/`--identities`/`--entity-registry`/`--dsn`/`--embedder`/`--answer-llm`, so the Slack
process reads exactly the same identities file, registry and index the other two transports do.

Same ground rule as `stigmergy.server.settings`: nothing here is read at import time —
`SlackSettings.from_args` is the one place flags and environment variables are consulted, called
once from `stigmergy.slack.app.main`.
"""
import os
from dataclasses import dataclass

from stigmergy.server.errors import StartupError
from stigmergy.server.settings import Settings
from stigmergy.slack import channels

APP_TOKEN_ENV = "SLACK_APP_TOKEN"          # xapp-... (Socket Mode)
BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"          # xoxb-...
TEAM_ID_ENV = "SLACK_TEAM_ID"              # the one workspace this bot answers in


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise StartupError(
            f"${name} is not set — stigmergy-slack never starts without it. Slack credentials come "
            "from the environment, never from the knowledge repo.")
    return value


@dataclass(frozen=True)
class SlackSettings:
    app_token: str
    bot_token: str
    team_id: str
    channels_path: str
    server: Settings

    @classmethod
    def from_args(cls, args) -> "SlackSettings":
        repo = getattr(args, "repo", None)
        channels_path = getattr(args, "channels", None) or channels.default_path(repo)
        return cls(
            app_token=_require_env(APP_TOKEN_ENV),
            bot_token=_require_env(BOT_TOKEN_ENV),
            team_id=_require_env(TEAM_ID_ENV),
            channels_path=channels_path,
            server=Settings.from_args(args),
        )


def no_link_resolver(path: str) -> str | None:
    """The link resolver every citation gets: "no link", so the renderer's affordance for that
    case (`Show it here`) is what every citation shows. Wired in production at
    `stigmergy.slack.app.build_context`. A future browsable surface replaces THIS VALUE where it is
    wired, never `stigmergy.slack.render`'s own contract."""
    return None
