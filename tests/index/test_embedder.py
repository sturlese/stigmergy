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
    with pytest.raises(RuntimeError) as exc_info:
        OpenAIEmbedder()
    message = str(exc_info.value)

    assert "use --embedder fake for keyless runs" not in message
    assert "OPENAI_API_KEY" in message
    # it says WHY fake is not a substitute, and what the real alternative is
    assert "different space" in message
    assert "rebuild the whole index" in message
    assert "stigmergy-index --rebuild --embedder fake" in message
