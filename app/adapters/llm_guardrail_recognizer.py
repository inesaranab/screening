"""Presidio recognizer backed by a self-hosted LLM (Gemma-4-31B via vLLM).

Replaces GLiNERRecognizer for GDPR Article 9 special categories, chosen on
measured F1: Gemma-4 averaged 0.786 across the seven categories against
GLiNER's 0.552, and beat the frontier cloud model too (see INE-16).
"""

import logging
import re

import instructor
from openai import OpenAI
from presidio_analyzer import EntityRecognizer, RecognizerResult
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger("screen")

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


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _span_for_match(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Turn a raw substring hit into a span that does not cut a word in half.

    Substring matching is blind to word boundaries, and the two ways it can
    straddle one need opposite treatment:

    * The hit STARTS mid-word -- "he" inside "the"/"When", "MS" inside "CMS".
      There is no reading of that occurrence under which the candidate
      disclosed anything, so it is dropped. Growing it leftwards instead (the
      previous behaviour) redacted whole innocent words: a quote of "he"
      turned "When the interviewer" into "<SEXUAL_ORIENTATION> interviewer".
    * The hit ENDS mid-word -- "Muslim" against a transcript saying "Muslims".
      That is the same disclosure inflected, so the span is grown rightwards.
      Stopping at the raw end would leave "<RELIGION>s" behind.

    Args:
        text: The transcript the offsets refer to.
        start: Start offset of the raw substring match.
        end: End offset (exclusive) of the raw substring match.

    Returns:
        The span to redact, or None when the hit began mid-word and should be
        discarded.
    """
    if start > 0 and _is_word_char(text[start]) and _is_word_char(text[start - 1]):
        return None
    while end < len(text) and _is_word_char(text[end - 1]) and _is_word_char(text[end]):
        end += 1
    return start, end


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
                timeout=settings.llm_guardrail_timeout_s,
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
        seen: set[tuple[str, int, int]] = set()
        for item in detected.entities:
            entity_type = item.entity_type.strip().upper()
            if entity_type not in requested:
                continue
            # A quote with no alphanumeric character in it -- "", " ", "." --
            # occurs almost everywhere in the transcript. Redacting every hit
            # replaces the separators between words and destroys the text the
            # model is scored on ("I<HEALTH>manage<HEALTH>my..."), so such a
            # quote is never actionable.
            if not any(char.isalnum() for char in item.text):
                continue
            # Offsets are computed here, never asked of the model -- LLMs are
            # unreliable at character arithmetic. No match means no trustworthy
            # span, so the finding is dropped rather than guessed.
            #
            # Case-insensitive on purpose: a model that returns "Diabetes"
            # against a transcript saying "diabetes" is pointing at a real
            # disclosure, and the offsets it produces are still exact. Matching
            # case-sensitively dropped it silently -- under-redaction of an
            # Article 9 category is the one failure this recognizer exists to
            # prevent.
            #
            # Every occurrence, not just the first: the same disclosure often
            # appears more than once ("I have diabetes ... my diabetes"), and
            # redacting only the first mention leaks the rest to the model.
            # `seen` absorbs the duplicates a model asked for "every occurrence"
            # tends to return.
            matched = False
            for match in re.finditer(re.escape(item.text), text, re.IGNORECASE):
                span = _span_for_match(text, match.start(), match.end())
                if span is None:
                    continue
                matched = True
                key = (entity_type, span[0], span[1])
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    RecognizerResult(
                        entity_type=entity_type,
                        start=span[0],
                        end=span[1],
                        score=_SCORE,
                    )
                )
            if not matched:
                # Logged, not silent: a dropped quote is an Article 9
                # disclosure the model saw and we did not redact. The quote
                # itself is special-category data, so only its length is
                # recorded.
                logger.warning(
                    "article9_quote_unmatched",
                    extra={
                        "context": {
                            "entity_type": entity_type,
                            "quote_length": len(item.text),
                        }
                    },
                )
        return results
