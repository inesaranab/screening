"""Configuration and secrets supplied by the environment."""

from typing import Annotated
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment variables (prefix ``SCREENING_``).

    Values fall back to a local-Ollama default so the app runs out of the box,
    except the service API key, which is required.

    Attributes:
        llm_base_url: Base URL of the OpenAI-compatible model endpoint.
        llm_api_key: API key for that endpoint (ignored by Ollama).
        llm_model: Model name to request.
        llm_guardrail_base_url: Base URL for the self-hosted LLM used by the guardrail
        llm_guardrail_model: Model name to request at that endpoint.
        llm_timeout_s: Per-request timeout for the assessment LLM, in seconds.
        llm_guardrail_timeout_s: Per-request timeout for the guardrail endpoint.
            Separate from the assessment timeout because that endpoint runs a
            larger model, and bounded below the platform's own request limit so
            it can actually fire. It does not cover the endpoint starting from
            zero; readiness is established by repeated probes before any
            screening runs.
        jobs_account_url: Table endpoint of the account holding job state.
        jobs_queue_url: Queue endpoint of the same account.
        jobs_table_name: Table holding one entity per screening job.
        jobs_queue_name: Queue carrying accepted job ids to the worker.
        service_api_key: Shared key clients must send to call this service.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SCREENING_", extra="ignore"
    )
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    llm_guardrail_base_url: str = "http://localhost:8001/v1"
    llm_guardrail_model: str = "google/gemma-4-31B-it"
    llm_timeout_s: float = 60.0
    # Below the 240 seconds at which ingress severs any single request, internal
    # routes included. A larger value cannot fire, so the call would end as a
    # transport error from the proxy rather than a timeout naming this endpoint.
    #
    # It does not have to cover the detector's cold start. The worker establishes
    # that the endpoint is serving by repeating a short probe before it screens
    # anything (see app/worker.py), so this bounds a call to an endpoint already
    # known to be up. Larger than the assessment timeout because this endpoint
    # runs a 31B model on one replica.
    llm_guardrail_timeout_s: float = 200.0

    # Job state and the work queue. A separate account from the model-weights
    # share: that one is kind=FileStorage, which serves file shares only and has
    # no table or queue endpoint.
    jobs_account_url: str = "https://screeningjobs.table.core.windows.net/"
    jobs_queue_url: str = "https://screeningjobs.queue.core.windows.net/"
    jobs_table_name: str = "jobs"
    jobs_queue_name: str = "screenings"

    # No default and non-empty on purpose: the app refuses to start without a
    # real key, so auth can never be silently disabled by a missing OR empty
    # env var.
    service_api_key: Annotated[str, Field(min_length=1)]
    portkey_api_key: str = ""
    portkey_virtual_key: str = ""

    @field_validator("llm_guardrail_base_url")
    @classmethod
    def _remote_detector_must_be_https(cls, url: str) -> str:
        """Refuse a remote detector address that is not https.

        A detector behind ingress that refuses plain HTTP answers it with a
        redirect. Readiness still passes, because the probe follows the
        redirect and receives a 200, so the worker wakes the GPU and only then
        fails every screening: following a redirect turns the guardrail's POST
        into a GET, which the endpoint rejects. Refusing the address at startup
        costs a clear error instead of a cold start.

        A local address is exempt, having no ingress in front of it.

        Args:
            url: The configured detector address.

        Returns:
            The address, unchanged.

        Raises:
            ValueError: The address is remote and does not use https.
        """
        host = urlparse(url).hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            return url
        if not url.startswith("https://"):
            raise ValueError(
                f"llm_guardrail_base_url must be https for a remote host, got {url!r}"
            )
        return url


settings = Settings()  # type: ignore[call-arg]
