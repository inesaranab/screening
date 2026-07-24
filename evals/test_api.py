import httpx
import pytest
from openai import APITimeoutError

from app.api.main import app, get_service, require_api_key
from app.config import settings
from app.domain.models import Assessment, Flags, NextStep, ScreenRequest, ScreenResult


class FakeService:
    def __init__(
        self, result: ScreenResult | None = None, error: Exception | None = None
    ):
        self._result = result
        self._error = error

    async def screen(self, request: ScreenRequest) -> ScreenResult:
        if self._error:
            raise self._error
        assert self._result is not None
        return self._result


_OK = ScreenResult(
    assessment=Assessment(
        fit_score=4, rationale="ok", evidence=["x"], next_step=NextStep.ADVANCE
    ),
    flags=Flags(),
)


async def _post(json, headers=None):
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://test_server") as client:
        return await client.post("/screen", json=json, headers=headers or {})


_BODY = {"transcript": "5y Python", "job_description": "Backend"}


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


# 1. auth gate works
@pytest.mark.asyncio
async def test_bad_key_is_401():
    app.dependency_overrides[get_service] = lambda: FakeService(result=_OK)
    r = await _post(_BODY)
    assert r.status_code == 401


# 2. happy path reaches the service (auth + wiring)
@pytest.mark.asyncio
async def test_valid_key_returns_200():
    app.dependency_overrides[get_service] = lambda: FakeService(result=_OK)
    r = await _post(_BODY, headers={"x-api-key": settings.service_api_key})
    assert r.status_code == 200


# 3. error mapping
@pytest.mark.asyncio
async def test_timeout_maps_to_504():
    app.dependency_overrides[get_service] = lambda: FakeService(
        error=APITimeoutError(request=httpx.Request("POST", "http://tests_server"))
    )
    app.dependency_overrides[require_api_key] = lambda: None
    r = await _post(_BODY)
    assert r.status_code == 504
