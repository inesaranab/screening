"""Configuration and secrets supplied by the environment."""

from typing import Annotated

from pydantic import Field
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
            Deliberately separate and much larger: that endpoint scales to zero,
            so the first request after an idle period waits for a GPU to start
            and load the model.
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
    # 15 minutes, against a measured ~13 minute cold start (2 min image pull,
    # 1 min engine init, ~10 min loading 58 GiB of weights off the file share).
    # Sharing the 60s assessment timeout meant every request that arrived on a
    # cold endpoint timed out, and the recognizer fails closed -- so /screen
    # returned 502 on the normal path, not an exceptional one.
    #
    # A caller waiting 13 minutes is still bad; the real fix is for /screen to
    # return 202 and be polled (see infra/gemma/README.md). This makes the
    # blocking path correct in the meantime rather than silently broken.
    llm_guardrail_timeout_s: float = 900.0

    # No default and non-empty on purpose: the app refuses to start without a
    # real key, so auth can never be silently disabled by a missing OR empty
    # env var.
    service_api_key: Annotated[str, Field(min_length=1)]
    portkey_api_key: str = ""
    portkey_virtual_key: str = ""


settings = Settings()  # type: ignore[call-arg]
