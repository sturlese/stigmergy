"""The result envelope every offline backend returns from `run()` — the shape `worker.py` reads
back from any processor, real or fake. Nothing here is a backend or a test double.
"""
import types


class _Usage:
    """Mirrors the attributes worker.py reads from pydantic-ai's usage object."""
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    details: dict = {}


def fake_result(output):
    """The (.output, .usage) result shape every fake backend returns from run() — one definition
    for all of them."""
    return types.SimpleNamespace(output=output, usage=_Usage())
