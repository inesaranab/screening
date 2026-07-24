"""Port: the guardrail boundary."""

from typing import Protocol

from app.domain.models import ScrubResult


class Guardrail(Protocol):
    async def scrub(self, text: str) -> ScrubResult:
        """Redact PII / protected attributes and neutralise injection framing.

        Returns the cleaned text plus what was found - never mutates the input.
        """
        ...
