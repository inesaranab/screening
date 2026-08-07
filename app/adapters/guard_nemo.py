"""Adapter: the guardrail again, this time orchestrated by NeMo Guardrails.

Satisfies the same `Guardrail` port as ClassifierGuardrail, so the two are
interchangeable in the composition root and `ScreenService` never changes.

Detection is not reimplemented here -- ClassifierGuardrail still owns it
(injection classifier, Presidio, custom regexes, and Gemma-4 via
LLMGuardrailRecognizer). NeMo contributes orchestration: a declarative place to
see the rail sequence, and a home for output rails, which the hand-rolled
adapter has no equivalent of.

NeMo's own `sensitive_data_detection` rail is deliberately unused: it wraps bare
Presidio + spaCy, which benchmarked at 0.433 average F1 against the 0.762 of
what we already run, and it cannot see our UK_NINO/UK_POSTCODE/DOB recognizers.
The *shape* of that rail is copied though -- an action returns the masked
string and Colang reassigns `$user_message` to it.
"""

import logging
import pathlib

from nemoguardrails import LLMRails, RailsConfig

from app.adapters.guard_classifier import ClassifierGuardrail
from app.domain.models import ScrubResult
from app.ports.guardrail import Guardrail

logger = logging.getLogger("screen")

_CONFIG_DIR = pathlib.Path(__file__).parent / "guardrails_config"

_WITHHELD = "[flagged by injection classifier — content withheld from scoring]"

# NeMo catches exceptions raised inside an action, logs them, and lets the flow
# continue with the action's result as None -- which Colang then stringifies to
# "None". Without an explicit signal, a dead detector is indistinguishable from
# a successful scrub of a transcript that happens to read "None". The action
# catches its own failures and returns this sentinel so `scrub` can fail closed.
# Plain ASCII on purpose: Colang interpolates the value into an expression it
# then evaluates, and control characters (a null byte, originally) raise
# ColangValueError there -- the sentinel would never reach `scrub` at all.
_RAIL_FAILED = "__GUARDRAIL_RAIL_FAILED__"


class NemoGuardrail:
    """Runs the existing detection stack through NeMo input rails."""

    def __init__(self, inner: Guardrail | None = None) -> None:
        """Build the rails runtime and register the scrub action.

        Args:
            inner: The detection stack to wrap, typed as the port rather than
                ClassifierGuardrail so any Guardrail implementation fits --
                which is also what lets tests pass a fake instead of loading
                Presidio, spaCy and a transformer. Defaults to the real one.
        """
        self._inner = inner if inner is not None else ClassifierGuardrail()
        self._rails = LLMRails(RailsConfig.from_path(str(_CONFIG_DIR)))
        # Registered at runtime rather than via an auto-loaded actions.py: that
        # module has no way to reach this instance, and reaching it through a
        # module-level singleton would make the adapter untestable.
        self._rails.register_action(self._scrub_text, name="ScrubAction")

    async def _scrub_text(self, text: str) -> str:
        """The input rail: run detection once and return the redacted text.

        Colang reassigns `$user_message` to this return value, so every later
        stage sees the scrubbed version. Injection is not a separate rail on
        purpose -- ClassifierGuardrail already reports it from the same pass,
        and a second rail would re-run the whole detection stack per request.

        Args:
            text: The raw transcript from the Colang flow.

        Returns:
            The transcript with PII and Article 9 spans replaced by `<TYPE>`
            placeholders, the withheld marker when injection fired, or
            `_RAIL_FAILED` when detection itself broke.
        """
        try:
            result = await self._inner.scrub(text)
        except Exception:
            # Caught rather than propagated: NeMo would swallow it anyway and
            # continue with None. Converting to a sentinel is what preserves
            # the failure for `scrub` to act on.
            logger.exception("guardrail detection failed inside NeMo input rail")
            return _RAIL_FAILED
        return result.clean_text

    async def scrub(self, text: str) -> ScrubResult:
        """Scrub a transcript by running it through the NeMo input rails.

        Args:
            text: The raw candidate transcript.

        Returns:
            A ScrubResult matching ClassifierGuardrail's contract. Flags are
            derived from what the rails did rather than carried out through
            NeMo: an aborted flow means injection, and text that came back
            changed means PII was redacted. Deriving them keeps this stateless,
            which matters because one adapter instance serves concurrent
            requests.

        Raises:
            RuntimeError: If detection failed. Deliberately not a ScrubResult --
                returning one would let an unredacted request continue with no
                flag raised, which is the failure mode this guardrail exists to
                prevent.
        """
        response = await self._rails.generate_async(
            messages=[{"role": "user", "content": text}]
        )
        content = response["content"] if isinstance(response, dict) else str(response)
        content = "" if content is None else str(content)

        # Three ways the rail can fail to produce scrubbed text, none of which
        # a healthy run reaches: our sentinel, the "None" NeMo substitutes when
        # an action returns nothing, and empty output (a real scrub always
        # returns either the transcript or the withheld marker).
        if _RAIL_FAILED in content or content == "None" or not content.strip():
            raise RuntimeError(
                "guardrail rail failed: detection did not complete, refusing to "
                "return a transcript that was never scrubbed"
            )

        if _WITHHELD in content:
            return ScrubResult(
                clean_text=_WITHHELD, pii_redacted=False, injection_detected=True
            )
        return ScrubResult(
            clean_text=content,
            pii_redacted=content != text,
            injection_detected=False,
        )
