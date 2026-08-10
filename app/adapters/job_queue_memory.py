"""Adapter: JobQueue held in process memory.

The reference implementation, used by unit tests and local development.

NOT for production. Nothing survives a restart, and each process holds its own
queue, so an API replica and a separate worker would never see each other's
messages. It also removes a message on receive rather than hiding it, so a
worker that dies mid-job loses the work instead of having it retried.
"""

import asyncio
from collections import deque

from app.domain.models import ScreenRequest
from app.ports.job_queue import QueuedJob


class InMemoryJobQueue:
    """JobQueue backed by a deque."""

    def __init__(self) -> None:
        self._messages: deque[QueuedJob] = deque()
        self._lock = asyncio.Lock()

    async def enqueue(self, job_id: str, request: ScreenRequest) -> None:
        """Publish a job. See ``JobQueue.enqueue``."""
        async with self._lock:
            self._messages.append(QueuedJob(job_id=job_id, request=request))

    async def receive(self) -> QueuedJob | None:
        """Take the next job, or None. See ``JobQueue.receive``."""
        async with self._lock:
            return self._messages.popleft() if self._messages else None

    async def delete(self, job: QueuedJob) -> None:
        """Accept a completed message. See ``JobQueue.delete``.

        A no-op: ``receive`` already removed it. Retained so this adapter
        satisfies the same contract as the Azure one, where deletion is what
        prevents redelivery.
        """
        return
