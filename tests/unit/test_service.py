import pytest

from app.domain.models import Assessment, NextStep, ScrubResult
from app.domain.service import ScreenRequest, ScreenService
from conftest import FakeGuardrail, FakeLLM

_REQ = ScreenRequest(transcript="x", job_description="Backend")


# 1. injection -> fail closed, and the LLM is never called
@pytest.mark.asyncio
async def test_injection_fails_closed_without_calling_llm():
    guard = FakeGuardrail(ScrubResult(clean_text="withheld", injection_detected=True))

    class BrokenLLM:
        async def assess(self, transcript: str, job_description: str):
            raise AssertionError("LLM must not be called on injection")

    service = ScreenService(guardrail=guard, llm=BrokenLLM())
    result = await service.screen(_REQ)
    assert result.flags.injection_detected
    assert result.flags.out_of_scope
    assert result.assessment.fit_score is None


# 2. clean path -> flags assembled from scrub + assessment
@pytest.mark.asyncio
async def test_clean_path_sets_flags():
    guard = FakeGuardrail(ScrubResult(clean_text="clean", pii_redacted=True))
    llm = FakeLLM(
        Assessment(fit_score=4, rationale="ok", evidence=[], next_step=NextStep.ADVANCE)
    )
    service = ScreenService(guardrail=guard, llm=llm)
    result = await service.screen(_REQ)
    assert result.flags.pii_redacted is True
    assert result.flags.low_confidence is True
    assert result.flags.injection_detected is False
