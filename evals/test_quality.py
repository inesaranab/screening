import json
import pathlib

import pytest
from deepeval import assert_test
from deepeval.dataset.golden import Golden

from app.adapters.llm_openai import OpenAICompatibleLLM
from app.domain.models import ScreenRequest
from app.domain.service import ScreenService
from evals.metrics import ALL_METRICS

_FIXTURES = json.loads((pathlib.Path(__file__).parent / "fixtures.json").read_text())
_JOB_DESCRIPTIONS = _FIXTURES["job_descriptions"]
_CASES = [c for c in _FIXTURES["cases"] if not c["expect"]["injection_detected"]]


@pytest.mark.quality
@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
async def test_assessment_meets_quality_bar(case, guardrail):
    """Judge the assessment the model produces for one fixture.

    Deliberately goes through ScreenService rather than calling the LLM adapter
    directly, so the model is judged on guardrail-scrubbed text — exactly what it
    receives in production. Calling the adapter directly would feed it raw
    fixtures and penalise it for quoting PII that would never have reached it.
    """
    job_description = _JOB_DESCRIPTIONS[case["jd"]]
    llm = OpenAICompatibleLLM()
    service = ScreenService(guardrail=guardrail, llm=llm)
    # @observe on llm.assess populates the DeepEval trace that the metrics read;
    # closed in a finally so a raised assess() cannot leak the HTTP client. No
    # except — the exception must propagate, a silently-scored failure is worse.
    try:
        await service.screen(
            ScreenRequest(
                transcript=case["transcript"], job_description=job_description
            )
        )
    finally:
        await llm.aclose()

    golden = Golden(
        input=job_description,
        additional_metadata={"case_id": case["id"]},
    )
    assert_test(metrics=ALL_METRICS, golden=golden)
