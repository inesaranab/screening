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


@pytest.mark.asyncio
async def test_a_repeatedly_redelivered_job_is_abandoned_rather_than_retried():
    """A message that keeps coming back is one no worker can finish. Screening
    it again repeats the failure and keeps the unredacted transcript alive on
    the queue, so it is recorded as failed and removed instead."""
    from app.ports.job_queue import QueuedJob
    from app.worker import MAX_DELIVERIES, drain

    store = InMemoryJobStore()
    job_id = "poisoned"
    await store.create(job_id)

    class RedeliveringQueue:
        def __init__(self):
            self.deleted = []
            self._left = 1

        async def enqueue(self, job_id: str, request: ScreenRequest) -> None:
            raise AssertionError("draining must not publish")

        async def receive(self):
            if not self._left:
                return None
            self._left -= 1
            return QueuedJob(
                job_id=job_id, request=_REQ, delivery_count=MAX_DELIVERIES + 1
            )

        async def delete(self, job):
            self.deleted.append(job.job_id)

    class BrokenGuardrail:
        async def scrub(self, text: str):
            raise AssertionError("an abandoned job must not be screened")

    queue = RedeliveringQueue()
    service = _service(store, queue, guardrail=BrokenGuardrail())

    processed = await drain(service, queue)

    assert processed == 0
    assert queue.deleted == [job_id]
    job = await store.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED


@pytest.mark.asyncio
async def test_a_job_is_not_screened_while_the_detector_is_unreachable():
    """Screening without the detector would spend the platform's whole request
    budget failing. The job is recorded as failed and its message removed, so
    the outcome is an answer rather than a retry that fails identically."""
    store, queue = InMemoryJobStore(), InMemoryJobQueue()

    class BrokenGuardrail:
        async def scrub(self, text: str):
            raise AssertionError("must not screen without a detector")

    service = _service(store, queue, guardrail=BrokenGuardrail())
    job_id = await service.start(_REQ)

    async def never_ready() -> bool:
        return False

    processed = await drain(service, queue, detector_ready=never_ready)

    assert processed == 0
    job = await store.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
