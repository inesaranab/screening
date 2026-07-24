"""The contract for /screen"""

from enum import Enum

from pydantic import BaseModel, Field


# input
class ScreenRequest(BaseModel):
    transcript: str = Field(min_length=1, description="Candidate interview transcript.")
    job_description: str = Field(
        min_length=1, description="The role being screened for."
    )


# base model for human in the loop
class NextStep(str, Enum):
    ADVANCE = "advance"
    REJECT = "reject"
    REQUEST_MORE_INFO = "more_info"


# guardrails output
class ScrubResult(BaseModel):
    clean_text: str  # what the llm recieves
    pii_redacted: bool = False  # signals if PII attributes were removed -> Flags
    injection_detected: bool = (
        False  # signals if instruction-like content was found and neutralized -> Flags
    )


# the model's structured output (validated by the LLM adapter)
class Assessment(BaseModel):
    fit_score: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="1 (poor fit) to 5 (strong fit). Out of range = invalid.",
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


# flags the system adds to inform reviewers
class Flags(BaseModel):
    injection_detected: bool = False  # transcript contained instruction-like content
    pii_redacted: bool = False  # protected attributes
    low_confidence: bool = False  # score should be treated with caution
    out_of_scope: bool = False  # transcript/JD didn't support a real assessment TODO: the LLM should not assess the transcript as it could be compromised, solutions: embedd the transcript with a cosine model, measure similarity on the score.


# result
class ScreenResult(BaseModel):
    assessment: Assessment
    flags: Flags = Field(default_factory=Flags)
    # This parameter is set to true to support a future Human in the loop
    decision_support_only: bool = True
