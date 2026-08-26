"""MCP tools shared by the stdio and authenticated HTTP transports."""
import argparse
import asyncio
import functools
import json
import logging
import sys

import psycopg
from psycopg.conninfo import conninfo_to_dict

from stigmergy.capture.errors import CaptureError
from stigmergy.index.errors import StigmergyIndexError
from stigmergy.kernel.blocking import run_blocking
from stigmergy.server.errors import (
    ArgumentLengthError,
    CapabilityUnavailableError,
    RateLimitError,
    StartupError,
    StigmergyServerError,
)
from stigmergy.server.service import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_SUBMISSION_LIMIT,
    BrainService,
    build_service,
    check_arg_length,
)
from stigmergy.server.settings import Settings

_DUMP = {"ensure_ascii": False, "indent": 1}

log = logging.getLogger(__name__)


def _error(message: str) -> str:
    """Return the stable tool error shape."""
    return json.dumps({"error": message}, **_DUMP)


def _failure(tool: str, ex: Exception, hint: str = "") -> str:
    """Hide unanticipated exception details from tool callers."""
    return _error(f"{tool} failed ({ex.__class__.__name__}){hint}")


def build_mcp(service: BrainService, *, stateless_http: bool = False, transport_security=None,
              json_response: bool = False):
    """Build the shared tool surface with transport-specific FastMCP settings."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("stigmergy-brain", stateless_http=stateless_http,
                  transport_security=transport_security, json_response=json_response)

    @mcp.tool()
    async def search_brain(query: str, filters: dict | None = None,
                           max_results: int = DEFAULT_MAX_RESULTS) -> str:
        """Search visible team knowledge with hybrid lexical and vector ranking."""
        try:
            result = await run_blocking(
                service.search, query, filters=filters, max_results=max_results
            )
            return json.dumps(result, **_DUMP)
        except (ArgumentLengthError, ValueError, StigmergyIndexError, RateLimitError,
                CapabilityUnavailableError) as ex:
            # All server-authored and safe to echo verbatim; `CapabilityUnavailableError`
            # especially — collapsed to a class name it would read as an unexplained outage
            # instead of naming the missing capability.
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001 — class name only: an unanticipated str(ex) may
            # carry a DSN fragment, a path or another internal detail.
            return _failure("search_brain", ex)

    @mcp.tool()
    async def read_page(path: str) -> str:
        """Read one visible wiki or source page with its links and citations."""
        try:
            return json.dumps(await run_blocking(service.read_page, path), **_DUMP)
        except (ArgumentLengthError, RateLimitError) as ex:
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001 — no bare `except ValueError`: it would also catch
            # a `pydantic_core.ValidationError` (a ValueError subclass) whose message can carry
            # untrusted LLM output or internal field paths.
            return _failure("read_page", ex)

    @mcp.tool()
    async def list_entities() -> str:
        """List entity identities with a name claim visible to the caller."""
        try:
            return json.dumps(await run_blocking(service.list_entities), **_DUMP)
        except RateLimitError as ex:
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001 — class name only: a malformed entity registry
            # raises ValueError here and its str(ex) can carry a filesystem path.
            return _failure("list_entities", ex)

    @mcp.tool()
    async def describe_entity(entity: str) -> str:
        """Compose visible knowledge for one entity ID, name, or alias."""
        try:
            return json.dumps(await run_blocking(service.describe_entity, entity), **_DUMP)
        except (ArgumentLengthError, RateLimitError) as ex:
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001 — same narrowing as read_page above
            return _failure("describe_entity", ex)

    @mcp.tool()
    async def brain_submit(
        text: str | None = None,
        path: str | None = None,
        url: str | None = None,
        title: str | None = None,
        occurred_at: str | None = None,
        audience: list[str] | None = None,
        resolution_of: str | None = None,
    ) -> str:
        """Capture exactly one text, local path, or URL through one filing flow.

        The cloud endpoint accepts text and public URLs. A local path or a private Drive URL
        requires the official local Stigmergy bridge, which uploads verified bytes without
        sending local or Google credentials to Stigmergy. Omitted audience uses the authenticated
        principal's configured default. When ``resolution_of`` names a contradiction, the writer
        may resolve only that target after the normal evidence and ACL gates pass.
        """
        try:
            return json.dumps(
                await run_blocking(
                    functools.partial(
                        service.submit,
                        text=text,
                        path=path,
                        url=url,
                        title=title,
                        occurred_at=occurred_at,
                        audience=audience,
                        resolution_of=resolution_of,
                    )
                ),
                **_DUMP)
        except (CaptureError, RateLimitError) as ex:
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001
            return _failure("brain_submit", ex)

    @mcp.tool()
    async def brain_submissions(limit: int = DEFAULT_SUBMISSION_LIMIT, status: str = "") -> str:
        """List capture progress as queued, processing, landed, or failed.

        Members see their own submissions. The unrestricted identity sees all submissions.
        Artifact metadata is returned without document content.
        """
        try:
            return json.dumps(
                await run_blocking(service.submissions, limit=limit, status=status or None),
                **_DUMP,
            )
        except (ValueError, CaptureError, RateLimitError) as ex:
            # ValueError here is the unknown-status rejection — the caller's own value plus a
            # static status list, safe to echo.
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001 — class name only
            return _failure("brain_submissions", ex)

    @mcp.tool()
    async def brain_delete(paths: list[str], why: str) -> str:
        """Queue an explicit page/source deletion and reference sweep.

        An unrestricted identity must provide repository-relative paths and a rationale. The
        single writer applies normal gates and records one commit and change entry.
        """
        try:
            return json.dumps(
                await run_blocking(
                    functools.partial(service.delete_pages, paths, why, source="mcp")),
                **_DUMP)
        except (ArgumentLengthError, CaptureError, RateLimitError, CapabilityUnavailableError) as ex:
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001 — only typed safe errors are echoed.
            return _failure("brain_delete", ex)

    @mcp.tool()
    async def ask(question: str) -> str:
        """Answer from visible team knowledge with cited, verified evidence."""
        from stigmergy.answer.service import AnswerService, audit_summary

        async def run(actual_service):
            # Length-checked inside the rate-limited/audited call, before the expensive work.
            check_arg_length("question", question)
            # `ask` searches, so it needs `search_brain`'s capability — asserted HERE because
            # `ask` lives one layer up and `service.py` may never import `stigmergy.answer`;
            # before the agent, so a keyless server refuses in milliseconds.
            actual_service.require_embedder()
            return await AnswerService(actual_service).ask(question)

        def run_sync(actual_service):
            return asyncio.run(
                actual_service.call_async(
                    "ask", {"question": question}, lambda: run(actual_service),
                    summarize=audit_summary,
                )
            )

        try:
            # `summarize=audit_summary`: the same summary the Slack transport writes, so
            # `audit_log.result` means one thing for `ask` whichever transport called it.
            if hasattr(service, "run_scoped"):
                result = await run_blocking(service.run_scoped, run_sync)
            else:
                result = await run_blocking(run_sync, service)
            # `usage` is operator telemetry, already recorded by `audit_summary` — the tool's
            # documented response shape does not grow a field for it.
            result.pop("usage", None)
            return json.dumps(result, **_DUMP)
        except (ArgumentLengthError, RateLimitError, CapabilityUnavailableError) as ex:
            return _error(str(ex))
        except Exception as ex:  # noqa: BLE001 — never a traceback or a raw message (which can
            # echo untrusted content): class name + a fixed actionable hint only.
            return _failure("ask", ex, "; check ANSWER_LLM / OPENROUTER_API_KEY and that the index "
                                       "is built")

    return mcp


def _dsn_location(dsn: str | None) -> str:
    """Return a credential-free database location for startup errors."""
    try:
        parts = conninfo_to_dict(dsn or "")
    except psycopg.Error:
        return "the configured DSN (--dsn / $STIGMERGY_INDEX_DSN)"
    host, port, dbname = parts.get("host"), parts.get("port"), parts.get("dbname")
    if not host and not dbname:
        return "the configured DSN (--dsn / $STIGMERGY_INDEX_DSN)"
    where = f"{host}:{port}" if port else (host or "?")
    return f"{where}/{dbname or '?'}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="stigmergy-server",
        description="Stigmergy MCP server (stdio or HTTP): the read/answer tools over the hybrid "
                    "index, plus the capture surface over the durable write queue.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio (default; one process = one --identity) or http "
                             "(multi-user, per-request identity from a bearer token)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="HTTP bind host (--transport http only; default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP bind port (--transport http only; default: 8080)")
    parser.add_argument("--identity", default=None,
                        help="the identity to serve as (fallback: $STIGMERGY_IDENTITY; "
                             "--transport stdio only — http resolves identity per request)")
    parser.add_argument("--identities", default=None,
                        help="path to identities.json (default: <repo>/ops/identities.json)")
    parser.add_argument("--repo", default=None,
                        help="knowledge-repo checkout (defaults --identities)")
    parser.add_argument("--entity-registry", dest="entity_registry", default=None,
                        help="path to entity-registry.json for entity-first resolution "
                            "(default: <repo>/ops/entity-registry.json) — like "
                            "--identities, an explicit path is needed in production, "
                            "where no --repo is passed at all")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (default: $STIGMERGY_INDEX_DSN)")
    parser.add_argument("--embedder", choices=["openrouter", "fake"], default=None,
                        help="query embedder (default: match the index's built model)")
    parser.add_argument(
        "--answer-llm",
        dest="answer_llm",
        choices=["openrouter", "fake"],
        default=None,
        help="the `ask` synthesizer backend (default: $ANSWER_LLM, else openrouter)",
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_args(args)
        # fail fast on an ANSWER_LLM typo — a bad value must never reach a live `ask` call
        if settings.llm not in ("openrouter", "fake"):
            raise StartupError(
                f"invalid ANSWER_LLM: {settings.llm!r} (use 'openrouter' or 'fake')"
            )
        if args.transport == "http":
            # identity resolves PER REQUEST for HTTP, never at startup — settings.identity unused
            from stigmergy.server.transport_http import serve_http
            serve_http(settings, args.host, args.port)
            return 0
        service = build_service(settings)
    except (StigmergyServerError, StigmergyIndexError) as ex:
        # actionable message, non-zero exit, no traceback
        print(f"stigmergy-server: {ex}", file=sys.stderr)
        return 2
    except psycopg.Error as ex:
        # DB-side startup failure: no traceback, name WHERE the index lives and the fix — never
        # the raw DSN (it commonly embeds a password and this line lands in client/cron logs).
        print(f"stigmergy-server: cannot read the index at {_dsn_location(settings.dsn)} "
              f"({ex.__class__.__name__}); is Postgres up and the index built? "
              f"run `stigmergy-index --rebuild --repo <dir>`", file=sys.stderr)
        return 2

    build_mcp(service).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
