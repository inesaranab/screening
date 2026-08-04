import asyncio
import json
import pathlib

import pytest
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from app.adapters.llm_openai import OpenAICompatibleLLM
from app.domain.models import ScreenRequest
from app.domain.service import ScreenService
from evals.conftest import FakeGuardrail
from evals.metrics import ALL_METRICS

_FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures.json").read_text())
_JOB_DESCRIPTIONS = _FIXTURES["job_descriptions"]
_CASES = [c for c in _FIXTURES["cases"] if not c["expect"]["injection_detected"]]


@pytest.mark.quality
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_assessment_meets_quality_bar(case, guardrail):
    async def _get_result():
        job_description = _JOB_DESCRIPTIONS[case["jd"]]
        scrub = await guardrail.scrub(case["transcript"])
        llm = OpenAICompatibleLLM()
        service = ScreenService(guardrail=FakeGuardrail(scrub), llm=llm)
        try:
            result = await service.screen(
                ScreenRequest(
                    transcript=case["transcript"], job_description=job_description
                )
            )
        finally:
            await llm.aclose()
        return job_description, scrub, result

    # Execute async setup in an isolated event loop that terminates before deepeval runs
    job_description, scrub, result = asyncio.run(_get_result())

    assessment = result.assessment
    test_case = LLMTestCase(
        input=job_description,
        actual_output=f"{assessment.rationale} {' '.join(assessment.evidence)}",
        retrieval_context=[scrub.clean_text, job_description],
    )

    # Executed synchronously without a active event loop, preventing nest_asyncio deadlocks
    assert_test(test_case=test_case, metrics=ALL_METRICS)
