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

    mcp = FastMCP("stigmergy-bridge")

    @mcp.tool()
    async def search_brain(query: str, filters: dict | None = None, max_results: int = 5) -> str:
        """Search ACL-visible team knowledge with hybrid ranking: the most similar pages, not every
        match. To enumerate a kind, a period, or a connection, use list_pages."""
        return await cloud.call_tool(
            "search_brain",
            {"query": query, "filters": filters, "max_results": max_results},
        )

    @mcp.tool()
    async def read_page(path: str) -> str:
        """Read one ACL-visible wiki page; a search hit's `fragment` (`path#n`) reads that part
        of a long page with its neighbours."""
        return await cloud.call_tool("read_page", {"path": path})

    @mcp.tool()
    async def list_pages(type: str | None = None, entity_type: str | None = None,
                         layer: str | None = None, since: str | None = None,
                         until: str | None = None, links_to: str | None = None,
                         sort: str = "recent", cursor: str | None = None) -> str:
        """List every visible page that matches, 50 per call: exhaustive, unlike search_brain,
        which returns only the most similar pages. Use it for "all", "every", "which", a period,
        or everything connected to a page. Filters combine: `type` (note, concept, entity,
        project, source), `entity_type` (person, organization, product, tool, repository,
        place), `layer` (one layer you may read), `since`/`until` (YYYY-MM-DD; a page matches
        when a source it cites occurred in the range, a source on its own date), `links_to` (a
        page path, or an entity title or alias: the pages linking it), each applied to the page
        as you see it. `sort` is `recent` (latest source first), `oldest`, or `title`; pass the
        returned `next` as `cursor` with the same filters."""
        arguments = {"type": type, "entity_type": entity_type, "layer": layer, "since": since,
                     "until": until, "links_to": links_to, "sort": sort, "cursor": cursor}
        return await cloud.call_tool(
            "list_pages", {key: value for key, value in arguments.items() if value is not None})

    @mcp.tool()
    async def describe_entity(entity: str) -> str:
        """Describe one entity page and the ACL-visible pages that link it."""
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
        resolution_of: str | None = None,
    ) -> str:
        """Submit exactly one text value, local file path, or URL to the team wiki.

        ``audience`` names the one layer the capture files in. Omitted, Stigmergy chooses among
        your own layers by their descriptions, or files in your default layer when unsure; it
        never chooses ``public``. The receipt reports ``layer``, ``layer_decision``, and
        ``probabilities`` when a choice was weighed. An explicit ``resolution_of`` target remains
        subject to the cloud writer's evidence and ACL gates.
        """
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
                    resolution_of=resolution_of,
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
