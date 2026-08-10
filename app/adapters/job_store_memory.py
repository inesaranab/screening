"""Adapter: JobStore kept in process memory.

The reference implementation -- it defines what correct looks like, and every
unit test runs against it rather than touching Azure.

NOT for production. Nothing here survives a restart, and each API replica would
hold its own dictionary, so a poll landing on a different replica than the one
that accepted the job would 404. The Azure Table adapter exists for that; this
one is for tests and local development, where there is one process and losing
state on restart is fine.
"""

import asyncio

from app.domain.models import Job, JobStatus, ScreenResult


class InMemoryJobStore:
    """JobStore backed by a dict."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        # Writes are read-modify-write, so two coroutines completing different
        # jobs concurrently could otherwise interleave. Cheap here; the Azure
        # adapter gets this from the storage service instead.
        self._lock = asyncio.Lock()

    async def create(self, job_id: str) -> Job:
        """Record a new job as pending. See `JobStore.create`."""
        job = Job(id=job_id)
        async with self._lock:
            self._jobs[job_id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        """Fetch a job, or None. See `JobStore.get`."""
        async with self._lock:
            return self._jobs.get(job_id)

    async def complete(self, job_id: str, result: ScreenResult) -> None:
        """Record a finished screening. See `JobStore.complete`."""
        await self._settle(job_id, status=JobStatus.DONE, result=result)

    async def fail(self, job_id: str, error: str) -> None:
        """Record a failed screening. See `JobStore.fail`."""
        await self._settle(job_id, status=JobStatus.FAILED, error=error)

    async def _settle(
        self,
        job_id: str,
        *,
        status: JobStatus,
        result: ScreenResult | None = None,
        error: str | None = None,
    ) -> None:
        """Move a job out of PENDING.

        Replaces the Job rather than mutating it, so a caller holding an earlier
        reference keeps seeing the state it read -- the same reason ScrubResult
        is returned rather than the input being edited in place.

        Args:
            job_id: The handle given out at creation.
            status: DONE or FAILED.
            result: The assessment, when completing.
            error: Why it failed, when failing.
        """
        async with self._lock:
            self._jobs[job_id] = Job(
                id=job_id, status=status, result=result, error=error
            )
