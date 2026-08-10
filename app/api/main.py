"""HTTP adapter + composition root.

The one place that knows both FastAPI and the concrete adapters: it builds the
guardrail + LLM client once at startup and injects them into ScreenService.
Everything below this layer (domain, ports) stays vendor-free.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)

from app.adapters.guard_classifier import ClassifierGuardrail
from app.adapters.job_store_table import AzureTableJobStore
from app.adapters.llm_openai import OpenAICompatibleLLM
from app.config import settings
from app.domain.models import Job, JobStatus, ScreenRequest
from app.domain.service import ScreenService
from app.logging_config import setup_logging

logger = logging.getLogger("screen")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the adapters once at startup and release them at shutdown."""
    setup_logging()
    guardrail = ClassifierGuardrail()
    llm = OpenAICompatibleLLM()

    # Managed identity, so no storage key or connection string is configured.
    # DefaultAzureCredential resolves to the container app's assigned identity
    # in Azure and to the developer's az-cli login locally.
    credential = DefaultAzureCredential()
    table = TableClient(
        endpoint=settings.jobs_account_url,
        table_name=settings.jobs_table_name,
        credential=credential,
    )

    app.state.service = ScreenService(
        guardrail=guardrail, llm=llm, job_store=AzureTableJobStore(table)
    )
    yield
    await llm.aclose()
    await table.close()
    await credential.close()


app = FastAPI(title="Screening /screen", lifespan=lifespan)


# authentication
def require_api_key(x_api_key: Annotated[str, Header()] = "") -> None:
    # Compare as bytes: secrets.compare_digest raises TypeError on non-ASCII str,
    # which would surface as a 500 instead of a 401. Reject an empty header up
    # front so a bad/missing key can never slip through.
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode("utf-8"), settings.service_api_key.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def get_service(request: Request) -> ScreenService:
    return request.app.state.service


@app.post("/screen", status_code=status.HTTP_202_ACCEPTED, response_model=Job)
async def screen(
    request: ScreenRequest,
    background: BackgroundTasks,
    service: Annotated[ScreenService, Depends(get_service)],
    auth: Annotated[None, Depends(require_api_key)],
) -> Job:
    """Accept a screening and hand back a handle to poll with.

    202, not 200: the answer does not exist yet. The Article 9 detector scales
    to zero and takes minutes to wake, while Azure's ingress closes any request
    at 240 seconds -- so a synchronous result is a promise the platform will not
    let us keep. Measured 2026-08-10: a cold request died at exactly 240s with a
    504 from the edge, before the app was consulted at all.

    The work runs in a background task for now. That is the weak part of this
    design: it lives in the web process, so the app cannot scale to zero while
    a job is in flight. Moving it to a queue-triggered worker is the next step
    and needs no change here -- `run` already does not care who calls it.
    """
    job_id = await service.start(request)
    background.add_task(service.run, job_id, request)
    logger.info("screen_accepted", extra={"context": {"job": job_id}})
    return Job(id=job_id)


@app.get("/screen/{job_id}", response_model=Job)
async def screen_result(
    job_id: str,
    response: Response,
    service: Annotated[ScreenService, Depends(get_service)],
    auth: Annotated[None, Depends(require_api_key)],
) -> Job:
    """Fetch a screening.

    The HTTP status answers "is it ready"; the body answers "what happened".
    A finished-but-failed job is 200, not an error: the read succeeded, and the
    poller needs to see `status: failed` so it can stop rather than keep asking.
    """
    job = await service.result(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such job."
        )
    if job.status is JobStatus.PENDING:
        response.status_code = status.HTTP_202_ACCEPTED
    return job


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
