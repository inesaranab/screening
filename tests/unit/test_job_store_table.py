"""Conversion between a Job and an Azure Table entity.

The CRUD calls need a real storage account and live in the integration tests;
this file covers the mapping, which is pure.
"""

import json
from collections.abc import Awaitable, Callable

import pytest

from app.adapters.job_store_table import (
    AzureTableJobStore,
    entity_to_job,
    job_to_entity,
)
from app.domain.models import (
    Assessment,
    Job,
    JobStatus,
    NextStep,
    ScreenResult,
)


def _a_result() -> ScreenResult:
    return ScreenResult(
        assessment=Assessment(
            fit_score=4,
            rationale="Six years of Python on payments systems.",
            next_step=NextStep.ADVANCE,
        )
    )


def test_a_pending_job_round_trips():
    job = Job(id="abc123")

    assert entity_to_job(job_to_entity(job)) == job


def test_a_completed_job_round_trips_with_its_result():
    job = Job(id="abc123", status=JobStatus.DONE, result=_a_result())

    restored = entity_to_job(job_to_entity(job))

    assert restored == job
    assert restored.result is not None
    assert restored.result.assessment.fit_score == 4


def test_a_failed_job_round_trips_with_its_error():
    job = Job(id="abc123", status=JobStatus.FAILED, error="ConnectionError")

    assert entity_to_job(job_to_entity(job)) == job


def test_the_job_id_is_both_partition_and_row_key():
    """Jobs are only ever fetched by id, never scanned or ranged over. Using the
    id for both keys spreads them across every partition, so no single partition
    becomes a throughput ceiling."""
    entity = job_to_entity(Job(id="abc123"))

    assert entity["PartitionKey"] == "abc123"
    assert entity["RowKey"] == "abc123"


def test_the_result_is_stored_as_one_json_column():
    """Table entities are flat and cannot nest, so the result is serialised
    rather than exploded into a column per Assessment field."""
    entity = job_to_entity(Job(id="abc123", status=JobStatus.DONE, result=_a_result()))

    assert json.loads(entity["result"])["assessment"]["fit_score"] == 4


class _FakeTableClient:
    """Records what the adapter sends, and answers reads from that record.

    Stands in for ``azure.data.tables.aio.TableClient`` so the adapter's logic is
    tested without a storage account.
    """

    def __init__(self) -> None:
        self.entities: dict[str, dict] = {}
        self._version = 0
        self.on_read: Callable[[], Awaitable[None]] | None = None

    def _stamp(self, row_key: str) -> None:
        """Give the row a new etag, as any write to it does."""
        self._version += 1
        self.entities[row_key]["etag"] = f"W/\"{self._version}\""

    async def upsert_entity(self, entity: dict, **kwargs) -> None:
        """Merge into the stored row, as UpdateMode.MERGE does.

        A double that replaced the row would hide the loss of any property the
        adapter deliberately leaves out of an update.
        """
        self.entities.setdefault(entity["RowKey"], {}).update(entity)
        self._stamp(entity["RowKey"])

    async def update_entity(self, entity: dict, **kwargs) -> None:
        """Merge only while the row still carries the etag the caller read.

        Models the conditional write the service depends on: without it a test
        double would accept a stale write that real Table Storage rejects.
        """
        from azure.core.exceptions import ResourceModifiedError

        row_key = entity["RowKey"]
        stored = self.entities.get(row_key)
        if stored is None:
            from azure.core.exceptions import ResourceNotFoundError

            raise ResourceNotFoundError("no such entity")
        if kwargs.get("etag") is not None and kwargs["etag"] != stored.get("etag"):
            raise ResourceModifiedError("etag mismatch")
        stored.update(entity)
        self._stamp(row_key)

    async def get_entity(self, partition_key: str, row_key: str) -> dict:
        from azure.core.exceptions import ResourceNotFoundError

        if row_key not in self.entities:
            raise ResourceNotFoundError("no such entity")
        entity = dict(self.entities[row_key])
        if self.on_read is not None:
            # Lets a test interleave another writer between a read and the
            # write that depends on it.
            await self.on_read()
        return entity


@pytest.fixture
def table():
    return _FakeTableClient()


@pytest.fixture
def store(table):
    return AzureTableJobStore(table)


@pytest.mark.asyncio
async def test_create_stores_a_pending_job(store, table):
    await store.create("abc123")

    assert table.entities["abc123"]["status"] == JobStatus.PENDING.value


@pytest.mark.asyncio
async def test_get_returns_the_stored_job(store):
    await store.create("abc123")

    job = await store.get("abc123")

    assert job is not None
    assert job.id == "abc123"
    assert job.status is JobStatus.PENDING


@pytest.mark.asyncio
async def test_get_returns_none_when_the_entity_is_missing(store):
    """The SDK raises ResourceNotFoundError; the port contract is None, which the
    API turns into a 404 rather than an indefinite wait."""
    assert await store.get("never-created") is None


@pytest.mark.asyncio
async def test_complete_stores_the_result(store):
    await store.create("abc123")

    await store.complete("abc123", _a_result())
    job = await store.get("abc123")

    assert job is not None
    assert job.status is JobStatus.DONE
    assert job.result is not None
    assert job.result.assessment.fit_score == 4


@pytest.mark.asyncio
async def test_fail_stores_the_error_and_no_result(store):
    await store.create("abc123")

    await store.fail("abc123", "ConnectionError")
    job = await store.get("abc123")

    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error == "ConnectionError"
    assert job.result is None


@pytest.mark.asyncio
async def test_settling_a_job_keeps_when_it_was_accepted():
    """created_at records acceptance. Rewriting it on completion would make it
    mean acceptance for pending jobs and settlement for finished ones, so a row
    could not be read without knowing its status first."""
    client = _FakeTableClient()
    store = AzureTableJobStore(client)

    accepted = await store.create("abc123")
    await store.complete("abc123", _a_result())

    stored = await store.get("abc123")
    assert stored is not None
    assert stored.created_at == accepted.created_at


@pytest.mark.asyncio
async def test_failing_a_job_keeps_when_it_was_accepted():
    client = _FakeTableClient()
    store = AzureTableJobStore(client)

    accepted = await store.create("abc123")
    await store.fail("abc123", "Timeout")

    stored = await store.get("abc123")
    assert stored is not None
    assert stored.created_at == accepted.created_at


@pytest.mark.asyncio
async def test_fail_if_pending_loses_to_a_completion_that_lands_first():
    """The check and the write are one conditional operation. A worker that
    completes the job between them changes the row, and the conditional write
    is then refused rather than replacing the result."""
    client = _FakeTableClient()
    store = AzureTableJobStore(client)
    await store.create("abc123")

    async def complete_between_read_and_write() -> None:
        client.on_read = None
        await store.complete("abc123", _a_result())

    client.on_read = complete_between_read_and_write

    settled = await store.fail_if_pending("abc123", "TooManyDeliveries")

    assert settled is False
    job = await store.get("abc123")
    assert job is not None
    assert job.status is JobStatus.DONE


@pytest.mark.asyncio
async def test_fail_if_pending_settles_a_job_still_pending():
    client = _FakeTableClient()
    store = AzureTableJobStore(client)
    await store.create("abc123")

    assert await store.fail_if_pending("abc123", "TooManyDeliveries") is True

    job = await store.get("abc123")
    assert job is not None
    assert job.status is JobStatus.FAILED
