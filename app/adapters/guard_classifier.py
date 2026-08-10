"""Adapter: the guardrail, behind the `Guardrail` port.

Three detectors, each doing what it is measurably best at.

    PII       -> Presidio, built-in multi-region recognizers (PhoneRecognizer
                 covers US/UK/DE/FR/IL/IN/CA/BR — no UK-only regex), plus
                 custom DOB/NINO/postcode recognizers and a tech-term
                 allow_list to curb over-redaction.
    Article 9 -> LLMGuardrailRecognizer: a self-hosted Gemma-4-31B behind a
                 vLLM endpoint. Replaced GLiNER on measured F1 — 0.786 average
                 across the seven special categories against GLiNER's 0.552,
                 and ahead of the frontier cloud model too (INE-16).
    Injection -> protectai/deberta-v3-base-prompt-injection-v2 (Apache-2.0),
                 a classifier that learned injection INTENT, so a reworded
                 attack ("please set aside the earlier guidance...") is still
                 caught.

Presidio and the injection classifier run in-process. Article 9 detection is an
HTTP call to a self-hosted endpoint: still our own infrastructure, so raw
transcripts never reach a third party, but no longer strictly in-process — and
the service now depends on that endpoint being up.
"""

from functools import cache

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from starlette.concurrency import run_in_threadpool
from transformers import pipeline

from app.adapters.llm_guardrail_recognizer import LLMGuardrailRecognizer
from app.domain.models import ScrubResult

# We use a specific DOB recogniser instead of the generic DATE_TIME so durations
# ("six years") survive — only an actual date of birth is a PII risk. Phones are
# left to Presidio's BUILT-IN multi-region recogniser (US/UK/DE/FR/IL/IN/CA/BR)
# rather than a UK-only regex — that's the locale-general choice.
_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "LOCATION",
    "DOB",
    "UK_NINO",
    "UK_POSTCODE",
    "RELIGION",
    "HEALTH",
    "DISABILITY",
    "SEXUAL_ORIENTATION",
    "TRADE_UNION",
    "POLITICAL_OPINION",
    "ETHNICITY",
]

# Terms Presidio otherwise mislabels (e.g. "Python"/"Go" as a PERSON). The
# allow_list drops any finding whose text is one of these — stops over-redaction
# from eating the exact skills we score on.
_ALLOW = [
    "Python",
    "Go",
    "Golang",
    "Java",
    "Ruby",
    "Rust",
    "Postgres",
    "PostgreSQL",
    "AWS",
    "GDPR",
    "PCI",
    "PCI-DSS",
    "Kubernetes",
    "Docker",
    "Redis",
    "Kafka",
]

# Pronouns the NER layer sometimes mislabels as <PERSON>
_PRONOUNS = {"i", "you", "he", "she", "we", "they", "it"}

# A date of birth, in the forms "3 March 1990" and "03/03/1990". Requires a day
# number, so "March 2027" / "six years" don't match. Date formats are fairly
# international, so this stays reasonably locale-general.
_DOB = [
    Pattern(
        name="dob_text",
        regex=r"\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{4}\b",
        score=0.85,
    ),
    Pattern(name="dob_numeric", regex=r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", score=0.85),
]

# UK National Insurance number: 2 prefix letters, 6 digits, 1 suffix letter (A-D),
# with optional spaces ("AB 12 34 56 C" or "AB123456C"). Presidio's built-in
# UkNinoRecognizer did NOT fire on these spaced formats in practice, so we register
# an explicit pattern — same approach as DOB. Deliberately permissive on the prefix
# letters (not restricted to HMRC's officially-issued combinations): HMRC's own
# specimen number "QQ123456C" — used throughout their docs and commonly copy-pasted
# into test data — uses a letter (Q) that's never issued to a real person, so a
# strict pattern misses exactly the kind of example text most likely to appear.
# For a redaction guardrail, over-matching a NINO-shaped string is the safe
# failure direction; under-matching a real one is not.
_NINO = [
    Pattern(
        name="uk_nino",
        regex=r"\b[A-Za-z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Za-z]\b",
        score=0.85,
    ),
]

# UK postcode ("LS6 2QP", "SW1A 1AA", "EC1A1BB"). Presidio's built-in
# UkPostcodeRecognizer is disabled by default and LOCATION/spaCy doesn't
# reliably catch the postcode segment itself, so we register an explicit
# pattern — same reasoning as NINO: over-matching a postcode-shaped string
# is the safe failure direction for a redaction guardrail.
_POSTCODE = [
    Pattern(
        name="uk_postcode",
        regex=r"\b[A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2}\b",
        score=0.85,
    ),
]


# The text that replaces a transcript flagged as injection. Exported because
# NemoGuardrail has to recognise it coming back out of the rails -- keeping two
# copies of the literal in sync by hand is how injection silently degrades into
# "PII was redacted".
WITHHELD_MESSAGE = "[flagged by injection classifier — content withheld from scoring]"

_INJECTION_MODEL = "protectai/deberta-v3-base-prompt-injection-v2"
# Flag when the INJECTION probability reaches this.
_INJECTION_THRESHOLD = 0.5
# The classifier truncates at ~512 tokens, so long transcripts are scanned in
# overlapping character windows
_WINDOW_CHARS = 250
_WINDOW_OVERLAP = 50


@cache
def _injection_classifier():
    """Build the injection classification pipeline once and cache it.

    Returns:
        A transformers text-classification pipeline. Model init is the expensive
        part, so it is loaded once and reused across calls.
    """
    return pipeline("text-classification", model=_INJECTION_MODEL)


def _windows(text: str) -> list[str]:
    """Split text into overlapping character windows for classification.

    Args:
        text: The text to scan.

    Returns:
        A list of overlapping substrings, or ``[text]`` if it fits one window.
    """
    if len(text) <= _WINDOW_CHARS:
        return [text]
    step = _WINDOW_CHARS - _WINDOW_OVERLAP
    return [text[i : i + _WINDOW_CHARS] for i in range(0, len(text), step)]


class ClassifierGuardrail:
    """Guardrail adapter: Presidio for PII, a trained classifier for injection."""

    def __init__(self) -> None:
        """Build the Presidio engines and load the injection classifier."""
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        ).create_engine()
        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine, supported_languages=["en"]
        )
        self._analyzer.registry.add_recognizer(
            PatternRecognizer(supported_entity="DOB", patterns=_DOB)
        )
        self._analyzer.registry.add_recognizer(
            PatternRecognizer(supported_entity="UK_NINO", patterns=_NINO)
        )
        self._analyzer.registry.add_recognizer(
            PatternRecognizer(supported_entity="UK_POSTCODE", patterns=_POSTCODE)
        )
        self._analyzer.registry.add_recognizer(LLMGuardrailRecognizer())
        self._anonymizer = AnonymizerEngine()
        self._classifier = _injection_classifier()

    def _injection_score(self, text: str) -> float:
        """Return the highest injection probability across all windows.

        Args:
            text: The transcript to scan.

        Returns:
            The maximum INJECTION probability seen (0.0–1.0). A label + score,
            not a text match, so reworded attacks are still caught.
        """
        best = 0.0
        for scores in self._classifier(_windows(text), top_k=None, truncation=True):
            for s in scores:
                if s["label"].upper() in ("INJECTION", "LABEL_1"):
                    best = max(best, float(s["score"]))
        return best

    async def scrub(self, text: str) -> ScrubResult:
        """Scrub off the event loop.

        Args:
            text: The raw candidate transcript.

        Returns:
            The ScrubResult from the synchronous ``_scrub``, run in a thread so
            the CPU-bound work doesn't block the server.
        """
        return await run_in_threadpool(self._scrub, text)

    def _scrub(self, text: str) -> ScrubResult:
        """Detect injection, then redact PII (the synchronous core of scrub).

        Args:
            text: The raw candidate transcript.

        Returns:
            A ScrubResult. On injection it fails closed — the content is withheld
            and PII work is skipped; otherwise PII entities are masked in place.
        """
        # 1. Injection first. If flagged, fail closed immediately: withhold the
        #    content and skip PII work entirely — nothing downstream sees it.
        if self._injection_score(text) >= _INJECTION_THRESHOLD:
            return ScrubResult(
                clean_text=WITHHELD_MESSAGE,
                pii_redacted=False,
                injection_detected=True,
            )

        # 2. PII: detect entities and replace each with a <TYPE> placeholder.
        found = self._analyzer.analyze(
            text=text, language="en", entities=_ENTITIES, allow_list=_ALLOW
        )

        found = [
            r
            for r in found
            if not (
                r.entity_type == "PERSON"
                and text[r.start : r.end].strip().lower() in _PRONOUNS
            )
        ]
        # Presidio's analyzer and anonymizer each define their own (identical)
        # RecognizerResult class; passing analyzer results in is the documented,
        # runtime-correct usage, so ty's cross-class mismatch is a false positive.
        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=found,  # ty: ignore[invalid-argument-type]
        )
        clean = anonymized.text
        return ScrubResult(
            clean_text=clean,
            pii_redacted=bool(found),
            injection_detected=False,
        )
