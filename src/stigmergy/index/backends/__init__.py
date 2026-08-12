"""Embedding backends: the real OpenAI embedder and the deterministic offline double.
Production never imports the fake at module level — `build_embedder` reaches for it with a
deferred import only when selected (architecture-tested).
"""
