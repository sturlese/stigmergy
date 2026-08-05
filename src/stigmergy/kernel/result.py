"""The result envelope every offline backend returns from `run()`.

It lives here rather than beside a fake backend, and that placement is the point: while it sat
inside one, production modules imported a large offline backend at module level just to build a
two-attribute namespace for the fakes they define themselves. The import graph then said
"production depends on the offline backend" when the real dependency was this envelope.

Nothing here is a backend or a test double: it is the shape `worker.py` reads back from any
processor, real or fake.
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
    for all of them (the fakes in fake_llm plus the in-module ones in views/versions/claims/ops)."""
    return types.SimpleNamespace(output=output, usage=_Usage())
