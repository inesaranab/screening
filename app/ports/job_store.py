"""Port: the job-storage boundary."""

from typing import Protocol

from app.domain.models import Job, ScreenResult


class JobStore(Protocol):
    """Anywhere a screening job can be kept while it is being worked on.

    A store rather than in-process state because the API runs up to ten
    replicas: the replica that accepts a job is rarely the one polled for its
    result, and the process doing the work is different again.
    """

    async def create(self, job_id: str) -> Job:
        """Record a new job as pending.

        Args:
            job_id: Opaque handle the caller will poll with. Chosen by the
                caller rather than the store, so the API can hand it back in
                the same response that accepts the work.

        Returns:
            The stored Job, pending.
        """
        ...

    async def get(self, job_id: str) -> Job | None:
        """Fetch a job.

        Args:
            job_id: The handle given out at creation.

        Returns:
            The Job, or None when no such job exists -- which the API turns
            into a 404 rather than an indefinite wait.
        """
        ...

    async def complete(self, job_id: str, result: ScreenResult) -> None:
        """Record a finished screening.

        Args:
            job_id: The handle given out at creation.
            result: The assessment and flags to hand back to the poller.
        """
        ...

    async def fail(self, job_id: str, error: str) -> None:
        """Record that the screening could not be produced.

        Separate from `complete` because a poller has to be able to stop.
        Storing a failure as an empty result is indistinguishable from work
        still in progress.

        Args:
            job_id: The handle given out at creation.
            error: Why it failed, written for an operator. Never candidate
                data: the store outlives the request and nothing scrubs it.
        """
        ...

    async def fail_if_pending(self, job_id: str, error: str) -> bool:
        """Record a failure only while the job is still outstanding.

        One operation, not a read followed by a write. A job can be completed
        between those two, and the failure would then replace an answer the
        caller may already have read.

        Args:
            job_id: The handle given out at creation.
            error: Why it failed, written for an operator. Never candidate
                data: the store outlives the request and nothing scrubs it.

        Returns:
            True if the job was still pending and is now failed. False if it
            had already finished, or does not exist.
        """
        ...
