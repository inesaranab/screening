"""Adapter: JobStore backed by Azure Table Storage.

Table Storage is serverless, so it holds no compute that would prevent the rest
of the system scaling to zero. It is billed per operation and per byte stored.

Authentication is by managed identity; no storage key is read or configured.
"""

import json
from datetime import datetime
from typing import Protocol

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import UpdateMode

from app.domain.models import Job, JobStatus, ScreenResult


class TableClientLike(Protocol):
    """The subset of ``azure.data.tables.aio.TableClient`` this adapter uses.

    Declared so the adapter can be constructed with a test double without
    depending on the concrete SDK type.
    """

    async def upsert_entity(self, entity: dict, **kwargs: object) -> object: ...

    async def get_entity(self, partition_key: str, row_key: str) -> dict: ...

    async def update_entity(self, entity: dict, **kwargs: object) -> object: ...


def job_to_entity(job: Job) -> dict:
    """Convert a Job into an Azure Table entity.

    The job id is used as both PartitionKey and RowKey. Jobs are fetched only by
    id, so this distributes them across every partition rather than concentrating
    them in one.

    ``result`` is serialised into a single JSON column. Table entities are flat
    and cannot hold nested values.

    Args:
        job: The job to store.

    Returns:
        A dict suitable for ``TableClient.upsert_entity``.
    """
    return {
        "PartitionKey": job.id,
        "RowKey": job.id,
        "status": job.status.value,
        "result": job.result.model_dump_json() if job.result is not None else "",
        "error": job.error or "",
        "created_at": job.created_at.isoformat(),
    }


def entity_to_job(entity: dict) -> Job:
    """Convert an Azure Table entity into a Job.

    Empty strings are read as absent. Table Storage stores no null for a
    property, so an unset column and one set to "" are indistinguishable on
    read.

    Args:
        entity: An entity as returned by ``TableClient.get_entity``.

    Returns:
        The reconstructed Job.
    """
    raw_result = entity.get("result") or ""
    raw_error = entity.get("error") or ""
    raw_created = entity.get("created_at") or ""
    fields: dict = {
        "id": entity["RowKey"],
        "status": JobStatus(entity["status"]),
        "result": ScreenResult(**json.loads(raw_result)) if raw_result else None,
        "error": raw_error or None,
    }
    # Omitted rather than defaulted when absent: a row written before this
    # column existed has no accepted-at time, and inventing "now" for it would
    # keep restarting its deadline on every read.
    if raw_created:
        fields["created_at"] = datetime.fromisoformat(raw_created)
    return Job(**fields)


class AzureTableJobStore:
    """JobStore backed by an Azure Table.

    Satisfies the same contract as ``InMemoryJobStore`` and is verified against
    the same behaviour, so the two are interchangeable in the composition root.

    Unlike the in-memory store this survives a restart and is shared by every
    replica, so a poll reaches the job regardless of which replica accepted it.
    """

    def __init__(self, table: TableClientLike) -> None:
        """Initialise the store.

        Args:
            table: A client for the table holding jobs. Injected rather than
                constructed here so the composition root owns its lifetime and
                tests can supply a double.
        """
        self._table = table

    async def create(self, job_id: str) -> Job:
        """Record a new job as pending. See ``JobStore.create``."""
        job = Job(id=job_id)
        await self._table.upsert_entity(job_to_entity(job))
        return job

    async def get(self, job_id: str) -> Job | None:
        """Fetch a job, or None. See ``JobStore.get``."""
        try:
            entity = await self._table.get_entity(job_id, job_id)
        except ResourceNotFoundError:
            return None
        return entity_to_job(dict(entity))

    async def complete(self, job_id: str, result: ScreenResult) -> None:
        """Record a finished screening. See ``JobStore.complete``."""
        await self._settle(job_id, JobStatus.DONE, result=result.model_dump_json())

    async def fail(self, job_id: str, error: str) -> None:
        """Record a failed screening. See ``JobStore.fail``."""
        await self._settle(job_id, JobStatus.FAILED, error=error)

    async def _settle(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: str = "",
        error: str = "",
    ) -> None:
        """Move a job out of PENDING.

        Writes only the properties that change. ``created_at`` is left out, so
        the merge keeps the time the job was accepted rather than replacing it
        with the time it finished.

        Args:
            job_id: The handle given out at creation.
            status: DONE or FAILED.
            result: The serialised assessment, when completing.
            error: Why it failed, when failing.
        """
        await self._table.upsert_entity(
            {
                "PartitionKey": job_id,
                "RowKey": job_id,
                "status": status.value,
                "result": result,
                "error": error,
            },
            mode=UpdateMode.MERGE,
        )

    async def fail_if_pending(self, job_id: str, error: str) -> bool:
        """Fail a job only while it is pending. See ``JobStore.fail_if_pending``.

        The read supplies the row's etag and the write requires it to be
        unchanged, so a completion that lands in between causes the write to be
        refused rather than to replace the result.
        """
        try:
            entity = await self._table.get_entity(job_id, job_id)
        except ResourceNotFoundError:
            return False
        if dict(entity).get("status") != JobStatus.PENDING.value:
            return False
        # The etag is metadata on the entity, not one of its properties, so it
        # does not survive being copied into a plain dict.
        etag = getattr(entity, "metadata", {}).get("etag")
        if etag is None:
            return False
        try:
            await self._table.update_entity(
                {
                    "PartitionKey": job_id,
                    "RowKey": job_id,
                    "status": JobStatus.FAILED.value,
                    "result": "",
                    "error": error,
                },
                mode=UpdateMode.MERGE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError:
            return False
        return True
