# evals/conftest.py
import subprocess

import pytest

from app.domain.models import Assessment, ScrubResult


@pytest.fixture
def prod_api_key():
    return subprocess.run(
        [
            "az",
            "keyvault",
            "secret",
            "show",
            "--vault-name",
            "screening-kv-7412",
            "--name",
            "screening-service-api-key",
            "--query",
            "value",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


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
