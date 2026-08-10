import asyncio
import json

import pytest
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from app.adapters.job_queue_memory import InMemoryJobQueue
from app.adapters.job_store_memory import InMemoryJobStore
from app.adapters.llm_openai import OpenAICompatibleLLM
from app.domain.models import ScreenRequest
from app.domain.service import ScreenService
from conftest import _FIXTURES_PATH, FakeGuardrail
from evals.metrics import ALL_METRICS

_FIXTURES = json.loads(_FIXTURES_PATH.read_text())
_JOB_DESCRIPTIONS = _FIXTURES["job_descriptions"]
_CASES = [c for c in _FIXTURES["cases"] if not c["expect"]["injection_detected"]]

# T-4 currently fails Faithfulness: the guardrail (Presidio/GLiNER) mangles the
# transcript before the model sees it, so the model hallucinates on already-bad
# input -- not a real model quality regression. Expected to resolve once GLiNER
# is replaced (Gemma-4-31B-it), not fixed by tuning GLiNER further.
#
# Deliberately not xfail-marked: GLiNER is being replaced, not tuned, so
# tracking this as a long-lived expected-failure would go stale the moment
# the swap lands. Leaving it as a plain, visible failure until then is
# intentional -- do not re-add an xfail marker here.


@pytest.mark.quality
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_assessment_meets_quality_bar(case, guardrail):
    async def _get_result():
        job_description = _JOB_DESCRIPTIONS[case["jd"]]
        scrub = await guardrail.scrub(case["transcript"])
        # _CASES is filtered on the fixture's *declared* expectation, not the
        # runtime scrub -- if the guardrail regresses and flags this transcript
        # for real, `scrub.clean_text` becomes withheld-result boilerplate and
        # the judge would silently score that instead of a real assessment.
        assert not scrub.injection_detected, (
            f"{case['id']}: guardrail flagged injection at runtime even though "
            "the fixture declares expect.injection_detected=false"
        )
        llm = OpenAICompatibleLLM()
        # screen() itself never touches the store; the constructor needs one
        # because the service also exposes start/run/result.
        service = ScreenService(
            guardrail=FakeGuardrail(scrub),
            llm=llm,
            job_store=InMemoryJobStore(),
            job_queue=InMemoryJobQueue(),
        )
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
