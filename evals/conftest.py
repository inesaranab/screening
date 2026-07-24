# evals/conftest.py

from app.domain.models import Assessment, ScrubResult


class FakeGuardrail:
    """Satisfies the Guardrail port - we return a canned value store in constructor"""

    def __init__(self, result: ScrubResult):
        self._result = result

    async def scrub(self, text: str) -> ScrubResult:
        return self._result


class FakeLLM:
    """Satisfies the LLMClient port - we return a canned value store in the constructor"""

    def __init__(self, assessment: Assessment):
        self._assessment = assessment

    async def assess(self, transcript: str, job_description: str) -> Assessment:
        return self._assessment
