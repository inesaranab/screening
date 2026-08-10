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

import base64
import binascii
import logging
import pathlib

from nemoguardrails import LLMRails, RailsConfig

from app.adapters.guard_classifier import WITHHELD_MESSAGE, ClassifierGuardrail
from app.domain.models import ScrubResult
from app.ports.guardrail import Guardrail

logger = logging.getLogger("screen")

_CONFIG_DIR = pathlib.Path(__file__).parent / "guardrails_config"

# Imported, never re-spelled: injection is signalled to `scrub` only by this
# exact string coming back out of the rails, so a second copy drifting out of
# sync would silently downgrade an injection to "PII was redacted".
_WITHHELD = WITHHELD_MESSAGE

# NeMo catches exceptions raised inside an action, logs them, and lets the flow
# continue with the action's result as None -- which Colang then stringifies to
# "None". Without an explicit signal, a dead detector is indistinguishable from
# a successful scrub of a transcript that happens to read "None". The action
# catches its own failures and returns this sentinel so `scrub` can fail closed.
# Plain ASCII on purpose: Colang interpolates the value into an expression it
# then evaluates, and control characters (a null byte, originally) raise
# ColangValueError there -- the sentinel would never reach `scrub` at all.
_RAIL_FAILED = "__GUARDRAIL_RAIL_FAILED__"

# Marks a payload as having been produced by ScrubAction. Without it, "the rail
# ran" is assumed rather than observed: if the input rail is ever not applied --
# rails.co missing from the image, a Colang version that renames the `input
# rails` flow -- `bot say` echoes the *input* message back, which is valid
# base64 that decodes cleanly to the untouched transcript. `scrub` would hand
# that to the model as scrubbed text with no flag raised, the exact fail-open
# every other check here exists to prevent. ':' is outside base64's alphabet,
# so it can never collide with the payload.
_SCRUBBED = "scrubbed:"


def _encode(text: str) -> str:
    """Wrap a transcript in base64 for the trip through Colang.

    Colang does not treat a message as opaque data: the runtime interpolates it
    into expressions it then evaluates. Measured against nemoguardrails 0.23.0:

        "I earn $rate"      -> "I earn var_rate"   (the model is then scored on
                               text the candidate never said, and the difference
                               also raises a false `pii_redacted`)
        "C:\\builds\\app"   -> "C:uildspp"         (`\\b` and `\\a` read as escapes)

    base64's alphabet ([A-Za-z0-9+/=]) contains none of the characters Colang
    reacts to, so encoding in and decoding out makes the round trip lossless.
    """
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _decode(payload: str) -> str:
    """Reverse `_encode`. Raises ValueError on anything that is not our payload."""
    try:
        return base64.b64decode(payload.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise ValueError("rail output was not a valid transcript payload") from exc


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
            text: The base64-wrapped transcript from the Colang flow (see
                `_encode` for why it is not the raw string).

        Returns:
            The base64-wrapped transcript with PII and Article 9 spans replaced
            by `<TYPE>` placeholders, the wrapped withheld marker when injection
            fired, or `_RAIL_FAILED` when detection itself broke.
        """
        try:
            result = await self._inner.scrub(_decode(text))
        except Exception:
            # Caught rather than propagated: NeMo would swallow it anyway and
            # continue with None. Converting to a sentinel is what preserves
            # the failure for `scrub` to act on.
            logger.exception("guardrail detection failed inside NeMo input rail")
            return _RAIL_FAILED
        # Prefixed so `scrub` can tell "the action ran" from "Colang echoed the
        # input back". The input is also valid base64, so decoding alone proves
        # nothing -- see the note on _SCRUBBED.
        return _SCRUBBED + _encode(result.clean_text)

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
            messages=[{"role": "user", "content": _encode(text)}]
        )
        # `.get`, not `["content"]`: a dict-shaped response without that key
        # would raise KeyError straight past every fail-closed check below and
        # surface as an unclassified 500. Missing content is just another way
        # the rail failed to produce scrubbed text.
        content = (
            response.get("content") if isinstance(response, dict) else str(response)
        )
        content = "" if content is None else str(content)
        content = content.strip()

        # Four ways the rail can fail to produce scrubbed text, none of which a
        # healthy run reaches: our sentinel, the "None" NeMo substitutes when an
        # action returns nothing, empty output, and output missing the marker
        # only ScrubAction adds -- which covers Colang echoing the input back
        # (valid base64, decodes to the raw transcript) just as well as an
        # outright error.
        if (
            _RAIL_FAILED in content
            or content == "None"
            or not content
            or not content.startswith(_SCRUBBED)
        ):
            raise RuntimeError(
                "guardrail rail failed: detection did not complete, refusing to "
                "return a transcript that was never scrubbed"
            )
        try:
            clean = _decode(content.removeprefix(_SCRUBBED))
        except ValueError as exc:
            raise RuntimeError(
                "guardrail rail failed: detection did not complete, refusing to "
                "return a transcript that was never scrubbed"
            ) from exc

        if clean == _WITHHELD:
            return ScrubResult(
                clean_text=_WITHHELD, pii_redacted=False, injection_detected=True
            )
        return ScrubResult(
            clean_text=clean,
            pii_redacted=clean != text,
            injection_detected=False,
        )
