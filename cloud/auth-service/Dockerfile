FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system xynigo && adduser --system --ingroup xynigo xynigo

COPY pyproject.toml uv.lock README.md alembic.ini ./
RUN pip install --no-cache-dir uv==0.11.7 \
    && uv sync --frozen --no-dev --no-install-project

COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}"

USER xynigo
EXPOSE 8080

CMD ["uvicorn", "xynigo_auth.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
