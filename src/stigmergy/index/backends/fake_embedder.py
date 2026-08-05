"""Deterministic offline embedder — the CI double, because CI must never need API keys.

Hashed bag-of-words: no ES<->EN semantics, but byte-for-byte deterministic — the same text always
embeds to the same vector, which is what the end-to-end idempotency proof (wipe -> rebuild ->
identical hit lists) and the embedding-cache tests rely on. Never used outside tests/CI;
`build_embedder` imports it deferred.
"""
import hashlib
import math

DIM = 256


class FakeEmbedder:
    """Duck-typed like OpenAIEmbedder: `.model` and `.embed(texts)`."""

    model = "fake-hashed-bow-256"
    dim = DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for token in text.lower().split():
                h = int.from_bytes(hashlib.md5(token.encode()).digest()[:8], "big")
                vec[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out
