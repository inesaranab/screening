"""The worker: performs screenings taken from the queue.

Runs as a Container Apps Job rather than inside the API. A job has no ingress,
so nothing holds an HTTP connection while the detector wakes -- Azure closes a
request at 240 seconds and a cold detector takes roughly 13 minutes.

The job exits once the queue is empty, and KEDA starts another when messages
arrive, so no process runs while there is no work.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from app.adapters.detector_readiness import http_probe, wait_until_ready
from app.adapters.guard_classifier import ClassifierGuardrail
from app.adapters.job_queue_azure import AzureJobQueue
from app.adapters.job_store_table import AzureTableJobStore
from app.adapters.llm_openai import OpenAICompatibleLLM
from app.config import settings
from app.domain.service import ScreenService
from app.logging_config import setup_logging
from app.ports.job_queue import JobQueue

logger = logging.getLogger("screen")

# How many times a message may be delivered before the job is given up on. A
# screening that failed is recorded and its message deleted, so redelivery only
# happens when a worker died before recording anything. Past this count the
# cause is not transient, and further attempts would keep an unredacted
# transcript on the queue while repeating the same failure.
MAX_DELIVERIES = 3

# How long to wait for the detector to start serving, and how often to ask.
# Loading the weights takes minutes; the deadline sits below the job's own
# replicaTimeout so an unreachable detector ends as a recorded failure rather
# than a killed replica. Each probe is a separate short request, because
# platform ingress closes any single request at 240 seconds -- the wait has to
# happen between requests, never inside one.
DETECTOR_READY_DEADLINE_S = 900.0
DETECTOR_PROBE_INTERVAL_S = 15.0

# A probe is an ordinary request, so ingress severs it at 240 seconds like any
# other. It is set well above a healthy endpoint's response time because the
# platform holds the request open while it starts a replica for an app scaled to
# zero: a probe that gives up in seconds can abandon that start before a replica
# exists, and the endpoint is then never reached however often it is retried.
DETECTOR_PROBE_TIMEOUT_S = 180.0

# How long the queue hides a message it has handed out. Everything one job can
# wait for has to fit inside it, or the message is redelivered while the
# execution holding it is still working.
VISIBILITY_TIMEOUT_S = 1800.0


async def drain(
    service: ScreenService,
    queue: JobQueue,
    *,
    detector_ready: Callable[[], Awaitable[bool]] | None = None,
) -> int:
    """Screen every job currently on the queue.

    A message is deleted once the screening has been recorded, whether it
    succeeded or failed. ``ScreenService.run`` stores failures rather than
    raising, so a failed screening is finished work; leaving its message would
    redeliver a job that fails identically, indefinitely.

    A message whose worker dies before deletion becomes visible again and is
    retried, which is the intended behaviour for a crash. Past MAX_DELIVERIES
    the job is recorded as failed and its message deleted without being
    attempted, so a job that kills every worker cannot cycle indefinitely.

    A job taken while the detector is unreachable is recorded as failed rather
    than attempted. Attempting it would spend the platform's entire request
    budget on a call that cannot succeed, and end in the same failure.

    Args:
        service: Performs the screening and records the outcome.
        queue: Supplies the work.
        detector_ready: Returns True once the detector can be called. None
            skips the check, for callers that supply their own guardrail.

    Returns:
        The number of jobs screened. Abandoned jobs are not counted, having
        never been attempted.
    """
    processed = 0
    while (job := await queue.receive()) is not None:
        if job.delivery_count > MAX_DELIVERIES:
            await service.abandon(job.job_id, "TooManyDeliveries")
            await queue.delete(job)
            continue
        if detector_ready is not None and not await detector_ready():
            await service.abandon(job.job_id, "DetectorUnavailable")
            await queue.delete(job)
            continue
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

    # The first request also activates the detector, since it scales to zero,
    # so probing both starts it and establishes when it is usable.
    # follow_redirects, because the detector's ingress refuses plain HTTP and
    # answers with a redirect to HTTPS. A client that does not follow it sees a
    # non-200 forever, and the redirect alone does not start an app scaled to
    # zero, so the endpoint would never become reachable.
    http = httpx.AsyncClient(timeout=DETECTOR_PROBE_TIMEOUT_S, follow_redirects=True)
    probe = http_probe(http, f"{settings.llm_guardrail_base_url.rstrip('/')}/models")

    async def detector_ready() -> bool:
        return await wait_until_ready(
            probe,
            deadline_s=DETECTOR_READY_DEADLINE_S,
            interval_s=DETECTOR_PROBE_INTERVAL_S,
            sleep=asyncio.sleep,
        )

    try:
        processed = await drain(service, queue, detector_ready=detector_ready)
        logger.info("worker_drained", extra={"context": {"processed": processed}})
    finally:
        await http.aclose()
        await llm.aclose()
        await queue_client.close()
        await table.close()
        await credential.close()


if __name__ == "__main__":
    asyncio.run(main())
