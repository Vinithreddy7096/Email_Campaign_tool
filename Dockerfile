# syntax=docker/dockerfile:1.7
#
# Two images from one file:
#   target dev         local compose (runserver, dev tools; source is bind-mounted)
#   target production  staging/production (gunicorn, runtime deps only, no tests)
#
#   docker build --target production -t email-platform:$(git rev-parse --short HEAD) .

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Both the Django project (app/) and the worker packages (workers/) must be
    # importable: `config.settings` and `workers.tasks` are sibling roots.
    PYTHONPATH=/srv:/srv/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    # Never run as root.
    && useradd --create-home --uid 10001 mailer

WORKDIR /srv


# ─── dev ─────────────────────────────────────────────────────────────────
FROM base AS dev

# Dependency layer — changes rarely, cached across source edits.
COPY pyproject.toml /srv/pyproject.toml
RUN pip install --upgrade pip && pip install ".[dev]"

COPY . /srv
RUN chown -R mailer:mailer /srv
USER mailer
WORKDIR /srv/app

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]


# ─── deps: runtime dependencies in a venv ────────────────────────────────
FROM base AS deps

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml /srv/pyproject.toml
# pyproject declares no packages, so this installs the dependencies only.
RUN pip install --upgrade pip && pip install /srv


# ─── production ──────────────────────────────────────────────────────────
FROM base AS production

ENV PATH=/opt/venv/bin:$PATH \
    DJANGO_SETTINGS_MODULE=config.settings.production

COPY --from=deps /opt/venv /opt/venv
# Only what runs. No tests, docs, infrastructure or dev scripts in the image.
COPY app /srv/app
COPY workers /srv/workers
COPY email_templates /srv/email_templates
COPY deploy/gunicorn.conf.py /srv/gunicorn.conf.py

# Hashed, compressed static files baked into the image (config.settings.build
# needs no secrets and no database).
RUN DJANGO_SETTINGS_MODULE=config.settings.build SECRET_KEY=collectstatic-only-not-a-secret \
        python /srv/app/manage.py collectstatic --noinput \
    && rm -rf /srv/email_templates/tests \
    && chown -R mailer:mailer /srv

USER mailer
WORKDIR /srv/app

ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
LABEL org.opencontainers.image.revision=$GIT_SHA

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:8000/health/live || exit 1

# Web by default; workers and beat override the command (deploy/docker-compose.prod.yml).
# Migrations are a separate one-off step, never run on start-up.
CMD ["gunicorn", "--config", "/srv/gunicorn.conf.py", "config.wsgi:application"]
