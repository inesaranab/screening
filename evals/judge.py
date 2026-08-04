# evals/judge.py
"""DeepEval judge model, routed through Portkey"""

from deepeval.models import DeepEvalBaseLLM
from openai import AsyncOpenAI, OpenAI

from app.config import settings


def _headers() -> dict[str, str] | None:
    if not settings.portkey_api_key:
        return None
    return {
        "x-portkey-api-key": settings.portkey_api_key,
        "x-portkey-virtual-key": settings.portkey_virtual_key,
    }


class PortkeyJudge(DeepEvalBaseLLM):
    def load_model(self) -> OpenAI:  # ty: ignore[invalid-method-override]
        return OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_s,
            default_headers=_headers(),
        )

    def get_model_name(self) -> str:
        return settings.llm_model

    def generate(self, prompt: str, *args, **kwargs):
        resp = self.load_model().chat.completions.create(
            model=settings.llm_model, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content or ""

    async def a_generate(self, prompt: str, *args, **kwargs) -> str:
        client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_s,
            default_headers=_headers(),
        )
        resp = await client.chat.completions.create(
            model=settings.llm_model, messages=[{"role": "user", "content": prompt}]
        )
        await client.close()
        return resp.choices[0].message.content or ""
