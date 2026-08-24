import pytest
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from stigmergy.librarian import worker


@pytest.mark.parametrize(
    "error",
    (
        UnexpectedModelBehavior("invalid structured output"),
        ModelHTTPError(503, "anthropic:claude-sonnet-5"),
    ),
)
def test_model_run_failures_are_retryable(error):
    assert worker._retryable(error) is True
