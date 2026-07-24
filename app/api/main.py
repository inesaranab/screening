"""HTTP adapter + composition root.

The one place that knows both FastAPI and the concrete adapters: it builds the
guardrail + LLM client once at startup and injects them into ScreenService.
Everything below this layer (domain, ports) stays vendor-free.
"""

import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from instructor.core.exceptions import InstructorRetryException
from openai import APIConnectionError, APITimeoutError

from app.adapters.guard_classifier import ClassifierGuardrail
from app.adapters.llm_openai import OpenAICompatibleLLM
from app.config import settings
from app.domain.models import ScreenRequest, ScreenResult
from app.domain.service import ScreenService
from app.logging_config import setup_logging

logger = logging.getLogger("screen")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire adapters into the service at startup; close the LLM client at exit.

    Args:
        app: The FastAPI app; the built service is stored on ``app.state``.
    """
    # Build the expensive adapters once at startup, tear the LLM client down at exit.
    setup_logging()
    guardrail = ClassifierGuardrail()
    llm = OpenAICompatibleLLM()
    app.state.service = ScreenService(guardrail=guardrail, llm=llm)
    yield
    await llm.aclose()


app = FastAPI(title="Screening /screen", lifespan=lifespan)


# authentication
def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Reject the request unless a valid API key is present.

    Args:
        x_api_key: The ``x-api-key`` request header.

    Raises:
        HTTPException: 401 if the key is missing or wrong. Compared in constant
            time so the check can't be timing-attacked.
    """
    if not secrets.compare_digest(x_api_key, settings.service_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def get_service(request: Request) -> ScreenService:
    """Return the singleton ScreenService built at startup (DI provider).

    Args:
        request: The incoming request, used to reach ``app.state``.

    Returns:
        The shared ScreenService instance.
    """
    return request.app.state.service


@app.post("/screen", response_model=ScreenResult)
async def screen(
    request: ScreenRequest,
    service: ScreenService = Depends(get_service),
    auth: None = Depends(require_api_key),
) -> ScreenResult:
    """Screen a candidate and return decision-support for a human reviewer.

    Args:
        request: The transcript and job description to assess.
        service: The screening service (injected).
        auth: Auth guard dependency; raises before the body runs if the key is bad.

    Returns:
        The ScreenResult on success.

    Raises:
        HTTPException: 504 on model timeout, 503 if the model is unreachable,
            502 on malformed model output or any other failure (fails closed,
            never leaking internals or candidate data).
    """
    started = time.perf_counter()
    try:
        result = await service.screen(request)
        logger.info(
            "screen_ok",
            extra={
                "context": {
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "fit_score": result.assessment.fit_score,
                    "injection_detected": result.flags.injection_detected,
                    "pii_redacted": result.flags.pii_redacted,
                    "low_confidence": result.flags.low_confidence,
                }
            },
        )
        return result
    except (APITimeoutError, APIConnectionError, InstructorRetryException) as exc:
        # Instructor WRAPS the underlying openai error in InstructorRetryException,
        # so we unwrap via __cause__ to map the real failure precisely. (Caught by
        # our eval suite: without this, every timeout became a generic 502.)
        cause = exc.__cause__ if isinstance(exc, InstructorRetryException) else exc
        if isinstance(cause, APITimeoutError):
            code, detail = (
                status.HTTP_504_GATEWAY_TIMEOUT,
                "The model timed out. Try again shortly.",
            )
        elif isinstance(cause, APIConnectionError):
            code, detail = (
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "The model service is unavailable.",
            )
        else:
            # Instructor exhausted retries on malformed/out-of-spec output.
            code, detail = (
                status.HTTP_502_BAD_GATEWAY,
                "The model returned unusable output.",
            )
        raise HTTPException(status_code=code, detail=detail)
    except Exception:
        logger.exception("screen_failed")
        # Fail closed with a generic message — never leak internals or candidate data.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The model returned unusable output.",
        )


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe.

    Returns:
        ``{"status": "ok"}`` when the process is serving.
    """
    return {"status": "ok"}
