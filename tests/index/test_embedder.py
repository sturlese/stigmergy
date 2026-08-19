"""Embedder backends: the deterministic double and the fake/real dispatch."""
import json
import math

import httpx
import pytest

from stigmergy.index.backends.embedder import OpenAIEmbedder, build_embedder, embedder_for_model


def test_fake_embedder_is_deterministic():
    e = build_embedder("fake")
    (v1,), (v2,) = e.embed(["hola mundo"]), e.embed(["hola mundo"])
    assert v1 == v2


def test_fake_embedder_dim_and_normalization():
    e = build_embedder("fake")
    (vec,) = e.embed(["some text to embed"])
    assert len(vec) == 256
    assert abs(math.sqrt(sum(x * x for x in vec)) - 1.0) < 1e-9


def test_fake_embedder_distinguishes_texts():
    e = build_embedder("fake")
    v1, v2 = e.embed(["revenue quarterly report", "booking lodge deposit"])
    assert v1 != v2


def test_openai_embedder_requires_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_embedder("openai")


def test_unknown_embedder_kind_raises():
    with pytest.raises(ValueError):
        build_embedder("voyage")   # only "fake" and "openai" are dispatched


def test_embedder_for_model_maps_fake_and_real(monkeypatch):
    assert embedder_for_model("fake-hashed-bow-256").model == "fake-hashed-bow-256"
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert embedder_for_model("text-embedding-3-large").model == "text-embedding-3-large"


def test_openai_embedder_http_path_over_a_stub_transport():
    """Exercise the real embedder's request/response parsing fully offline by injecting an
    httpx.MockTransport — no network, no key round-trip. Proves the batching request shape and
    the index-ordered response decode without touching OpenAI."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        payload = json.loads(request.content)
        seen["model"] = payload["model"]
        seen["inputs"] = payload["input"]
        # answer out of order on purpose: the embedder must sort by `index` before extracting
        data = [{"index": 1, "embedding": [0.3, 0.4]}, {"index": 0, "embedding": [0.1, 0.2]}]
        return httpx.Response(200, json={"data": data})

    embedder = OpenAIEmbedder(model="text-embedding-3-large", api_key="sk-test",
                              transport=httpx.MockTransport(handler))
    vectors = embedder.embed(["first", "second"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]          # re-ordered back to input order
    assert seen["auth"] == "Bearer sk-test"
    assert seen["model"] == "text-embedding-3-large"
    assert seen["inputs"] == ["first", "second"]


def test_openai_embedder_raises_for_status_over_a_stub_transport():
    """A non-2xx from the API surfaces as an HTTP error, not a silent empty result."""
    embedder = OpenAIEmbedder(api_key="sk-test",
                              transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed(["x"])


# ── the movable host: EMBED_BASE_URL / EMBED_API_KEY / EMBED_MODEL ────────────────────────────────
def _capturing_handler(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    return handler


def test_embed_base_url_moves_the_host_and_keeps_the_endpoint_path(monkeypatch):
    """OLD BEHAVIOUR: the URL was a module constant, so the embedder could only ever bill OpenAI —
    pointing the index at any other OpenAI-compatible host (OpenRouter, a self-hosted server)
    took a code edit. The env carries a BASE url; `/embeddings` is appended by the class, and a
    trailing slash must not double it."""
    seen = {}
    monkeypatch.setenv("EMBED_BASE_URL", "https://openrouter.ai/api/v1/")
    e = OpenAIEmbedder(api_key="sk-test", transport=httpx.MockTransport(_capturing_handler(seen)))
    e.embed(["x"])
    assert seen["url"] == "https://openrouter.ai/api/v1/embeddings"


def test_the_default_host_is_openai_exactly_as_before(monkeypatch):
    """The benign twin: with no EMBED_BASE_URL set, requests go byte-for-byte where they always
    went — a deployment that configures nothing new must notice nothing new."""
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    seen = {}
    e = OpenAIEmbedder(api_key="sk-test", transport=httpx.MockTransport(_capturing_handler(seen)))
    e.embed(["x"])
    assert seen["url"] == "https://api.openai.com/v1/embeddings"


def test_embed_api_key_wins_and_the_openai_key_stays_the_fallback(monkeypatch):
    """EMBED_API_KEY exists so a non-OpenAI host never silently rides the OpenAI credential; with
    it unset, OPENAI_API_KEY keeps working because it is what every existing deployment sets."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("EMBED_API_KEY", "sk-embed-host")
    seen = {}
    OpenAIEmbedder(transport=httpx.MockTransport(_capturing_handler(seen))).embed(["x"])
    assert seen["auth"] == "Bearer sk-embed-host"

    monkeypatch.delenv("EMBED_API_KEY")
    OpenAIEmbedder(transport=httpx.MockTransport(_capturing_handler(seen))).embed(["x"])
    assert seen["auth"] == "Bearer sk-openai"


def test_an_explicit_model_beats_the_env_so_queries_stay_in_the_indexes_space(monkeypatch):
    """`embedder_for_model` passes the INDEX's recorded model and must win over $EMBED_MODEL: if
    the env won, changing it under a standing index would embed every query into a different
    space than the documents — noise, not an error. The env is the BUILD-time default only."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("EMBED_MODEL", "qwen/qwen3-embedding-8b")
    assert embedder_for_model("text-embedding-3-large").model == "text-embedding-3-large"
    assert build_embedder("openai").model == "qwen/qwen3-embedding-8b"


# ── the keyless message must not name an action that silently produces wrong results ─────────────
def test_the_missing_key_message_never_suggests_the_fake_embedder_as_a_substitute(monkeypatch):
    """It used to read `(use --embedder fake for keyless runs)`.

    Against an index built with the real embedder that advice yields vector noise **without
    failing**: `--embedder fake` embeds the query into a different space, so search returns
    unrelated results and reports success. `embedder_for_model` exists precisely to keep query and
    index in the same space, so a message that tells an operator to break that pairing is worse
    than no message. The honest options are the key, or a whole index rebuilt fake.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        OpenAIEmbedder()
    message = str(exc_info.value)

    assert "use --embedder fake for keyless runs" not in message
    assert "OPENAI_API_KEY" in message
    assert "EMBED_API_KEY" in message   # the other credential this class reads
    # it says WHY fake is not a substitute, and what the real alternative is
    assert "different space" in message
    assert "rebuild the whole index" in message
    assert "stigmergy-index --rebuild --embedder fake" in message
