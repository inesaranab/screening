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
    """A judge whose clients are built once per instance, not once per call.

    Both clients are cached deliberately: a metric run issues one judge call per
    statement/claim, so building a fresh ``OpenAI``/``AsyncOpenAI`` each time
    would open (and never close) a connection pool per call and exhaust file
    descriptors well before the suite finishes.
    """

    _sync_client: OpenAI | None = None
    _aclient: AsyncOpenAI | None = None

    def load_model(self) -> OpenAI:  # ty: ignore[invalid-method-override]
        if self._sync_client is None:
            self._sync_client = OpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                timeout=settings.llm_timeout_s,
                default_headers=_headers(),
            )
        return self._sync_client

    def get_model_name(self) -> str:
        return settings.llm_model

    def generate(self, prompt: str, *args, **kwargs) -> str:
        resp = self.load_model().chat.completions.create(
            model=settings.llm_model, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content or ""

    async def a_generate(self, prompt: str, *args, **kwargs) -> str:
        # Reused rather than closed after each call: the previous per-call
        # `await client.close()` sat *after* the request, so any API error
        # skipped it and leaked the client.
        if self._aclient is None:
            self._aclient = AsyncOpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                timeout=settings.llm_timeout_s,
                default_headers=_headers(),
            )
        resp = await self._aclient.chat.completions.create(
            model=settings.llm_model, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content or ""
