"""Cloud protocol used by the local bridge."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from stigmergy.capture.schema import AcquisitionProvenance


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AcquiredArtifact:
    data: bytes
    media_type: str
    original_name: str | None
    source_url: str | None
    locator: str | None
    acquisition: AcquisitionProvenance | None


class CloudClient:
    def __init__(self, url: str, token: str, *, client: httpx.Client | None = None) -> None:
        if not url or not token:
            raise BridgeError("STIGMERGY_URL and STIGMERGY_TOKEN are required")
        normalized = url.rstrip("/")
        self.mcp_url = normalized if normalized.endswith("/mcp") else normalized + "/mcp"
        self.api_root = normalized[:-4] if normalized.endswith("/mcp") else normalized
        self.token = token
        self.client = client or httpx.Client(timeout=60)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def submit_artifacts(
        self,
        artifacts: list[AcquiredArtifact],
        *,
        title: str | None,
        occurred_at: str | None,
        audience: list[str] | None,
        resolution_of: str | None = None,
    ) -> dict:
        submission_key = str(uuid.uuid4())
        upload_ids = []
        for position, artifact in enumerate(artifacts):
            digest = __import__("hashlib").sha256(artifact.data).hexdigest()
            response = self.client.post(
                self.api_root + "/bridge/uploads",
                headers=self.headers,
                json={
                    "idempotency_key": f"{submission_key}:{position}",
                    "sha256": digest,
                    "bytes": len(artifact.data),
                    "media_type": artifact.media_type,
                    "original_name": artifact.original_name,
                    "source_url": artifact.source_url,
                },
            )
            upload = self._json(response)
            if upload["upload_url"]:
                put = self.client.put(
                    upload["upload_url"],
                    content=artifact.data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                if put.status_code >= 400:
                    raise BridgeError("evidence upload failed")
            upload_ids.append(upload["upload_id"])
        first = artifacts[0]
        capture_request = {
            "upload_ids": upload_ids,
            "idempotency_key": submission_key,
            "title": title,
            "occurred_at": occurred_at,
            "audience": audience,
            "locator": first.locator,
            "acquisition": (
                first.acquisition.model_dump(mode="json")
                if first.acquisition is not None
                else None
            ),
        }
        if resolution_of is not None:
            capture_request["resolution_of"] = resolution_of
        response = self.client.post(
            self.api_root + "/bridge/captures",
            headers=self.headers,
            json=capture_request,
        )
        return self._json(response)

    async def call_tool(self, name: str, arguments: dict) -> str:
        async with (
            streamablehttp_client(self.mcp_url, headers=self.headers) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
        blocks = [block.text for block in result.content if isinstance(block, TextContent)]
        if len(blocks) == 1:
            return blocks[0]
        return json.dumps({"error": "cloud returned an invalid tool response"})

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError as error:
            raise BridgeError("cloud returned an invalid response") from error
        if response.status_code >= 400:
            message = body.get("error") if isinstance(body, dict) else None
            raise BridgeError(message or "cloud request failed")
        if not isinstance(body, dict):
            raise BridgeError("cloud returned an invalid response")
        return body
