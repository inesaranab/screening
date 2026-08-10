"""The contract for /screen — the Pydantic types every layer depends on."""

from enum import Enum

from pydantic import BaseModel, Field


class ScreenRequest(BaseModel):
    """The request body for a screening.

    Attributes:
        transcript: Candidate interview transcript. Untrusted input.
        job_description: The role being screened for.
    """

    transcript: str = Field(min_length=1, description="Candidate interview transcript.")
    job_description: str = Field(
        min_length=1, description="The role being screened for."
    )


class NextStep(str, Enum):
    """Suggested next action for the recruiter.

    Attributes:
        ADVANCE: Move the candidate forward.
        REJECT: Do not proceed with the candidate.
        REQUEST_MORE_INFO: Neither, pending further information.
    """

    ADVANCE = "advance"
    REJECT = "reject"
    REQUEST_MORE_INFO = "more_info"


class ScrubResult(BaseModel):
    """The output of the guardrail.

    Attributes:
        clean_text: The redacted text. This, never the raw input, is what the
            model receives.
        pii_redacted: Whether any PII or special-category span was removed.
        injection_detected: Whether instruction-like content was found and
            neutralised.
    """

    clean_text: str
    pii_redacted: bool = False
    injection_detected: bool = False


class Assessment(BaseModel):
    """The model's structured output, validated by the LLM adapter.

    Attributes:
        fit_score: 1 (poor fit) to 5 (strong fit). None when the input was
            withheld and no score was produced.
        rationale: Explanation of the score, grounded in the evidence.
        evidence: Quotes or facts from the transcript backing the score.
        next_step: Suggested next action for the recruiter to review.
    """

    fit_score: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="1 (poor fit) to 5 (strong fit); None when the input was withheld.",
    )
    rationale: str = Field(
        min_length=1,
        description="Explanation of the score, grounded in the evidence.",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Quotes or facts from the transcript backing the score.",
    )
    next_step: NextStep = Field(
        description="Suggested next action for the recruiter to review."
    )


class Flags(BaseModel):
    """Signals the system raises for the reviewer, distinct from the model's claims.

    Attributes:
        injection_detected: The transcript contained instruction-like content.
        pii_redacted: PII or protected attributes were removed before the model
            was called.
        low_confidence: The score should be treated with caution.
        out_of_scope: The input was withheld, or did not support a real
            assessment.
    """

    injection_detected: bool = False
    pii_redacted: bool = False
    low_confidence: bool = False
    out_of_scope: bool = False


class ScreenResult(BaseModel):
    """The response body for a completed screening.

    Attributes:
        assessment: The model's structured judgement.
        flags: Signals raised by the system for the reviewer.
        decision_support_only: Always True. The output supports a human
            decision and does not replace it.
    """

    assessment: Assessment
    flags: Flags = Field(default_factory=Flags)
    decision_support_only: bool = True


class JobStatus(str, Enum):
    """The state of a screening job.

    Attributes:
        PENDING: Accepted, not yet finished.
        DONE: Finished, with a result.
        FAILED: Finished, with an error and no result.
    """

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class Job(BaseModel):
    """One screening, tracked between being accepted and being answered.

    Exactly one of `result` and `error` is set once `status` leaves PENDING.

    Attributes:
        id: Opaque handle the caller polls with.
        status: Where the job is in its life.
        result: The completed screening. Set when status is DONE.
        error: Why the screening failed. Set when status is FAILED.
    """

    id: str = Field(min_length=1, description="Opaque handle the caller polls with.")
    status: JobStatus = JobStatus.PENDING
    result: ScreenResult | None = Field(
        default=None, description="The completed screening. Set when status is DONE."
    )
    error: str | None = Field(
        default=None,
        description="Why the screening failed. Set when status is FAILED.",
    )
