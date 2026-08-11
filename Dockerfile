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

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv/app
# alembic.ini travels with the image: migrations run as a release command, not at
# app start, so the container must be able to run `alembic upgrade head` itself.
COPY --chown=app:app alembic.ini ./alembic.ini
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app config ./config
COPY --chown=app:app app ./app

USER app

EXPOSE 8000

# `--workers 2`: two processes on a shared-cpu-1x, which is what the free tier
# gives. More would contend; one would mean a single slow provider call blocking
# every other request on this machine, since the whole app is async but the
# process is still one event loop.
#
# `--proxy-headers` with `--forwarded-allow-ips "*"`: Fly terminates TLS at its
# edge and forwards over the private network. Without these, `request.url.scheme`
# is `http` on every request and the client address in every log line is Fly's
# proxy rather than the caller. The wildcard is safe *here specifically* because
# nothing but that proxy can route to this port — it would be a header-spoofing
# hole on any host where the port is directly reachable.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
