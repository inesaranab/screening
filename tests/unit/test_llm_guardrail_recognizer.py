import pytest

from app.adapters.llm_guardrail_recognizer import (
    DetectedEntities,
    DetectedEntity,
    LLMGuardrailRecognizer,
)


@pytest.fixture
def recognizer():
    return LLMGuardrailRecognizer()


def _model_returns(monkeypatch, recognizer, entities: list[DetectedEntity]) -> None:
    """Stub the endpoint.

    Patched at the instructor client rather than at the HTTP layer: instructor
    already guarantees a validated DetectedEntities via schema-constrained
    decoding, so faking the JSON round-trip would only re-test instructor.
    """
    monkeypatch.setattr(
        recognizer._client.chat.completions,
        "create",
        lambda **kwargs: DetectedEntities(entities=entities),
    )


def test_maps_quoted_text_to_character_offsets(recognizer, monkeypatch):
    _model_returns(
        monkeypatch, recognizer, [DetectedEntity(entity_type="RELIGION", text="Quaker")]
    )

    results = recognizer.analyze("She said she is a Quaker.", ["RELIGION"], None)

    assert len(results) == 1
    assert results[0].entity_type == "RELIGION"
    assert results[0].start == 18
    assert results[0].end == 24


def test_flags_every_occurrence_not_just_the_first(recognizer, monkeypatch):
    """`str.find` always returns the first match, so a disclosure repeated later
    in the transcript keeps its original offsets and the second mention is left
    unredacted -- Article 9 data reaching the model is exactly what this
    recognizer exists to prevent."""
    _model_returns(
        monkeypatch, recognizer, [DetectedEntity(entity_type="HEALTH", text="diabetes")]
    )

    text = "I have diabetes, and my diabetes is well managed."
    results = recognizer.analyze(text, ["HEALTH"], None)

    spans = sorted((r.start, r.end) for r in results)
    assert spans == [(7, 15), (24, 32)]
    assert all(text[s:e] == "diabetes" for s, e in spans)


def test_drops_entities_the_caller_did_not_request(recognizer, monkeypatch):
    _model_returns(
        monkeypatch, recognizer, [DetectedEntity(entity_type="HEALTH", text="diabetes")]
    )

    assert recognizer.analyze("I have diabetes.", ["RELIGION"], None) == []


def test_drops_quotes_that_are_not_verbatim(recognizer, monkeypatch):
    """The model paraphrased instead of quoting. Without an exact match there is
    no trustworthy span, and redacting a guessed one is worse than missing it --
    the anonymizer would blank the wrong characters."""
    _model_returns(
        monkeypatch,
        recognizer,
        [DetectedEntity(entity_type="RELIGION", text="Quakerism")],
    )

    assert recognizer.analyze("She said she is a Quaker.", ["RELIGION"], None) == []


def test_makes_no_call_when_no_supported_entity_is_requested(recognizer):
    # No stub: a real call would try to reach the endpoint and fail, so passing
    # proves we short-circuit before touching the network.
    assert recognizer.analyze("some text", ["PERSON"], None) == []
