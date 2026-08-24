"""Result objects returned by offline processors."""
import types
from dataclasses import dataclass, field


@dataclass
class _Usage:
    """Usage fields consumed by workers and repair spend records."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    tool_calls: int = 0
    details: dict = field(default_factory=dict)


def fake_result(output):
    """Return the common offline processor result shape."""
    return types.SimpleNamespace(output=output, usage=_Usage())
