# syntax=docker/dockerfile:1
# Multi-stage build: `builder` resolves and installs dependencies into a
# venv; the final stage copies only that venv + app source, dropping pip's
# build cache and any compiler toolchain pulled in transitively. Pin the
# base tag (not floating `slim`) so a CI-triggered build is reproducible.
FROM python:3.12.8-slim AS builder

WORKDIR /srv

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY pyproject.toml ./
# Dummy package so dependency resolution/install can happen in its own
# cached layer before the real source is copied in — editing app code
# doesn't invalidate this layer.
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

COPY app ./app
COPY ui ./ui
# Re-run so the real package (not the dummy) is what's actually installed.
RUN pip install --no-cache-dir --no-deps .

FROM python:3.12.8-slim

RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app

WORKDIR /srv

COPY --from=builder /venv /venv
COPY --from=builder /srv/app ./app
COPY --from=builder /srv/ui ./ui
ENV PATH="/venv/bin:$PATH"

RUN chown -R app:app /srv
USER app

# Railway (and most PaaS targets) inject $PORT at runtime and route traffic
# to it; docker-compose.yml's local dev setup doesn't set $PORT, so this
# falls back to 8000 to keep `docker compose up` unchanged.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/health', timeout=3)" || exit 1

# Shell form (not exec form) so ${PORT:-8000} actually expands. The worker
# service (Railway + docker-compose.yml) overrides this CMD entirely with
# `arq app.worker.settings.WorkerSettings` — same image, different role.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
