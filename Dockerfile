FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY app ./app

RUN useradd -m screening-user
USER screening-user

# Warm the model caches into the image before HF_HUB_OFFLINE is set below;
# otherwise the first real request fails offline. Constructing the guardrail is
# enough: the only things that download are spaCy's en_core_web_sm and the
# injection classifier, and both load in `ClassifierGuardrail.__init__`.
#
# Deliberately NOT a real `.scrub()` any more. Article 9 detection is now an
# HTTP call to the Gemma endpoint (LLMGuardrailRecognizer), which does not exist
# at build time, and that recognizer fails closed on purpose — so a scrub here
# would abort the build. GLiNER, whose lazy loading was the original reason for
# running a scrub, is gone.
#
# SCREENING_SERVICE_API_KEY is a build-only placeholder passed to this one
# command (not an ENV, so it is never baked into the image): guard_classifier
# now imports app.config transitively, and Settings refuses to construct without
# a key.
RUN SCREENING_SERVICE_API_KEY=build-warmup-not-a-real-secret \
    python -c "from app.adapters.guard_classifier import ClassifierGuardrail; ClassifierGuardrail()"
EXPOSE 8000

ENV HF_HUB_OFFLINE=1

CMD ["fastapi", "run"]