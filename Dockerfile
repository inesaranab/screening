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

# Presidio recognizers (incl. GLiNER) lazy-load on first `.analyze()` call, not
# at construction, so building the guardrail alone doesn't fetch GLiNER's
# weights. Run an actual scrub so every model — spaCy, GLiNER, the injection
# classifier — is downloaded and cached into the image before HF_HUB_OFFLINE
# is set below; otherwise the first real request fails offline.
RUN python -c "import asyncio; from app.adapters.guard_classifier import ClassifierGuardrail; asyncio.run(ClassifierGuardrail().scrub('warmup'))"
EXPOSE 8000

ENV HF_HUB_OFFLINE=1

CMD ["fastapi", "run"]