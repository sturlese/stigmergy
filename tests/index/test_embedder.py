import json

import httpx
import pytest

from stigmergy.index.backends.embedder import (
    DEFAULT_BASE_URL,
    DEFAULT_DIMENSIONS,
    DEFAULT_MODEL,
    MISSING_KEY_MESSAGE,
    PROVIDER_POLICY,
    OpenRouterEmbedder,
    build_embedder,
    embedder_for_model,
)
from stigmergy.index.backends.fake_embedder import FakeEmbedder


def _response(request: httpx.Request, dimensions: int = DEFAULT_DIMENSIONS):
    payload = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": index, "embedding": [float(index)] * dimensions}
                for index, _text in reversed(list(enumerate(payload["input"])))
            ]
        },
    )


def test_real_embedder_requires_the_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")

    with pytest.raises(RuntimeError) as exc:
        OpenRouterEmbedder()

    assert str(exc.value) == MISSING_KEY_MESSAGE


def test_real_embedder_uses_only_qwen_and_the_private_provider_policy():
    seen = []

    def handler(request):
        seen.append(request)
        return _response(request)

    embedder = OpenRouterEmbedder(
        api_key="test-key", transport=httpx.MockTransport(handler)
    )

    vectors = embedder.embed(["uno", "two"])

    assert embedder.model == DEFAULT_MODEL
    assert embedder.host == DEFAULT_BASE_URL
    assert [vector[0] for vector in vectors] == [0.0, 1.0]
    request = seen[0]
    assert str(request.url) == f"{DEFAULT_BASE_URL}/embeddings"
    assert request.headers["authorization"] == "Bearer test-key"
    payload = json.loads(request.content)
    assert payload == {
        "model": DEFAULT_MODEL,
        "input": ["uno", "two"],
        "dimensions": DEFAULT_DIMENSIONS,
        "provider": PROVIDER_POLICY,
    }


@pytest.mark.parametrize(
    "model",
    ["text-embedding-3-large", "openai/text-embedding-3-large", "other/model"],
)
def test_non_qwen_embedding_models_are_rejected(model):
    with pytest.raises(RuntimeError, match="not approved"):
        OpenRouterEmbedder(model=model, api_key="test-key")


def test_wrong_dimension_response_is_rejected():
    embedder = OpenRouterEmbedder(
        api_key="test-key",
        transport=httpx.MockTransport(
            lambda request: _response(request, dimensions=12)
        ),
    )

    with pytest.raises(RuntimeError, match="returned 12 dimensions"):
        embedder.embed(["text"])


def test_build_and_index_model_dispatch_are_closed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    assert isinstance(build_embedder("openrouter"), OpenRouterEmbedder)
    assert isinstance(build_embedder("fake"), FakeEmbedder)
    assert isinstance(embedder_for_model("fake-hashed-bow-256"), FakeEmbedder)
    assert isinstance(embedder_for_model(DEFAULT_MODEL), OpenRouterEmbedder)
    with pytest.raises(ValueError, match="unknown embedder"):
        build_embedder("openai")
    with pytest.raises(RuntimeError, match="not approved"):
        embedder_for_model("text-embedding-3-large")
