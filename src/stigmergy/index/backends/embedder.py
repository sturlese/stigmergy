"""The real embedder: OpenAI `text-embedding-3-large`, chosen because it measured ES->EN
hit@5 = 1.00 on this corpus — cross-language retrieval is the requirement, not an extra.

`build_embedder` is the one fake/real dispatch: the offline double is imported DEFERRED, only when
selected, so production never loads it and the import graph stays honest.
"""
import os

import httpx

DEFAULT_MODEL = "text-embedding-3-large"
OPENAI_URL = "https://api.openai.com/v1/embeddings"
EMBED_BATCH = 128


class OpenAIEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        # `transport` is the injectable HTTP seam: production passes None (real network), tests
        # pass an httpx.MockTransport so the request/response parsing path runs fully offline.
        self._transport = transport
        if not self._api_key:
            # NOT "use --embedder fake for keyless runs". A query embedded by the fake embedder
            # against an index built with the real one lands in a different vector space: search
            # returns noise and does not fail, which is the one failure mode worse than an error.
            # `embedder_for_model` exists precisely to keep query and index in the same space, so
            # the honest advice is the key — or a whole index rebuilt fake, which is a different
            # index and only useful offline.
            raise RuntimeError(
                "OPENAI_API_KEY is not set, and this index was built with the real embedder. "
                "`--embedder fake` is NOT a substitute: it embeds into a different space, so "
                "search would silently return unrelated results instead of failing. Set "
                "OPENAI_API_KEY, or rebuild the whole index with --embedder fake for an offline "
                "run (`stigmergy-index --rebuild --embedder fake`).")

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        with httpx.Client(timeout=120, transport=self._transport) as client:
            for i in range(0, len(texts), EMBED_BATCH):
                resp = client.post(OPENAI_URL,
                                   headers={"Authorization": f"Bearer {self._api_key}"},
                                   json={"model": self.model, "input": texts[i:i + EMBED_BATCH]})
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                vectors.extend(d["embedding"] for d in data)
        return vectors


def build_embedder(kind: str = "openai", model: str | None = None):
    """'openai' (default) or 'fake'. The fake import is deferred on purpose — production
    modules must never load the offline double (same rule the ingest layering enforces)."""
    if kind == "fake":
        from stigmergy.index.backends.fake_embedder import FakeEmbedder
        return FakeEmbedder()
    if kind == "openai":
        return OpenAIEmbedder(model or DEFAULT_MODEL)
    raise ValueError(f"unknown embedder: {kind!r} (use 'openai' or 'fake')")


def embedder_for_model(model: str):
    """The embedder matching an already-built index's recorded model (index_meta): queries
    must embed in the same space the documents did."""
    if model.startswith("fake"):
        return build_embedder("fake")
    return build_embedder("openai", model)
