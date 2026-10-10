"""Token usage of a reasoning response is counted, not reported as free.

pydantic-ai 2.13 extracted every count as zero for a response reporting
`output_tokens_details.reasoning_tokens`, so a paid filing or answer recorded no usage; the project
carried a shim until the framework fixed it. This pins the framework's own behavior so an upgrade
that brings the defect back fails here instead of in the usage ledger.
"""
from pydantic_ai.usage import RequestUsage

REASONING_RESPONSE = {
    "model": "gpt-5.6-terra",
    "usage": {"input_tokens": 1081, "input_tokens_details": {"cached_tokens": 400},
              "output_tokens": 1250, "output_tokens_details": {"reasoning_tokens": 384},
              "total_tokens": 2331},
}


def test_a_reasoning_response_reports_the_tokens_it_used():
    usage = RequestUsage.extract(REASONING_RESPONSE, provider="openai",
                                 provider_url="https://api.openai.com/v1", provider_fallback="openai",
                                 api_flavor="responses", details={"reasoning_tokens": 384})

    assert usage.input_tokens == 1081
    assert usage.cache_read_tokens == 400
    assert usage.output_tokens == 1250
