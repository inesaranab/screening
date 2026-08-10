"""The auth gate on /screen.

The request/response contract itself is covered in test_api_async.py; this file
is only about who is allowed through the door.
"""

import httpx
import pytest

from app.api.main import app, get_service
from app.config import settings
from app.domain.models import Job, ScreenRequest

_BODY = {"transcript": "5y Python", "job_description": "Backend"}


class FakeService:
    """Accepts work and does nothing with it -- enough to exercise auth."""

    async def start(self, request: ScreenRequest) -> str:
        return "job-1"

    async def run(self, job_id: str, request: ScreenRequest) -> None:
        return None

    async def result(self, job_id: str) -> Job | None:
        return Job(id=job_id)


async def _post(json, headers=None):
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://test_server") as client:
        return await client.post("/screen", json=json, headers=headers or {})


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_bad_key_is_401():
    app.dependency_overrides[get_service] = lambda: FakeService()
    r = await _post(_BODY)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_valid_key_is_accepted():
    """202, not 200 -- the work is accepted, not finished. See test_api_async."""
    app.dependency_overrides[get_service] = lambda: FakeService()
    r = await _post(_BODY, headers={"x-api-key": settings.service_api_key})
    assert r.status_code == 202


# Removed: test_timeout_maps_to_504. The endpoint no longer calls the model, so
# it cannot map model errors -- a timeout now lands on the job as
# `status: failed`, covered by test_a_failed_job_is_200_and_says_so.
