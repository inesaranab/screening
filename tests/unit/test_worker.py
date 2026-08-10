"""The worker loop: take a job, run it, delete the message."""

import pytest

from app.adapters.job_queue_memory import InMemoryJobQueue
from app.adapters.job_store_memory import InMemoryJobStore
from app.domain.models import Assessment, JobStatus, NextStep, ScrubResult
from app.domain.service import ScreenRequest, ScreenService
from app.worker import drain
from conftest import FakeGuardrail, FakeLLM

_REQ = ScreenRequest(transcript="I am a Quaker.", job_description="Backend")


def _service(store, queue, guardrail=None) -> ScreenService:
    return ScreenService(
        guardrail=guardrail or FakeGuardrail(ScrubResult(clean_text="clean")),
        llm=FakeLLM(
            Assessment(
                fit_score=4, rationale="ok", evidence=["x"], next_step=NextStep.ADVANCE
            )
        ),
        job_store=store,
        job_queue=queue,
    )


@pytest.mark.asyncio
async def test_a_queued_job_is_screened_and_completed():
    store, queue = InMemoryJobStore(), InMemoryJobQueue()
    service = _service(store, queue)
    job_id = await service.start(_REQ)

    processed = await drain(service, queue)

    assert processed == 1
    job = await store.get(job_id)
    assert job is not None
    assert job.status is JobStatus.DONE


@pytest.mark.asyncio
async def test_an_empty_queue_processes_nothing():
    store, queue = InMemoryJobStore(), InMemoryJobQueue()

    assert await drain(_service(store, queue), queue) == 0


@pytest.mark.asyncio
async def test_every_queued_job_is_processed():
    store, queue = InMemoryJobStore(), InMemoryJobQueue()
    service = _service(store, queue)
    ids = [await service.start(_REQ) for _ in range(3)]

    processed = await drain(service, queue)

    assert processed == 3
    for job_id in ids:
        job = await store.get(job_id)
        assert job is not None
        assert job.status is JobStatus.DONE


@pytest.mark.asyncio
async def test_a_failed_screening_still_removes_the_message():
    """run() records the failure rather than raising, so the message is done and
    must be deleted. Leaving it would redeliver a job that will fail identically,
    forever."""

    class DeadDetector:
        async def scrub(self, text: str):
            raise ConnectionError("endpoint down")

    store, queue = InMemoryJobStore(), InMemoryJobQueue()
    service = _service(store, queue, guardrail=DeadDetector())
    job_id = await service.start(_REQ)

    processed = await drain(service, queue)

    assert processed == 1
    job = await store.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert await queue.receive() is None
