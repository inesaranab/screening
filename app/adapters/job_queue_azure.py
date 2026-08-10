"""Adapter: JobQueue backed by an Azure Storage Queue.

The queue is serverless, so it holds no compute that would prevent the rest of
the system scaling to zero, and it is what KEDA scales the worker on.

The request travels inside the message. An unredacted transcript therefore
exists only while the work is outstanding and is destroyed when the message is
deleted; nothing persists it.

Authentication is by managed identity; no storage key is read or configured.
"""

import json
from typing import Any, Protocol

from app.domain.models import ScreenRequest
from app.ports.job_queue import QueuedJob


class QueueClientLike(Protocol):
    """The subset of ``azure.storage.queue.aio.QueueClient`` this adapter uses."""

    async def send_message(self, content: str) -> Any: ...

    def receive_messages(self, **kwargs: Any) -> Any: ...

    async def delete_message(self, message: Any, pop_receipt: Any = None) -> Any: ...


def encode_message(job_id: str, request: ScreenRequest) -> str:
    """Serialise a job into a queue message.

    Args:
        job_id: The id already recorded in the job store.
        request: The transcript and job description to screen.

    Returns:
        A JSON string carrying both.
    """
    return json.dumps({"job_id": job_id, "request": request.model_dump()})


def decode_message(content: str) -> tuple[str, ScreenRequest]:
    """Reverse ``encode_message``.

    Args:
        content: The message body.

    Returns:
        The job id and the request it carries.
    """
    payload = json.loads(content)
    return payload["job_id"], ScreenRequest(**payload["request"])


class AzureJobQueue:
    """JobQueue backed by an Azure Storage Queue."""

    def __init__(self, client: QueueClientLike, visibility_timeout: int = 1800) -> None:
        """Initialise the queue.

        Args:
            client: A client for the queue. Injected rather than constructed
                here so the composition root owns its lifetime and tests can
                supply a double.
            visibility_timeout: Seconds a received message stays hidden from
                other consumers. Must exceed the longest screening, which is
                dominated by the detector's cold start of roughly 13 minutes;
                a shorter window would hand the same job to a second worker
                while the first is still waiting on the GPU.
        """
        self._client = client
        self._visibility_timeout = visibility_timeout

    async def enqueue(self, job_id: str, request: ScreenRequest) -> None:
        """Publish a job. See ``JobQueue.enqueue``."""
        await self._client.send_message(encode_message(job_id, request))

    async def receive(self) -> QueuedJob | None:
        """Take the next job, or None. See ``JobQueue.receive``."""
        async for message in self._client.receive_messages(
            messages_per_page=1, visibility_timeout=self._visibility_timeout
        ):
            job_id, request = decode_message(message.content)
            return QueuedJob(job_id=job_id, request=request, receipt=message)
        return None

    async def delete(self, job: QueuedJob) -> None:
        """Remove a processed message. See ``JobQueue.delete``."""
        await self._client.delete_message(job.receipt)
