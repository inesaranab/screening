"""Port: the guardrail boundary."""

from typing import Protocol

from app.domain.models import ScrubResult


class Guardrail(Protocol):
    """Anything that can scrub a transcript before the model sees it."""

    async def scrub(self, text: str) -> ScrubResult:
        """Redact PII / protected attributes and neutralise injection framing.

        Args:
            text: The raw candidate transcript.

        Returns:
            A ScrubResult with the cleaned text and what was found. Never
            mutates the input.
        """
        ...
