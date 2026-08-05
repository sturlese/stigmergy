"""Embedding backends: the real OpenAI embedder and the deterministic offline double.

Production code never imports the fake at module level — `build_embedder` reaches for it with a
deferred import only when the fake backend is actually selected (tests/CI).
`tests/index/test_architecture.py` enforces it.
"""
