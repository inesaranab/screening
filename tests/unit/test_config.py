from app.config import Settings


def test_llm_guardrail_settings_have_defaults():
    settings = Settings()
    assert settings.llm_guardrail_base_url == "http://localhost:8001/v1"
    assert settings.llm_guardrail_model == "google/gemma-4-31B-it"


def test_guardrail_timeout_is_long_enough_for_a_cold_start():
    """The guardrail endpoint scales to zero, so the first request after an idle
    period waits for an A100 to boot and load 58 GiB of weights -- measured at
    ~13 minutes. Sharing `llm_timeout_s` (60s) with the assessment LLM means
    every cold request times out, and the recognizer fails closed, so /screen
    returns 502 on the normal path rather than an exceptional one."""
    from app.config import settings

    assert settings.llm_guardrail_timeout_s >= 900
