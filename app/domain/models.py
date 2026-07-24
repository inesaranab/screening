"""The contract for /screen — the Pydantic types every layer depends on."""

from enum import Enum

from pydantic import BaseModel, Field


class ScreenRequest(BaseModel):
    """The request body: one transcript screened against one job description."""

    transcript: str = Field(min_length=1, description="Candidate interview transcript.")
    job_description: str = Field(
        min_length=1, description="The role being screened for."
    )


class NextStep(str, Enum):
    """Suggested next action.

    An enum, not a free string, so the model can't invent an unhandled value —
    an out-of-spec step fails validation.
    """

    ADVANCE = "advance"
    REJECT = "reject"
    REQUEST_MORE_INFO = "more_info"


class ScrubResult(BaseModel):
    """What the guardrail returns: the cleaned text plus what it found."""

    clean_text: str  # the cleaned text the LLM receives — never the raw input
    pii_redacted: bool = False  # signals if PII attributes were removed -> Flags
    injection_detected: bool = (
        False  # signals if instruction-like content was found and neutralized -> Flags
    )


class Assessment(BaseModel):
    """The model's structured output, validated by the LLM adapter.

    Every field is a guarantee: Instructor coerces the raw completion into this
    shape and reasks on failure.
    """

    fit_score: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="1 (poor fit) to 5 (strong fit); None when the input was withheld (e.g. injection).",
    )
    rationale: str = Field(
        min_length=1,
        description="Short explanation of the score, grounded in the evidence.",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Quotes/facts from the transcript backing the score. Makes 'cite evidence' enforceable.",
    )
    next_step: NextStep = Field(
        description="Suggested next action for the recruiter to review"
    )


class Flags(BaseModel):
    """Signals the *system* raises for the reviewer — separate from the model's claims."""

    injection_detected: bool = False  # transcript contained instruction-like content
    pii_redacted: bool = False  # PII / protected attributes were removed pre-model
    low_confidence: bool = False  # score should be treated with caution
    out_of_scope: bool = (
        False  # input was withheld (injection) or didn't support a real assessment
    )


class ScreenResult(BaseModel):
    """The response envelope: decision-support for a human, never an autonomous verdict."""

    assessment: Assessment
    flags: Flags = Field(default_factory=Flags)
    # Standing reminder that this output supports a human decision, never replaces it.
    decision_support_only: bool = True
