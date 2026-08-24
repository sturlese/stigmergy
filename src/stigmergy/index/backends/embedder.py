"""Qwen embeddings through the approved OpenRouter boundary."""

from __future__ import annotations

import os

import httpx

from stigmergy.kernel.llm import OPENROUTER_PROVIDER_POLICY

DEFAULT_MODEL = "qwen/qwen3-embedding-8b"
DEFAULT_DIMENSIONS = 2560
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
EMBED_BATCH = 128

PROVIDER_POLICY = OPENROUTER_PROVIDER_POLICY

MISSING_KEY_MESSAGE = (
    "OPENROUTER_API_KEY is not set, so Qwen embeddings are unavailable. "
    "Capture remains available; configure the key or rebuild and run the complete index with "
    "the deterministic fake embedder for offline tests."
)


class OpenRouterEmbedder:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model or DEFAULT_MODEL
        if self.model != DEFAULT_MODEL:
            raise RuntimeError(
                f"embedding model is not approved for Stigmergy: {self.model!r}"
            )
        self.host = DEFAULT_BASE_URL
        self._url = f"{self.host}/embeddings"
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self._transport = transport
        if not self._api_key:
            raise RuntimeError(MISSING_KEY_MESSAGE)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        with httpx.Client(timeout=120, transport=self._transport) as client:
            for index in range(0, len(texts), EMBED_BATCH):
                response = client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self.model,
                        "input": texts[index:index + EMBED_BATCH],
                        "dimensions": DEFAULT_DIMENSIONS,
                        "provider": dict(PROVIDER_POLICY),
                    },
                )
                response.raise_for_status()
                batch = sorted(response.json()["data"], key=lambda item: item["index"])
                for item in batch:
                    vector = item["embedding"]
                    if len(vector) != DEFAULT_DIMENSIONS:
                        raise RuntimeError(
                            f"embedding host returned {len(vector)} dimensions; "
                            f"expected {DEFAULT_DIMENSIONS}"
                        )
                    vectors.append(vector)
        return vectors


def build_embedder(kind: str = "openrouter", model: str | None = None):
    if kind == "fake":
        from stigmergy.index.backends.fake_embedder import FakeEmbedder
        return FakeEmbedder()
    if kind == "openrouter":
        return OpenRouterEmbedder(model)
    raise ValueError(f"unknown embedder: {kind!r} (use 'openrouter' or 'fake')")


def embedder_for_model(model: str):
    if model.startswith("fake"):
        return build_embedder("fake")
    return build_embedder("openrouter", model)
