"""Official local MCP bridge entry point."""

from __future__ import annotations

import argparse
import functools
import json
import os

import anyio.to_thread

from stigmergy.bridge.acquire import Acquirer
from stigmergy.bridge.cloud import BridgeError, CloudClient
from stigmergy.bridge.drive import DriveClient

_DUMP = {"ensure_ascii": False, "indent": 1}


def _error(error: Exception) -> str:
    return json.dumps({"error": str(error)}, **_DUMP)


def build_mcp(cloud: CloudClient, acquirer: Acquirer):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("stigmergy-local")

    @mcp.tool()
    async def search_brain(query: str, filters: dict | None = None, max_results: int = 5) -> str:
        """Search ACL-visible team knowledge with hybrid ranking."""
        return await cloud.call_tool(
            "search_brain",
            {"query": query, "filters": filters, "max_results": max_results},
        )

    @mcp.tool()
    async def read_page(path: str) -> str:
        """Read one ACL-visible wiki page."""
        return await cloud.call_tool("read_page", {"path": path})

    @mcp.tool()
    async def list_entities() -> str:
        """List entity identities visible to the current reader."""
        return await cloud.call_tool("list_entities", {})

    @mcp.tool()
    async def describe_entity(entity: str) -> str:
        """Compose visible knowledge anchored to one entity identity."""
        return await cloud.call_tool("describe_entity", {"entity": entity})

    @mcp.tool()
    async def ask(question: str) -> str:
        """Answer from ACL-visible team knowledge with citations."""
        return await cloud.call_tool("ask", {"question": question})

    @mcp.tool()
    async def brain_submit(
        text: str | None = None,
        path: str | None = None,
        url: str | None = None,
        title: str | None = None,
        occurred_at: str | None = None,
        audience: list[str] | None = None,
    ) -> str:
        """Submit exactly one text value, local file path, or URL to the team wiki."""
        present = [name for name, value in (("text", text), ("path", path), ("url", url)) if value is not None]
        if len(present) != 1:
            return _error(BridgeError("provide exactly one of text, path, or url"))
        try:
            if text is not None:
                artifact = await anyio.to_thread.run_sync(acquirer.text, text)
            elif path is not None:
                artifact = await anyio.to_thread.run_sync(acquirer.path, path)
            else:
                artifact = await anyio.to_thread.run_sync(acquirer.url, url or "")
            receipt = await anyio.to_thread.run_sync(
                functools.partial(
                    cloud.submit_artifacts,
                    [artifact],
                    title=title,
                    occurred_at=occurred_at,
                    audience=audience,
                )
            )
            return json.dumps(receipt, **_DUMP)
        except BridgeError as error:
            return _error(error)
        except Exception as error:  # noqa: BLE001
            return json.dumps({"error": f"submission failed ({error.__class__.__name__})"}, **_DUMP)

    @mcp.tool()
    async def brain_submissions(limit: int = 20, status: str = "") -> str:
        """List queued, processing, landed, or failed submissions."""
        return await cloud.call_tool("brain_submissions", {"limit": limit, "status": status})

    @mcp.tool()
    async def brain_delete(paths: list[str], why: str) -> str:
        """Queue an authorized explicit page or source deletion."""
        return await cloud.call_tool("brain_delete", {"paths": paths, "why": why})

    return mcp


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="stigmergy-bridge")
    parser.add_argument("--url", default=os.environ.get("STIGMERGY_URL", ""))
    parser.add_argument(
        "--google-client-secrets",
        default=os.environ.get("STIGMERGY_GOOGLE_CLIENT_SECRETS", ""),
    )
    args = parser.parse_args(argv)
    token = os.environ.get("STIGMERGY_TOKEN", "")
    try:
        cloud = CloudClient(args.url, token)
        drive = DriveClient(args.google_client_secrets) if args.google_client_secrets else None
        build_mcp(cloud, Acquirer(drive)).run()
        return 0
    except BridgeError as error:
        parser.error(str(error))
