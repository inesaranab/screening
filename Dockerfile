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

RUN python -c "from app.adapters.guard_classifier import ClassifierGuardrail; ClassifierGuardrail()"
EXPOSE 8000

ENV HF_HUB_OFFLINE=1

CMD ["fastapi", "run"]