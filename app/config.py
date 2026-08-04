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
        llm_timeout_s: Per-request timeout, in seconds.
        service_api_key: Shared key clients must send to call this service.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SCREENING_", extra="ignore"
    )
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    llm_timeout_s: float = 60.0

    # No default and non-empty on purpose: the app refuses to start without a
    # real key, so auth can never be silently disabled by a missing OR empty
    # env var.
    service_api_key: Annotated[str, Field(min_length=1)]
    portkey_api_key: str = ""
    portkey_virtual_key: str = ""


settings = Settings()  # type ignore[call-arg]
