"""Port: the llm boundary"""

from typing import Protocol

from app.domain.models import Assessment


class LLMClient(Protocol):
    async def assess(self, transcript: str, job_description: str) -> Assessment:
        """Turn a (scrubbed) transcript + JD into a validated Assessment.

        Implementations must return a valid `Assessment` or raise — reasking on
        malformed model output is the adapter's responsibility, not the core's.
        """
        ...
