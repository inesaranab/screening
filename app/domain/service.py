"""The service layer.

The ordering of operations is as follows:

    scrub (remove PII + injection)  →  assess (model)  →  assemble result
"""

from app.domain.models import (
    Assessment,
    Flags,
    NextStep,
    ScreenRequest,
    ScreenResult,
)
from app.ports.guardrail import Guardrail
from app.ports.llm import LLMClient


class ScreenService:
    def __init__(self, guardrail: Guardrail, llm: LLMClient) -> None:
        self._guardrail = guardrail
        self._llm = llm

    async def screen(self, request: ScreenRequest) -> ScreenResult:
        # 1. Scrub the raw transcript.
        scrub = await self._guardrail.scrub(request.transcript)

        # 2. Fail closed on injection: never let the model score tampered input.
        #    We short-circuit — no model call — and mark it out of scope so the
        #    recruiter sees a withheld result, not a fabricated score.
        if scrub.injection_detected:
            return ScreenResult(
                assessment=Assessment(
                    fit_score=None,
                    rationale=(
                        "Not scored: the transcript contained instruction-like "
                        "content (possible prompt injection) and was withheld "
                        "from the model."
                    ),
                    evidence=[],
                    next_step=NextStep.REQUEST_MORE_INFO,
                ),
                flags=Flags(
                    injection_detected=True,
                    pii_redacted=scrub.pii_redacted,
                    low_confidence=True,
                    out_of_scope=True,
                ),
            )

        # 3. Ask the model over the cleaned text.
        assessment = await self._llm.assess(scrub.clean_text, request.job_description)

        # 4. Assemble flags for the reviewer.
        flags = Flags(
            injection_detected=False,
            pii_redacted=scrub.pii_redacted,
            low_confidence=not assessment.evidence,
        )
        return ScreenResult(assessment=assessment, flags=flags)
