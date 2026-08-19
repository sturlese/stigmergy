"""The real embedder: any OpenAI-compatible `/embeddings` host — OpenAI itself by default,
serving `text-embedding-3-large` because cross-language retrieval is the requirement (ES->EN
hit@5 measured 1.00 on this corpus). `build_embedder` is the one fake/real dispatch; the offline
double is imported DEFERRED so production never loads it.
"""
import os

import httpx

DEFAULT_MODEL = "text-embedding-3-large"
# The HOST is configuration, the request shape is not: every embedding host worth pointing at
# (OpenRouter, a self-hosted server, OpenAI itself) speaks OpenAI's `/embeddings` dialect, so one
# client with a movable base URL covers them all. `$EMBED_BASE_URL` is a BASE — `/embeddings` is
# appended here — so the variable cannot be misquoted into a full endpoint.
DEFAULT_BASE_URL = "https://api.openai.com/v1"
BASE_URL_ENV = "EMBED_BASE_URL"
API_KEY_ENV = "EMBED_API_KEY"
MODEL_ENV = "EMBED_MODEL"
EMBED_BATCH = 128


class OpenAIEmbedder:
    """OpenAI-DIALECT embedder, whatever the host: the class name and `build_embedder`'s
    'openai' kind name the request/response shape, not the company billed."""

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None, base_url: str | None = None):
        # An EXPLICIT model always beats `$EMBED_MODEL`: `embedder_for_model` passes the index's
        # own recorded model, and a query embedded per-env against an index built per-flag would
        # land in a different vector space without ever failing. The env is the BUILD-time
        # default only.
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        base = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        self._url = base.rstrip("/") + "/embeddings"
        # `$EMBED_API_KEY` wins so a non-OpenAI host never silently rides the OpenAI credential;
        # `$OPENAI_API_KEY` stays the fallback because it is what every existing deployment sets.
        self._api_key = (api_key or os.environ.get(API_KEY_ENV)
                         or os.environ.get("OPENAI_API_KEY"))
        # the injectable HTTP seam: production passes None (real network), tests an
        # httpx.MockTransport
        self._transport = transport
        if not self._api_key:
            # Never suggest the fake as a keyless substitute: a query embedded by the fake against
            # an index built real lands in a different vector space — search returns noise and
            # does not fail, the one failure mode worse than an error.
            raise RuntimeError(
                "neither EMBED_API_KEY nor OPENAI_API_KEY is set, and this index was built with "
                "the real embedder. `--embedder fake` is NOT a substitute: it embeds into a "
                "different space, so search would silently return unrelated results instead of "
                "failing. Set the key this embedder's host reads (OPENAI_API_KEY for the default "
                "host; EMBED_API_KEY for an EMBED_BASE_URL one), or rebuild the whole index with "
                "--embedder fake for an offline run (`stigmergy-index --rebuild --embedder "
                "fake`).")

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        with httpx.Client(timeout=120, transport=self._transport) as client:
            for i in range(0, len(texts), EMBED_BATCH):
                resp = client.post(self._url,
                                   headers={"Authorization": f"Bearer {self._api_key}"},
                                   json={"model": self.model, "input": texts[i:i + EMBED_BATCH]})
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                vectors.extend(d["embedding"] for d in data)
        return vectors


def build_embedder(kind: str = "openai", model: str | None = None):
    """'openai' (default — any OpenAI-compatible host, see `$EMBED_BASE_URL`) or 'fake'. The fake
    import is deferred on purpose — production modules must never load the offline double."""
    if kind == "fake":
        from stigmergy.index.backends.fake_embedder import FakeEmbedder
        return FakeEmbedder()
    if kind == "openai":
        return OpenAIEmbedder(model)
    raise ValueError(f"unknown embedder: {kind!r} (use 'openai' or 'fake')")


def embedder_for_model(model: str):
    """The embedder matching an already-built index's recorded model (index_meta): queries
    must embed in the same space the documents did."""
    if model.startswith("fake"):
        return build_embedder("fake")
    return build_embedder("openai", model)
