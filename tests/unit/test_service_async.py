"""Accepting work, doing it elsewhere, and fetching the answer."""

import pytest

from app.adapters.job_store_memory import InMemoryJobStore
from app.domain.models import Assessment, JobStatus, NextStep, ScrubResult
from app.domain.service import ScreenRequest, ScreenService
from conftest import FakeGuardrail, FakeLLM

_REQ = ScreenRequest(transcript="I am a Quaker.", job_description="Backend")


def _service(guardrail=None, llm=None, store=None, queue=None) -> ScreenService:
    from app.adapters.job_queue_memory import InMemoryJobQueue

    return ScreenService(
        guardrail=guardrail or FakeGuardrail(ScrubResult(clean_text="clean")),
        llm=llm
        or FakeLLM(
            Assessment(
                fit_score=4, rationale="ok", evidence=["x"], next_step=NextStep.ADVANCE
            )
        ),
        job_store=store or InMemoryJobStore(),
        job_queue=queue or InMemoryJobQueue(),
    )


@pytest.mark.asyncio
async def test_start_records_a_pending_job_and_does_no_work():
    """start() must return before the detector is touched -- that is the whole
    point: the caller gets an id in milliseconds while a GPU takes minutes."""

    class BrokenGuardrail:
        async def scrub(self, text: str):
            raise AssertionError("start() must not do the work")

    store = InMemoryJobStore()
    service = _service(guardrail=BrokenGuardrail(), store=store)

    job_id = await service.start(_REQ)

    job = await store.get(job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_start_leaves_the_job_pending_when_publishing_raises():
    """A raised publish is ambiguous: the queue may have accepted the message
    before the failure, in which case a worker will still run the job. FAILED
    would then be contradicted by a result arriving later, so the job keeps the
    status that is true either way."""

    class BrokenQueue:
        """Records the id it was asked to publish, then refuses to publish."""

        def __init__(self) -> None:
            self.job_id = ""

        async def enqueue(self, job_id: str, request: ScreenRequest) -> None:
            self.job_id = job_id
            raise ConnectionError("queue unreachable")

    store = InMemoryJobStore()
    queue = BrokenQueue()
    service = _service(store=store, queue=queue)

    with pytest.raises(ConnectionError):
        await service.start(_REQ)

    job = await store.get(queue.job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.error is None


@pytest.mark.asyncio
async def test_run_stores_the_assessment():
    store = InMemoryJobStore()
    service = _service(store=store)
    job_id = await service.start(_REQ)

    await service.run(job_id, _REQ)

    job = await store.get(job_id)
    assert job is not None
    assert job.status is JobStatus.DONE
    assert job.result is not None
    assert job.result.assessment.fit_score == 4


@pytest.mark.asyncio
async def test_run_records_failure_instead_of_raising():
    """Synchronously a dead detector becomes a 502 because someone is waiting.
    In a worker nobody is: an uncaught exception leaves the job PENDING forever
    and the caller polls into the void. run() has to store the failure."""

    class DeadDetector:
        async def scrub(self, text: str):
            raise ConnectionError("endpoint down")

    store = InMemoryJobStore()
    service = _service(guardrail=DeadDetector(), store=store)
    job_id = await service.start(_REQ)

    await service.run(job_id, _REQ)

    job = await store.get(job_id)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error


@pytest.mark.asyncio
async def test_a_stored_failure_never_carries_candidate_data():
    """The store outlives the request and nothing scrubs it, so an error string
    built from the exception must not end up quoting the transcript."""

    class LeakyDetector:
        async def scrub(self, text: str):
            raise ValueError(f"failed on: {text}")

    store = InMemoryJobStore()
    service = _service(guardrail=LeakyDetector(), store=store)
    job_id = await service.start(_REQ)

    await service.run(job_id, _REQ)

    job = await store.get(job_id)
    assert job is not None
    assert job.error is not None
    assert "Quaker" not in job.error


@pytest.mark.asyncio
async def test_result_returns_none_for_an_unknown_id():
    service = _service()
    assert await service.result("never-created") is None


@pytest.mark.asyncio
async def test_start_publishes_the_job_for_a_worker():
    """The work leaves the web process entirely. Nothing in the API holds it, so
    the app can scale to zero while a screening is still outstanding."""
    from app.adapters.job_queue_memory import InMemoryJobQueue

    queue = InMemoryJobQueue()
    store = InMemoryJobStore()
    service = ScreenService(
        guardrail=FakeGuardrail(ScrubResult(clean_text="clean")),
        llm=FakeLLM(
            Assessment(
                fit_score=4, rationale="ok", evidence=["x"], next_step=NextStep.ADVANCE
            )
        ),
        job_store=store,
        job_queue=queue,
    )

    job_id = await service.start(_REQ)

    message = await queue.receive()
    assert message is not None
    assert message.job_id == job_id
    assert message.request.transcript == _REQ.transcript
