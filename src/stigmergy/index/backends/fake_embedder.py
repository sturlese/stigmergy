"""Deterministic keyless embedder for offline validation."""
import hashlib
import math

DIM = 256


class FakeEmbedder:
    """Duck-typed like OpenRouterEmbedder: `.model` and `.embed(texts)`."""

    model = "fake-hashed-bow-256"
    dim = DIM
    host = "fake"    # its own name-space marker, recorded in index_meta like the real host

    def embed(self, texts: list[str], *, timeout_s: float | None = None) -> list[list[float]]:
        del timeout_s
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for token in text.lower().split():
                h = int.from_bytes(hashlib.md5(token.encode()).digest()[:8], "big")
                vec[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out
