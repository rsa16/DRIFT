FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml LICENSE ./
COPY drift ./drift
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 --home-dir /app drift \
    && mkdir -p /data/snapshots /data/cache \
    && chown -R drift:drift /data

USER drift

EXPOSE 8000

CMD ["uvicorn", "drift.api:app", "--host", "0.0.0.0", "--port", "8000"]
