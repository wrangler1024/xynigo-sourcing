FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN addgroup --system xynigo && adduser --system --ingroup xynigo xynigo

COPY requirements.lock alembic.ini ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY migrations ./migrations
COPY src ./src

USER xynigo
EXPOSE 8080

CMD ["uvicorn", "xynigo_auth.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
