"""The worker: performs screenings taken from the queue.

Runs as a Container Apps Job rather than inside the API. A job has no ingress,
so nothing holds an HTTP connection while the detector wakes -- Azure closes a
request at 240 seconds and a cold detector takes roughly 13 minutes.

The job exits once the queue is empty, and KEDA starts another when messages
arrive, so no process runs while there is no work.
"""

import asyncio
import logging

from app.adapters.guard_classifier import ClassifierGuardrail
from app.adapters.job_queue_azure import AzureJobQueue
from app.adapters.job_store_table import AzureTableJobStore
from app.adapters.llm_openai import OpenAICompatibleLLM
from app.config import settings
from app.domain.service import ScreenService
from app.logging_config import setup_logging
from app.ports.job_queue import JobQueue

logger = logging.getLogger("screen")


async def drain(service: ScreenService, queue: JobQueue) -> int:
    """Screen every job currently on the queue.

    A message is deleted once the screening has been recorded, whether it
    succeeded or failed. ``ScreenService.run`` stores failures rather than
    raising, so a failed screening is finished work; leaving its message would
    redeliver a job that fails identically, indefinitely.

    A message whose worker dies before deletion becomes visible again and is
    retried, which is the intended behaviour for a crash.

    Args:
        service: Performs the screening and records the outcome.
        queue: Supplies the work.

    Returns:
        The number of jobs processed.
    """
    processed = 0
    while (job := await queue.receive()) is not None:
        logger.info("worker_job_started", extra={"context": {"job": job.job_id}})
        await service.run(job.job_id, job.request)
        await queue.delete(job)
        processed += 1
        logger.info("worker_job_finished", extra={"context": {"job": job.job_id}})
    return processed


async def main() -> None:
    """Build the adapters, drain the queue once, and release them."""
    setup_logging()

    from azure.data.tables.aio import TableClient
    from azure.identity.aio import DefaultAzureCredential
    from azure.storage.queue.aio import QueueClient

    credential = DefaultAzureCredential()
    table = TableClient(
        endpoint=settings.jobs_account_url,
        table_name=settings.jobs_table_name,
        credential=credential,
    )
    queue_client = QueueClient(
        account_url=settings.jobs_queue_url,
        queue_name=settings.jobs_queue_name,
        credential=credential,
    )
    llm = OpenAICompatibleLLM()
    queue = AzureJobQueue(queue_client)

    service = ScreenService(
        guardrail=ClassifierGuardrail(),
        llm=llm,
        job_store=AzureTableJobStore(table),
        job_queue=queue,
    )

    try:
        processed = await drain(service, queue)
        logger.info("worker_drained", extra={"context": {"processed": processed}})
    finally:
        await llm.aclose()
        await queue_client.close()
        await table.close()
        await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
