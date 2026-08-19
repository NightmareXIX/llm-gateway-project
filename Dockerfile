# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------- #
# Stage 1 — build the virtualenv
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency layer: only invalidated when the manifest changes.
COPY pyproject.toml ./
COPY app/__init__.py app/__init__.py
RUN pip install --upgrade pip && pip install .

# --------------------------------------------------------------------------- #
# Stage 2 — runtime
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

# Phase 4 Step 1 (D30): the local perception tier's OCR path shells out to the
# `tesseract` binary — a system dependency, not a wheel. `tesseract-ocr-eng` is
# the English language data; roughly 100MB combined, which is why the image
# roughly doubles in size and Render's free build has to be watched for a
# timeout on the next deploy (Step 12). Detected at startup rather than
# assumed present (`PERCEPTION_LOCAL_OCR_ENABLED`) so a build without this
# layer still boots — but the deployed image always carries it.
RUN apt-get update \
    && apt-get install --no-install-recommends -y tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv/app
# alembic.ini travels with the image because the container runs its own
# migration: Render's free plan has no pre-deploy hook, so `alembic upgrade head`
# happens in the start command (start.sh, invoked by render.yaml's dockerCommand).
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app start.sh ./start.sh
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app config ./config
COPY --chown=app:app app ./app

USER app

EXPOSE 8000

# Shell form, and deliberately so: `$PORT` is the platform's to choose, not the
# image's. Render assigns one (10000 unless overridden) and expects the server to
# bind it; an exec-form CMD would pass the literal string `$PORT` to uvicorn and
# fail on a host that picks anything else. The defaults are what the image does
# when nobody says otherwise — `docker run` and docker-compose both land here.
#
# `WEB_CONCURRENCY` defaults to 2: two processes is right for a box with a whole
# shared core. More would contend; one would mean any CPU-bound stretch — JSON,
# token counting — stalling every other request on this instance, since the whole
# app is async but a process is still one event loop. Render's free instance is
# 0.1 CPU / 512MB and sets this to 1 (see render.yaml), where the arithmetic goes
# the other way: two workers would split a tenth of a core and double both the
# resident set and the Postgres pool (pool_size 5 + max_overflow 5 per process).
#
# `--proxy-headers` with `--forwarded-allow-ips "*"`: Render terminates TLS at its
# edge and forwards to this container. Without these, `request.url.scheme` is
# `http` on every request and the client address in every log line is the proxy
# rather than the caller. The wildcard is safe *here specifically* because nothing
# but that proxy can route to this port — it would be a header-spoofing hole on
# any host where the port is directly reachable.
CMD ["/bin/sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips '*'"]
