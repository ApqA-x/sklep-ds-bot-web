# syntax=docker/dockerfile:1

FROM node:24-alpine AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt
COPY api/ ./api/
COPY --from=ui-build /ui/dist ./ui/dist
EXPOSE 8000
# T12: контейнерный health = readiness (готовность работать), а не liveness:
# healthz всегда 200, пока процесс обслуживает запросы, и не отражал бы отвал
# Mongo. unhealthy сам по себе контейнер не рестартит (см. runbook в wt-dsbot
# docs/runbook-health.md) — это сигнал оператору/монитору.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/readyz', timeout=3)" || exit 1
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
