"""The result envelope every offline backend returns from `run()` — the shape `worker.py` reads
back from any processor, real or fake. Nothing here is a backend or a test double.
"""
import types
from dataclasses import dataclass, field


@dataclass
class _Usage:
    """Mirrors the attributes worker.py and the repair spend records read from pydantic-ai's
    usage object."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    tool_calls: int = 0
    details: dict = field(default_factory=dict)


def fake_result(output):
    """The (.output, .usage) result shape every fake backend returns from run() — one definition
    for all of them."""
    return types.SimpleNamespace(output=output, usage=_Usage())
