import pytest
from pydantic import ValidationError

from app.domain.models import Assessment, NextStep, ScreenRequest


# 1. fit_score must be 1-5
def test_fit_score_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Assessment(fit_score=6, rationale="x", next_step=NextStep.ADVANCE)


# 2. a valid one is accepted
def test_valid_assessment_defaults_evidence_to_empty_list():
    a = Assessment(fit_score=4, rationale="ok", next_step=NextStep.ADVANCE)
    assert a.evidence == []


# 3. empty transcript rejected
def test_empty_transcript_rejected():
    with pytest.raises(ValidationError):
        ScreenRequest(transcript="", job_description="Backend developer")


def test_a_transcript_too_large_for_the_detector_is_rejected():
    """The detector is served with --max-model-len 8192, so a transcript beyond
    that cannot be processed: vLLM returns a 400 and the request surfaces as a
    502 blaming the assessment model. Rejecting it here gives the caller an
    accurate 422 instead."""
    import pytest
    from pydantic import ValidationError

    from app.domain.models import MAX_TRANSCRIPT_CHARS, ScreenRequest

    with pytest.raises(ValidationError):
        ScreenRequest(
            transcript="x" * (MAX_TRANSCRIPT_CHARS + 1), job_description="Backend"
        )


def test_a_transcript_at_the_limit_is_accepted():
    from app.domain.models import MAX_TRANSCRIPT_CHARS, ScreenRequest

    request = ScreenRequest(
        transcript="x" * MAX_TRANSCRIPT_CHARS, job_description="Backend"
    )

    assert len(request.transcript) == MAX_TRANSCRIPT_CHARS
