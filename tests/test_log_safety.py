import ast
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "stigmergy"


def _renders_exception_value(node, name: str) -> bool:
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "__class__"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == name
    ):
        return False
    if isinstance(node, ast.Name):
        return node.id == name
    return any(_renders_exception_value(child, name) for child in ast.iter_child_nodes(node))


def test_runtime_logging_never_renders_exception_messages_or_tracebacks():
    offenders = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "exception":
                offenders.append(f"{path.relative_to(SOURCE)}:{node.lineno}:log.exception")
            for keyword in node.keywords:
                if (
                    keyword.arg == "exc_info"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    offenders.append(f"{path.relative_to(SOURCE)}:{node.lineno}:exc_info=True")
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            if not handler.name:
                continue
            for call in (node for statement in handler.body for node in ast.walk(statement)
                         if isinstance(node, ast.Call)):
                if not isinstance(call.func, ast.Attribute) or call.func.attr not in {
                    "debug", "info", "warning", "error", "critical", "exception"
                }:
                    continue
                if any(_renders_exception_value(arg, handler.name) for arg in call.args[1:]):
                    offenders.append(
                        f"{path.relative_to(SOURCE)}:{call.lineno}:exception-value"
                    )
    assert offenders == []
