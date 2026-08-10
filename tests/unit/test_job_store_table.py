"""Conversion between a Job and an Azure Table entity.

The CRUD calls need a real storage account and live in the integration tests;
this file covers the mapping, which is pure.
"""

import json

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

    async def upsert_entity(self, entity: dict) -> None:
        self.entities[entity["RowKey"]] = entity

    async def get_entity(self, partition_key: str, row_key: str) -> dict:
        from azure.core.exceptions import ResourceNotFoundError

        if row_key not in self.entities:
            raise ResourceNotFoundError("no such entity")
        return self.entities[row_key]


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
