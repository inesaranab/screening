"""The service layer: the vendor-free core that owns the order of operations.

Screening runs in a fixed sequence::

    scrub -> [gate: fail closed on injection] -> assess (model) -> assemble

Scrubbing precedes every other step, so the model never receives raw candidate
data. When the guardrail reports injection, the model is not called and a
withheld result is returned in place of a score.

The work is split across three entry points so that accepting a screening and
producing its result are separate operations:

    - ``start`` records the job and returns its id.
    - ``run`` performs the screening and stores the outcome.
    - ``result`` reads the outcome back.

``run`` is called by whichever process performs the work; the service does not
depend on which.

Collaborators are declared as ports (``Guardrail``, ``LLMClient``, ``JobStore``,
``JobQueue``), so this module depends on no vendor or transport.
"""

import logging
import uuid
from datetime import UTC, datetime

from app.domain.models import (
    JOB_DEADLINE_SECONDS,
    Assessment,
    Flags,
    Job,
    JobStatus,
    NextStep,
    ScreenRequest,
    ScreenResult,
)
from app.ports.guardrail import Guardrail
from app.ports.job_queue import JobQueue
from app.ports.job_store import JobStore
from app.ports.llm import LLMClient

logger = logging.getLogger("screen")


class ScreenService:
    """Orchestrates one screening request across the guardrail and LLM ports."""

    def __init__(
        self,
        guardrail: Guardrail,
        llm: LLMClient,
        job_store: JobStore,
        job_queue: JobQueue,
    ) -> None:
        """Initialise the service with its collaborators.

        Args:
            guardrail: Redacts a transcript and reports what it found.
            llm: Produces an Assessment from a scrubbed transcript.
            job_store: Persists a job between acceptance and completion.
            job_queue: Carries accepted work to whoever performs it.
        """
        self._guardrail = guardrail
        self._llm = llm
        self._jobs = job_store
        self._queue = job_queue

    async def start(self, request: ScreenRequest) -> str:
        """Record a screening as pending, publish it, and return its id.

        Performs no screening. The guardrail and model are not called.

        The job is recorded before it is published, so a worker can never
        receive an id that has no corresponding job.

        A publish that raises leaves the job PENDING. The failure is ambiguous:
        the queue may have accepted the message before the error surfaced, in
        which case a worker will still perform the screening. Recording FAILED
        would then be contradicted by a result arriving afterwards, and a
        status that a later event can contradict is worse than one that is
        merely incomplete.

        Args:
            request: The transcript and job description to assess.

        Returns:
            The job id, to be passed to ``result``.

        Raises:
            Exception: Whatever publishing raised. The caller therefore never
                receives an id for a job that may not run.
        """
        job_id = uuid.uuid4().hex
        await self._jobs.create(job_id)
        try:
            await self._queue.enqueue(job_id, request)
        except Exception:
            logger.exception("enqueue_failed", extra={"context": {"job": job_id}})
            raise
        return job_id

    async def abandon(self, job_id: str, reason: str) -> None:
        """Record a job as failed without attempting it.

        For work that cannot be completed however many times it is tried, where
        another attempt would repeat the failure rather than resolve it.

        Only a job still PENDING is settled, and the store decides that as one
        operation. Work is given up on because it was delivered too often, and a
        job can be delivered again after its outcome was recorded, so a job
        already carrying an answer keeps it -- including one that acquires an
        answer while this call is in flight.

        Args:
            job_id: The id returned by ``start``.
            reason: Why the job was given up on. Must not quote the transcript.
        """
        if await self._jobs.fail_if_pending(job_id, reason):
            logger.warning("job_abandoned", extra={"context": {"job": job_id}})
        else:
            logger.info("job_abandon_skipped", extra={"context": {"job": job_id}})

    async def run(self, job_id: str, request: ScreenRequest) -> None:
        """Perform the screening and store its outcome against the job.

        Does not raise. Any exception is recorded as a failed job, so the job
        always leaves the PENDING state.

        The stored error is the exception class name only. Exception messages
        may quote the transcript, and the job store is not covered by the
        guardrail. Full detail is written to the log instead.

        Args:
            job_id: The id returned by ``start``.
            request: The transcript and job description to assess.
        """
        try:
            result = await self.screen(request)
        except Exception as exc:
            logger.exception("screen_job_failed", extra={"context": {"job": job_id}})
            await self._jobs.fail(job_id, type(exc).__name__)
            return
        await self._jobs.complete(job_id, result)

    async def result(self, job_id: str) -> Job | None:
        """Return a job's current state.

        A job still PENDING past JOB_DEADLINE_SECONDS is settled as failed
        first. The queue expires the message carrying the work, so a job can
        stop being any worker's responsibility without a worker having touched
        it; nothing else would move it out of PENDING, and a caller polling it
        would be told to wait indefinitely.

        Settling on read rather than on a schedule keeps the answer correct
        without a separate process having to be running for it to be correct.

        Args:
            job_id: The id returned by ``start``.

        Returns:
            The Job, or None if no job with that id exists.
        """
        job = await self._jobs.get(job_id)
        if job is None or job.status is not JobStatus.PENDING:
            return job
        age = (datetime.now(UTC) - job.created_at).total_seconds()
        if age < JOB_DEADLINE_SECONDS:
            return job
        await self._jobs.fail(job_id, "Expired")
        return await self._jobs.get(job_id)

    async def screen(self, request: ScreenRequest) -> ScreenResult:
        """Screen a candidate transcript against a job description.

        Args:
            request: The transcript and job description to assess.

        Returns:
            A ScreenResult. On the normal path it carries the model's
            assessment and the reviewer flags. When injection is detected it
            carries a withheld result: no fit score, ``next_step`` set to
            REQUEST_MORE_INFO, and ``out_of_scope`` set.
        """
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
