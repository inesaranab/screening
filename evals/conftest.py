# evals/conftest.py
import shutil
import subprocess

import pytest

from app.domain.models import Assessment, ScrubResult


def pytest_addoption(parser):
    parser.addoption(
        "--run-prod", action="store_true", default=False, help="run tests marked prod"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-prod"):
        return
    skip_prod = pytest.mark.skip(reason="needs --run-prod to run")
    for item in items:
        if "prod" in item.keywords:
            item.add_marker(skip_prod)


@pytest.fixture
def prod_api_key():
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("az CLI not found on PATH")
    return subprocess.run(
        [
            az,
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
