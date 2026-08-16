#!/bin/sh
# The container's start command on Render: migrate, then serve.
#
# This is a file rather than a one-liner in render.yaml because Render does not
# run its Docker Command through a shell. It interpolates environment variables
# into the string and then splits the result into argv and execs it directly,
# which leaves no way to express a chained command:
#
#   alembic upgrade head && exec uvicorn ...   -> alembic: error: unrecognized
#                                                 arguments: && exec uvicorn ...
#   /bin/sh -c "alembic upgrade head && ..."   -> /bin/sh: 1: alembic upgrade
#                                                 head && ...: not found
#
# The second failed because the quote characters survive tokenization as literal
# text instead of being consumed as syntax, so the inner shell received one
# quoted word and looked for an executable by that name. Both were real deploys.
#
# `dockerCommand: sh /srv/app/start.sh` is two bare tokens, so it survives that
# tokenizer unchanged — and invoking it through `sh` rather than relying on this
# shebang means the file does not need its executable bit to survive a checkout
# on Windows (see .gitattributes for the related line-ending trap).
#
# The image's own CMD stays serve-only. `make docker-run` and docker-compose
# should not migrate a database as a side effect of starting a container; only
# the deployed host does that, and only because Render's free plan has no
# pre-deploy hook to put it in (ADR-017).
set -e

# Before uvicorn binds, deliberately. alembic/env.py resolves DATABASE_URL
# through app/config.py, which validates the whole settings object — so a bad
# migration or a missing variable exits non-zero here, the health check never
# passes, Render cancels the deploy, and the previous instance keeps serving.
alembic upgrade head

# `exec` so uvicorn replaces this shell as PID 1 and receives Render's SIGTERM
# directly at spin-down; without it the signal stops at a shell that ignores it
# and the container is killed after the grace period instead of shutting down.
#
# --workers and --forwarded-allow-ips are absent on purpose: uvicorn reads
# $WEB_CONCURRENCY and $FORWARDED_ALLOW_IPS when the flags are not given, and
# render.yaml sets both. That keeps the `*` out of any command line.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers
