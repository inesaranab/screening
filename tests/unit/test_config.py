from app.config import Settings


def test_llm_guardrail_settings_have_defaults():
    settings = Settings()
    assert settings.llm_guardrail_base_url == "http://localhost:8001/v1"
    assert settings.llm_guardrail_model == "google/gemma-4-31B-it"
