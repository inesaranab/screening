"""The Job model: the domain's vocabulary for work that is not finished yet."""

import pytest
from pydantic import ValidationError

from app.domain.models import (
    Assessment,
    Job,
    JobStatus,
    NextStep,
    ScreenResult,
)


def _a_result() -> ScreenResult:
    return ScreenResult(
        assessment=Assessment(
            fit_score=4,
            rationale="Six years of Python on payments systems.",
            next_step=NextStep.ADVANCE,
        )
    )


def test_a_new_job_is_pending_and_carries_no_result():
    job = Job(id="abc123")

    assert job.status is JobStatus.PENDING
    assert job.result is None
    assert job.error is None


def test_a_completed_job_carries_its_result():
    job = Job(id="abc123", status=JobStatus.DONE, result=_a_result())

    assert job.status is JobStatus.DONE
    assert job.result is not None
    assert job.result.assessment.fit_score == 4


def test_a_failed_job_carries_why_and_no_result():
    """Failed and pending must be distinguishable: a poller that cannot tell
    them apart waits forever on work that already died."""
    job = Job(id="abc123", status=JobStatus.FAILED, error="detector unreachable")

    assert job.status is JobStatus.FAILED
    assert job.error == "detector unreachable"
    assert job.result is None


def test_an_unknown_status_is_rejected():
    """A string status would let a typo ("compelte") sit in storage forever,
    read as neither done nor failed. The enum makes that a validation error."""
    with pytest.raises(ValidationError):
        Job(id="abc123", status="compelte")


def test_a_done_job_without_a_result_is_rejected():
    """DONE is what tells a poller to stop and read the answer. A DONE job with
    no result sends it to read nothing."""
    with pytest.raises(ValidationError):
        Job(id="abc123", status=JobStatus.DONE)


def test_a_failed_job_carrying_a_result_is_rejected():
    """FAILED means no assessment was produced. Carrying one contradicts the
    status, and which of the two a reader trusts is undefined."""
    with pytest.raises(ValidationError):
        Job(
            id="abc123",
            status=JobStatus.FAILED,
            error="Timeout",
            result=_a_result(),
        )


def test_a_pending_job_carrying_an_outcome_is_rejected():
    """PENDING means the work is outstanding. An outcome attached to it is
    either a leftover from a previous attempt or a bug."""
    with pytest.raises(ValidationError):
        Job(id="abc123", status=JobStatus.PENDING, error="Timeout")
