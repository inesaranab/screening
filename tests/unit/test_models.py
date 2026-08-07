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
