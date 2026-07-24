"""Port: the llm boundary."""

from typing import Protocol

from app.domain.models import Assessment


class LLMClient(Protocol):
    """Anything that can turn a scrubbed transcript into a validated Assessment."""

    async def assess(self, transcript: str, job_description: str) -> Assessment:
        """Turn a (scrubbed) transcript + JD into a validated Assessment.

        Args:
            transcript: The already-scrubbed transcript.
            job_description: The role being screened for.

        Returns:
            A valid Assessment.

        Raises:
            Exception: On malformed model output the adapter can't repair —
                reasking is the adapter's responsibility, not the core's.
        """
        ...
