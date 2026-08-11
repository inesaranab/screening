"""Port: the work-queue boundary."""

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.models import ScreenRequest


@dataclass
class QueuedJob:
    """One unit of accepted work, taken off the queue.

    Attributes:
        job_id: The id the caller polls with.
        request: The transcript and job description to screen.
        receipt: Adapter-specific handle identifying this delivery, passed back
            to ``delete``. Opaque to the domain.
        delivery_count: How many times this message has been handed to a
            worker, this delivery included. A count above one means an earlier
            attempt did not finish.
    """

    job_id: str
    request: ScreenRequest
    receipt: Any = None
    delivery_count: int = 1


class JobQueue(Protocol):
    """Carries accepted work from whoever accepts it to whoever performs it.

    The request travels in the message rather than being stored, so an
    unredacted transcript exists only for as long as the work is outstanding.
    """

    async def enqueue(self, job_id: str, request: ScreenRequest) -> None:
        """Publish a job for a worker to pick up.

        Args:
            job_id: The id already recorded in the job store.
            request: The transcript and job description to screen.
        """
        ...

    async def receive(self) -> QueuedJob | None:
        """Take the next job off the queue.

        A received message is hidden from other consumers, so two workers do
        not perform the same screening.

        Returns:
            The next job, or None when the queue is empty.
        """
        ...

    async def delete(self, job: QueuedJob) -> None:
        """Remove a message that has been fully processed.

        Deletion is what marks the work done. A message that is received but
        never deleted becomes visible again, so a worker that dies mid-job
        leaves the screening to be retried rather than lost.

        Args:
            job: The job as returned by ``receive``.
        """
        ...
