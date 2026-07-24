import pytest

from app.adapters.llm_openai import OpenAICompatibleLLM
from app.domain.models import NextStep


@pytest.mark.live
@pytest.mark.asyncio
async def test_llm_returns_valid_assessment():
    assessment = await OpenAICompatibleLLM().assess(
        "5 years Python, built REST APIs", "Python backend role"
    )
    assert assessment.fit_score is not None
    assert 1 <= assessment.fit_score <= 5
    assert assessment.rationale
    assert assessment.next_step in NextStep
