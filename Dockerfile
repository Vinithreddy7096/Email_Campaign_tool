FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Both the Django project (app/) and the worker packages (workers/) must be
    # importable: `config.settings` and `workers.tasks` are sibling roots.
    PYTHONPATH=/srv:/srv/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Dependency layer — changes rarely, cached across source edits.
COPY pyproject.toml /srv/pyproject.toml
RUN pip install --upgrade pip && pip install ".[dev]"

COPY . /srv

# Never run as root.
RUN useradd --create-home --uid 10001 mailer && chown -R mailer:mailer /srv
USER mailer

WORKDIR /srv/app

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
