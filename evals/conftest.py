# evals/conftest.py
import os
import shutil
import subprocess

import pytest

from app.domain.models import Assessment, ScrubResult

# Set before any test module imports transformers/presidio, so every test run
# skips the Hub freshness check and loads straight from the local cache.
os.environ.setdefault("HF_HUB_OFFLINE", "1")


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


@pytest.fixture(scope="session")
def guardrail():
    """The real guardrail, built once for the entire test session.

    Loading Presidio, spaCy, GLiNER and the injection classifier costs ~30s and
    several GB of RAM, so a per-test (function-scoped) fixture pays that cost once
    per test — six tests meant six full loads. Session scope means one load no
    matter which files or how many tests are selected.

    Imported inside the function so importing this conftest stays cheap, and so
    HF_HUB_OFFLINE above is already set before transformers/presidio load.
    """
    from app.adapters.guard_classifier import ClassifierGuardrail

    return ClassifierGuardrail()


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
