"""The contract for /screen — the Pydantic types every layer depends on."""

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Self

from pydantic import BaseModel, Field, model_validator

# Derived from the detector's context window. It is served with
# --max-model-len 8192, covering the instructions, the transcript and the quotes
# the model emits back. Reserving roughly 1,200 tokens for instructions and
# output leaves ~7,000 for the transcript, at a conservative 3.5 characters per
# token. Raise this only alongside --max-model-len in infra/gemma/vllm-app.yaml.
MAX_TRANSCRIPT_CHARS = 24_000

# Derived from the 64 KiB ceiling on a queue message, which carries the job
# description alongside the transcript. An uncapped field turns an oversized
# posting into a transport error raised after the job is recorded, rather than a
# validation error naming the field.
MAX_JOB_DESCRIPTION_CHARS = 8_000

# Derived from the 64 KiB ceiling on a queue message, less headroom for the job
# id and the JSON that wraps both fields. The character caps above bound length;
# this bounds size, which is what the ceiling is actually expressed in. A
# character can occupy up to four UTF-8 bytes, and a JSON string expands a
# control character to six, so neither cap implies the other.
MAX_REQUEST_BYTES = 60_000

# How long a job may stay PENDING before it is treated as never going to finish.
# The queue that carries the work expires messages, so a job can stop being any
# worker's responsibility without a worker having touched it, and nothing would
# otherwise move it out of PENDING. Must exceed the queue's message lifetime plus
# one screening; below that, work still in progress would be declared dead.
JOB_DEADLINE_SECONDS = 5 * 60 * 60


def _json_string_bytes(value: str) -> int:
    """Measure a string as it occupies space inside a JSON document.

    Args:
        value: The text to measure.

    Returns:
        The UTF-8 byte length of the text escaped as a JSON string, quotes
        included. Non-ASCII characters are left as themselves, matching how the
        request is published.
    """
    return len(json.dumps(value, ensure_ascii=False).encode())


class ScreenRequest(BaseModel):
    """The request body for a screening.

    Attributes:
        transcript: Candidate interview transcript. Untrusted input, capped at
            what the detector's context window can process.
        job_description: The role being screened for, capped at what fits in a
            queue message alongside the transcript.
    """

    transcript: str = Field(
        min_length=1,
        max_length=MAX_TRANSCRIPT_CHARS,
        description="Candidate interview transcript.",
    )
    job_description: str = Field(
        min_length=1,
        max_length=MAX_JOB_DESCRIPTION_CHARS,
        description="The role being screened for.",
    )

    @model_validator(mode="after")
    def _fits_in_a_queue_message(self) -> Self:
        """Reject a request too large to publish.

        Both fields travel as JSON strings, so the size that counts is the
        escaped one: a control character occupies one byte in the field and six
        in the message. Measuring the field alone would pass a request that the
        transport then rejects, once the job has already been recorded.

        Returns:
            The request, unchanged.

        Raises:
            ValueError: If the two fields together exceed MAX_REQUEST_BYTES
                once escaped as JSON strings and encoded as UTF-8.
        """
        size = _json_string_bytes(self.transcript) + _json_string_bytes(
            self.job_description
        )
        if size > MAX_REQUEST_BYTES:
            raise ValueError(
                f"encoded request is {size} bytes, over the "
                f"{MAX_REQUEST_BYTES} allowed (MAX_REQUEST_BYTES)"
            )
        return self


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
        created_at: When the job was accepted. Establishes whether a job still
            pending is outstanding or abandoned.
    """

    id: str = Field(min_length=1, description="Opaque handle the caller polls with.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the job was accepted.",
    )
    status: JobStatus = JobStatus.PENDING
    result: ScreenResult | None = Field(
        default=None, description="The completed screening. Set when status is DONE."
    )
    error: str | None = Field(
        default=None,
        description="Why the screening failed. Set when status is FAILED.",
    )

    @model_validator(mode="after")
    def _payload_matches_status(self) -> Self:
        """Reject a job whose payload contradicts its status.

        Returns:
            The job, unchanged.

        Raises:
            ValueError: If DONE carries no result, FAILED carries no error,
                PENDING carries either, or a job carries both.
        """
        expected = {
            JobStatus.PENDING: (False, False),
            JobStatus.DONE: (True, False),
            JobStatus.FAILED: (False, True),
        }[self.status]
        if (self.result is not None, self.error is not None) != expected:
            raise ValueError(
                f"a {self.status.value} job must carry "
                f"{'a result' if expected[0] else 'an error' if expected[1] else 'neither'}"
                " and nothing else"
            )
        return self
