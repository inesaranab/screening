import pytest

from app.adapters.guard_nemo import NemoGuardrail
from app.domain.models import ScrubResult


class _FakeInner:
    """Stands in for ClassifierGuardrail so the test needs no Presidio, spaCy,
    transformer weights, or vLLM endpoint."""

    def __init__(self, result: ScrubResult):
        self._result = result
        self.calls: list[str] = []

    async def scrub(self, text: str) -> ScrubResult:
        self.calls.append(text)
        return self._result


@pytest.mark.asyncio
async def test_redacted_text_from_the_rail_reaches_the_caller():
    inner = _FakeInner(
        ScrubResult(
            clean_text="I am a <RELIGION>.", pii_redacted=True, injection_detected=False
        )
    )
    guardrail = NemoGuardrail(inner=inner)

    result = await guardrail.scrub("I am a Quaker.")

    assert inner.calls == ["I am a Quaker."]
    assert result.clean_text == "I am a <RELIGION>."
    assert result.pii_redacted is True
    assert result.injection_detected is False


class _BrokenInner:
    """Detection backend is unreachable -- e.g. the vLLM endpoint is down."""

    async def scrub(self, text: str) -> ScrubResult:
        raise ConnectionError("endpoint down")


@pytest.mark.asyncio
async def test_rail_failure_raises_instead_of_returning_a_bogus_scrub():
    """NeMo swallows action exceptions and hands back the string "None". Left
    unchecked that reads as a successful scrub of the text "None", so a dead
    detector would silently pass an unredacted-but-empty transcript downstream
    with no flag set. The adapter must fail closed instead."""
    guardrail = NemoGuardrail(inner=_BrokenInner())

    with pytest.raises(RuntimeError, match="guardrail rail failed"):
        await guardrail.scrub("I am a Quaker.")
