"""The async /screen contract: accept, then poll."""

import httpx
import pytest

from app.adapters.job_store_memory import InMemoryJobStore
from app.api.main import app, get_service, require_api_key
from app.domain.models import (
    Assessment,
    Flags,
    JobStatus,
    NextStep,
    ScreenRequest,
    ScreenResult,
)

_BODY = {"transcript": "5y Python", "job_description": "Backend"}

_OK = ScreenResult(
    assessment=Assessment(
        fit_score=4, rationale="ok", evidence=["x"], next_step=NextStep.ADVANCE
    ),
    flags=Flags(),
)


class FakeService:
    """Stands in for ScreenService, tracking whether the work was run."""

    def __init__(self, *, fail: str | None = None):
        self._store = InMemoryJobStore()
        self._fail = fail
        self.ran: list[str] = []

    async def start(self, request: ScreenRequest) -> str:
        await self._store.create("job-1")
        return "job-1"

    async def run(self, job_id: str, request: ScreenRequest) -> None:
        self.ran.append(job_id)
        if self._fail:
            await self._store.fail(job_id, self._fail)
        else:
            await self._store.complete(job_id, _OK)

    async def result(self, job_id: str):
        return await self._store.get(job_id)


@pytest.fixture
def service():
    svc = FakeService()
    app.dependency_overrides[get_service] = lambda: svc
    app.dependency_overrides[require_api_key] = lambda: None
    yield svc
    app.dependency_overrides.clear()


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test_server"
    )


@pytest.mark.asyncio
async def test_post_accepts_and_returns_a_handle_without_blocking(service):
    """202, not 200: the answer does not exist yet. Azure's ingress closes a
    request at 240s and the detector needs minutes, so promising a result
    inside one request is a promise the platform will not let us keep."""
    async with await _client() as c:
        r = await c.post("/screen", json=_BODY)

    assert r.status_code == 202
    body = r.json()
    assert body["id"] == "job-1"
    assert body["status"] == JobStatus.PENDING.value


@pytest.mark.asyncio
async def test_polling_an_unfinished_job_is_202(service):
    """Created but not run. Not driven through POST on purpose: the background
    task fires as the response is returned, so a job posted here is already
    finished by the time it could be polled."""
    job_id = await service.start(ScreenRequest(**_BODY))

    async with await _client() as c:
        r = await c.get(f"/screen/{job_id}")

    assert r.status_code == 202
    assert r.json()["status"] == JobStatus.PENDING.value


@pytest.mark.asyncio
async def test_polling_a_finished_job_is_200_with_the_result(service):
    async with await _client() as c:
        posted = await c.post("/screen", json=_BODY)
        job_id = posted.json()["id"]
        await service.run(job_id, ScreenRequest(**_BODY))
        r = await c.get(f"/screen/{job_id}")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == JobStatus.DONE.value
    assert body["result"]["assessment"]["fit_score"] == 4


@pytest.mark.asyncio
async def test_a_failed_job_is_200_and_says_so(service):
    """The read succeeded; the screening did not. HTTP status answers "is it
    ready", the body answers "what happened" -- so a poller can stop."""
    svc = FakeService(fail="ConnectionError")
    app.dependency_overrides[get_service] = lambda: svc

    async with await _client() as c:
        posted = await c.post("/screen", json=_BODY)
        job_id = posted.json()["id"]
        await svc.run(job_id, ScreenRequest(**_BODY))
        r = await c.get(f"/screen/{job_id}")

    assert r.status_code == 200
    assert r.json()["status"] == JobStatus.FAILED.value
    assert r.json()["error"] == "ConnectionError"


@pytest.mark.asyncio
async def test_an_unknown_job_is_404(service):
    async with await _client() as c:
        r = await c.get("/screen/never-created")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_storage_failure_on_submission_is_503():
    """The screening was never accepted, and the cause is the storage account
    rather than the request. 503 tells the caller to retry; 500 would suggest
    the request itself was at fault."""
    from azure.core.exceptions import ServiceRequestError

    class UnreachableStorage(FakeService):
        async def start(self, request: ScreenRequest) -> str:
            raise ServiceRequestError("queue unreachable")

    app.dependency_overrides[get_service] = lambda: UnreachableStorage()
    app.dependency_overrides[require_api_key] = lambda: None
    try:
        async with await _client() as c:
            r = await c.post("/screen", json=_BODY)
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 503


@pytest.mark.asyncio
async def test_a_programming_error_on_submission_is_not_masked_as_503():
    """503 claims the dependency is at fault and the request may be retried.
    A bug in our own code is neither, so it must not be reported as one."""

    class BuggyService(FakeService):
        async def start(self, request: ScreenRequest) -> str:
            raise TypeError("wrong argument")

    app.dependency_overrides[get_service] = lambda: BuggyService()
    app.dependency_overrides[require_api_key] = lambda: None
    try:
        async with await _client() as c:
            with pytest.raises(TypeError):
                await c.post("/screen", json=_BODY)
    finally:
        app.dependency_overrides.clear()
