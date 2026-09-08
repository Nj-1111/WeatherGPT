import pytest

from app.services.input_pipeline.query_guardrail import _decision_cache


@pytest.fixture(autouse=True)
def _isolate_guardrail_cache():
    """run_guardrail memoizes by question text, and several tests reuse one question with a
    different stubbed LLM. Without this the second would silently assert against the first
    one's cached decision."""
    _decision_cache.clear()
