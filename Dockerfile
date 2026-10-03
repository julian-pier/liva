FROM node:24-slim AS frontend

WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run check

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system liva && useradd --system --gid liva --home-dir /app liva
WORKDIR /app

COPY . .
COPY --from=frontend /src/static/dist/liva-ui/ /app/static/dist/liva-ui/
RUN python -m pip install --no-cache-dir . \
    && mkdir -p database data logs uploads var \
    && mkdir -p /opt /var/lib/liva/database \
    && ln -s /app /opt/liva \
    && chown -R liva:liva /app /var/lib/liva

USER liva
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)"

CMD ["gunicorn", "--bind=0.0.0.0:5000", "--workers=1", "--threads=4", "--timeout=120", "--access-logfile=-", "--error-logfile=-", "app:app"]
