"""Presidio recognizer backed by a self-hosted LLM (Gemma-4-31B via vLLM).

Replaces GLiNERRecognizer for GDPR Article 9 special categories, chosen on
measured F1: Gemma-4 averaged 0.786 across the seven categories against
GLiNER's 0.552, and beat the frontier cloud model too (see INE-16).
"""

import instructor
from openai import OpenAI
from presidio_analyzer import EntityRecognizer, RecognizerResult
from pydantic import BaseModel

from app.config import settings

_ARTICLE9_ENTITIES = [
    "RELIGION",
    "HEALTH",
    "DISABILITY",
    "SEXUAL_ORIENTATION",
    "TRADE_UNION",
    "POLITICAL_OPINION",
    "ETHNICITY",
]

# Presidio expects a confidence per finding. The LLM does not return one, so we
# use a fixed value above Presidio's default 0.35 threshold -- a detection that
# survived constrained decoding and a verbatim-match check has already cleared
# two filters.
_SCORE = 0.85


class DetectedEntity(BaseModel):
    """One special-category disclosure, quoted verbatim from the transcript."""

    entity_type: str
    text: str


class DetectedEntities(BaseModel):
    """Wrapper: schema-constrained output must be an object, not a bare array."""

    entities: list[DetectedEntity]


class LLMGuardrailRecognizer(EntityRecognizer):
    """Detects Article 9 special categories via a self-hosted vLLM endpoint."""

    def __init__(self) -> None:
        super().__init__(
            supported_entities=_ARTICLE9_ENTITIES,
            name="LLMGuardrailRecognizer",
            supported_language="en",
        )
        self._client = instructor.from_openai(
            OpenAI(
                base_url=settings.llm_guardrail_base_url,
                api_key="not-used-by-vllm",  # stub: the SDK requires one, vLLM ignores it
                timeout=settings.llm_timeout_s,
            ),
            mode=instructor.Mode.JSON_SCHEMA,
        )

    def load(self) -> None:
        """Nothing to preload -- the model lives behind the endpoint."""

    def analyze(self, text, entities, nlp_artifacts=None):
        """Detect Article 9 special categories in `text`.

        Args:
            text: The transcript to scan.
            entities: Entity types the caller wants. Anything outside this
                recognizer's Article 9 set is ignored.
            nlp_artifacts: spaCy output supplied by Presidio. Unused -- this
                recognizer sends raw text to the LLM rather than reusing the
                pipeline's tokens.

        Returns:
            One RecognizerResult per detection, each carrying the entity type
            and the character offsets of the matching substring, e.g.
            ``[RecognizerResult("RELIGION", 18, 24, 0.85)]``. Empty when
            nothing is found or no Article 9 entity was requested.

        Raises:
            Exception: If the endpoint is unreachable. Deliberate -- returning
                [] would make "clean transcript" and "detector offline"
                indistinguishable, silently disabling Article 9 redaction.
        """
        requested = [e for e in entities if e in _ARTICLE9_ENTITIES]
        if not requested:
            return []

        detected = self._client.chat.completions.create(
            model=settings.llm_guardrail_model,
            response_model=DetectedEntities,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Find every occurrence of these entity types in the "
                        f"transcript: {', '.join(requested)}.\n\n"
                        "For each one found, quote the exact matching substring "
                        "verbatim from the transcript (character-for-character, "
                        "do not paraphrase or normalize it) and label its entity "
                        f"type.\n\nTranscript:\n{text}"
                    ),
                }
            ],
        )

        results = []
        for item in detected.entities:
            if item.entity_type not in requested:
                continue
            # Offsets are computed here, never asked of the model -- LLMs are
            # unreliable at character arithmetic. No exact match means no
            # trustworthy span, so the finding is dropped rather than guessed.
            start = text.find(item.text)
            if start == -1:
                continue
            results.append(
                RecognizerResult(
                    entity_type=item.entity_type,
                    start=start,
                    end=start + len(item.text),
                    score=_SCORE,
                )
            )
        return results
