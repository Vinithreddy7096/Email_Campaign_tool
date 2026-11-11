.DEFAULT_GOAL := help
DC := docker compose
MANAGE := python app/manage.py

.PHONY: help up down logs migrate makemigrations seed superuser shell test test-cov \
        lint fmt dns-check relay-test purge-queues image mta-render mta-test \
        monitoring-check backup-test

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up:  ## docker compose up -d --build
	$(DC) up -d --build

down:  ## Stop the stack. Safe locally; NEVER run against a prod MTA with a queue.
	$(DC) down

logs:  ## Tail the noisy services
	$(DC) logs -f web worker-deliver

migrate:  ## Apply migrations
	$(DC) run --rm web python manage.py migrate

makemigrations:  ## Generate migrations
	$(DC) run --rm web python manage.py makemigrations

seed:  ## Roles, test domain, 200 fake contacts
	$(DC) run --rm web python /srv/scripts/seed_dev.py

superuser:  ## Create a Django superuser
	$(DC) run --rm -it web python manage.py createsuperuser

shell:  ## shell_plus
	$(DC) run --rm -it web python manage.py shell_plus

test:  ## Fast test run
	pytest -x -q

test-cov:  ## Coverage gate
	pytest --cov=app --cov=workers --cov=email_templates --cov-report=term-missing --cov-fail-under=80

lint:  ## Static checks
	ruff check . mta/bin/forward-bounce mta/bin/ship-postfix-log && ruff format --check . && mypy app workers tests

fmt:  ## Autoformat + autofix
	ruff format . && ruff check --fix .

dns-check:  ## SPF/DKIM/DMARC/PTR check. usage: make dns-check DOMAIN=mail.example.com
	python scripts/validate_dns.py --domain $(DOMAIN)

relay-test:  ## Assert the MTA is not an open relay. usage: make relay-test HOST=1.2.3.4
	python scripts/relay_test.py --host $(HOST)

purge-queues:  ## Drop every queued Celery task. Local only.
	$(DC) run --rm web celery -A config purge -f

# ─── Production pieces (same checks as CI) ───────────────────────────────

image:  ## Build the production image and the backup image
	docker build --target production -t email-platform:local .
	docker build -f deploy/backup/Dockerfile -t email-platform-backup:local .

mta-render:  ## Render the MTA templates with the test vars into build/mta
	python scripts/render_mta.py --vars mta/test/vars.yml --out build/mta

mta-test:  ## Boot a throwaway Postfix+OpenDKIM: postfix check, relay test, DKIM, bounce pipe
	PYTHON=$${PYTHON:-python3} sh mta/test/run.sh

monitoring-check:  ## promtool + amtool on monitoring/
	docker run --rm --entrypoint sh -v "$(CURDIR)/monitoring:/m:ro" prom/prometheus:v2.54.1 -c 'cd /m/prometheus/tests && promtool test rules mailer.rules.test.yml && promtool check rules /m/prometheus/rules/*.yml'

backup-test:  ## Back up the local compose database to a volume and restore-check it
	docker build -q -f deploy/backup/Dockerfile -t email-platform-backup:local .
	docker run --rm --network email_app_default -v pgbackup-local:/backups -e BACKUP_DIR=/backups -e BACKUP_ENV=local 	  -e DATABASE_URL=postgres://mailer:mailer@postgres:5432/mailer --user root email-platform-backup:local backup
	docker run --rm --network email_app_default -v pgbackup-local:/backups -e BACKUP_DIR=/backups -e BACKUP_ENV=local 	  -e DATABASE_URL=postgres://mailer:mailer@postgres:5432/postgres --user root email-platform-backup:local restore-check
