"""The in-memory JobStore: the reference behaviour every adapter must match."""

import pytest

from app.adapters.job_store_memory import InMemoryJobStore
from app.domain.models import (
    Assessment,
    JobStatus,
    NextStep,
    ScreenResult,
)
from app.ports.job_store import JobStore


def _a_result() -> ScreenResult:
    return ScreenResult(
        assessment=Assessment(
            fit_score=4,
            rationale="Six years of Python on payments systems.",
            next_step=NextStep.ADVANCE,
        )
    )


@pytest.fixture
def store() -> JobStore:
    return InMemoryJobStore()


@pytest.mark.asyncio
async def test_a_created_job_can_be_read_back_as_pending(store):
    await store.create("abc123")

    job = await store.get("abc123")

    assert job is not None
    assert job.id == "abc123"
    assert job.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_an_unknown_job_is_none_not_pending(store):
    """None and PENDING must differ: a typo'd id that read as pending would be
    polled forever for work nobody is doing."""
    assert await store.get("never-created") is None


@pytest.mark.asyncio
async def test_completing_a_job_stores_its_result(store):
    await store.create("abc123")

    await store.complete("abc123", _a_result())
    job = await store.get("abc123")

    assert job is not None
    assert job.status is JobStatus.DONE
    assert job.result is not None
    assert job.result.assessment.fit_score == 4
    assert job.error is None


@pytest.mark.asyncio
async def test_failing_a_job_stores_why_and_leaves_no_result(store):
    await store.create("abc123")

    await store.fail("abc123", "detector unreachable")
    job = await store.get("abc123")

    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error == "detector unreachable"
    assert job.result is None


@pytest.mark.asyncio
async def test_jobs_do_not_leak_into_each_other(store):
    await store.create("first")
    await store.create("second")

    await store.complete("first", _a_result())

    second = await store.get("second")
    assert second is not None
    assert second.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_settling_a_job_keeps_when_it_was_accepted():
    """The same property the Azure store holds: the two are interchangeable
    only if settling preserves acceptance time in both."""
    store = InMemoryJobStore()
    accepted = await store.create("abc123")

    await store.complete("abc123", _a_result())

    stored = await store.get("abc123")
    assert stored is not None
    assert stored.created_at == accepted.created_at


@pytest.mark.asyncio
async def test_fail_if_pending_refuses_a_job_that_already_finished():
    """Checking the status and writing the failure must be one operation. A job
    can be completed between a separate read and write, and the failure would
    then replace an answer the caller may already have read."""
    store = InMemoryJobStore()
    await store.create("abc123")
    await store.complete("abc123", _a_result())

    settled = await store.fail_if_pending("abc123", "TooManyDeliveries")

    assert settled is False
    job = await store.get("abc123")
    assert job is not None
    assert job.status is JobStatus.DONE
    assert job.result is not None


@pytest.mark.asyncio
async def test_fail_if_pending_settles_a_job_still_pending():
    store = InMemoryJobStore()
    await store.create("abc123")

    settled = await store.fail_if_pending("abc123", "TooManyDeliveries")

    assert settled is True
    job = await store.get("abc123")
    assert job is not None
    assert job.status is JobStatus.FAILED
