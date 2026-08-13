from app.config import Settings


def test_llm_guardrail_settings_have_defaults(monkeypatch):
    """Asserting a default requires nothing to be overriding it. Settings reads
    a .env file and SCREENING_ variables, either of which would otherwise make
    this assert the host's configuration instead of the declared default."""
    for name in ("SCREENING_LLM_GUARDRAIL_BASE_URL", "SCREENING_LLM_GUARDRAIL_MODEL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.llm_guardrail_base_url == "http://localhost:8001/v1"
    assert settings.llm_guardrail_model == "google/gemma-4-31B-it"


def test_guardrail_timeout_expires_before_the_platform_severs_the_call():
    """Ingress closes any single request at 240 seconds, internal routes
    included. A timeout above that can never fire, so the call ends as a
    transport error from the proxy instead of a timeout naming the endpoint.
    The wait for a cold detector happens across repeated probes, not inside this
    request, so this bounds a call to an endpoint already known to be serving."""
    from app.config import settings

    assert settings.llm_guardrail_timeout_s < 240
    assert settings.llm_guardrail_timeout_s > settings.llm_timeout_s


def test_jobs_storage_defaults_point_at_the_general_purpose_account():
    """The premium file-share account cannot host a table or a queue, so job
    state lives in a separate general-purpose account."""
    from app.config import Settings

    assert "screeningjobs" in Settings.model_fields["jobs_account_url"].default
    assert Settings.model_fields["jobs_table_name"].default
    assert Settings.model_fields["jobs_queue_name"].default


def test_a_plain_http_detector_address_is_refused():
    """The detector's ingress answers plain HTTP with a redirect rather than
    serving it, and a client following that redirect turns the POST into a GET,
    which the endpoint rejects with 405. Readiness still passes -- the probe
    follows the redirect and gets a 200 -- so the failure arrives only after a
    cold start has been paid for."""
    import pytest
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(llm_guardrail_base_url="http://screening-gemma.internal.example/v1")


def test_a_local_http_detector_address_is_allowed():
    """A detector served on localhost has no ingress in front of it, so the
    scheme carries none of the same risk."""
    from app.config import Settings

    settings = Settings(llm_guardrail_base_url="http://localhost:8001/v1")

    assert settings.llm_guardrail_base_url.startswith("http://")
