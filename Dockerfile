# syntax=docker/dockerfile:1

FROM node:24-alpine AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

# T15/п.1-2: зависимости строго из api/requirements.lock (uv pip compile,
# --require-hashes сверяет sha256 каждого колеса на обеих целевых платформах
# образа, linux/amd64 и linux/arm64). Диапазоны из requirements.txt в сборке
# образа больше не резолвятся.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_PREFERENCE=only-system
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

WORKDIR /srv
COPY api/requirements.lock ./api/requirements.lock
RUN uv venv /srv/.venv \
 && uv pip install --python /srv/.venv/bin/python --no-cache --require-hashes -r api/requirements.lock
ENV VIRTUAL_ENV=/srv/.venv \
    PATH="/srv/.venv/bin:${PATH}"

COPY api/ ./api/
COPY --from=ui-build /ui/dist ./ui/dist

# T15/п.9: не root по умолчанию (compose задаёт user: и так, но ручной
# docker run и CI-smoke получают безопасный дефолт; uid 10001 = DSBOT_UID).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin dsbot \
 && chown -R dsbot:dsbot /srv
USER 10001:10001

EXPOSE 8000
# T12: контейнерный health = readiness (готовность работать), а не liveness:
# healthz всегда 200, пока процесс обслуживает запросы, и не отражал бы отвал
# Mongo. unhealthy сам по себе контейнер не рестартит (см. runbook в wt-dsbot
# docs/runbook-health.md) — это сигнал оператору/монитору.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/readyz', timeout=3)" || exit 1
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
