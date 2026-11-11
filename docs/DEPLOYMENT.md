# Deploying the email platform

How the production pieces fit together and the order to bring them up. The
decisions that must be made first (hosting and IPs, rate limits, legal
sign-off) are listed in `docs/OPEN_QUESTIONS.md`; this document assumes them.

## Layout

| Host | Runs | Built from |
|---|---|---|
| App node(s) | nginx → gunicorn web, Celery workers, beat (one node only) | `Dockerfile` target `production`, `deploy/docker-compose.prod.yml`, `deploy/nginx.conf` |
| MTA host(s) | Postfix, OpenDKIM, `forward-bounce`, `ship-postfix-log` — **not** containerised (spec §13) | `mta/` templates via `infrastructure/ansible/playbooks/mta.yml` |
| Data | Postgres 16, RabbitMQ, Redis | your provider, or `infrastructure/ansible/playbooks/data.yml` |
| Monitoring | Prometheus, Alertmanager, Grafana, exporters | `monitoring/` |
| Backups | nightly dump + weekly restore test, on one app node | `deploy/backup/` |

Mail flow: app → SMTP (app network only) → Postfix → stream transport (bound
to that stream's IP) → recipient. Bounces and complaints → Postfix (bounce
domain MX) → `forward-bounce` → signed `POST /api/v1/events/mail`. Delivery
status lines → `ship-postfix-log` → signed `POST /api/v1/events/postfix-log`.

## 1. Secrets

Never in Git. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`.

| Secret | Where it lives |
|---|---|
| `SECRET_KEY`, `FIELD_ENCRYPTION_KEYS`, `WEBHOOK_SIGNING_SECRET`, `MTA_INGEST_SECRET`, `METRICS_TOKEN`, database/broker/Redis URLs | `/etc/email-platform/app.env` on app nodes (root, 0600) |
| `MTA_INGEST_SECRET` (same value), DKIM private keys | Ansible vault: `inventories/<env>/group_vars/mta/vault.yml` (`vault_mta_ingest_secret`, `vault_dkim_private_keys`) |
| `METRICS_TOKEN` (same value) | Prometheus: `/etc/prometheus/secrets/metrics_token` |
| Bucket credentials, DB URLs for backups | `/etc/email-platform/backup.env` (root, 0600) — template `deploy/backup/backup.env.example` |
| SMTP password / chat webhooks for alerts | Alertmanager: `/etc/alertmanager/secrets/*` |

The app refuses to boot in production without the required secrets, with
MFA off, with `MOCK_MODE` on, or with an `http://` `BASE_URL`.

## 2. App nodes

```sh
docker build --target production --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
  -t registry.example.com/email-platform:$(git rev-parse --short HEAD) .
# push, then on each app node:
export IMAGE=registry.example.com/email-platform:<sha>
docker compose -f deploy/docker-compose.prod.yml run --rm migrate   # once per release
docker compose -f deploy/docker-compose.prod.yml up -d
```

- Set `BEAT_REPLICAS=0` on every app node but one.
- Install `deploy/nginx.conf` as the TLS front end; set the operator networks
  and certificate paths. The console, admin and API are operator-only; `/u/`
  (unsubscribe) and the signed webhook are public; MTA intake, `/health/*` and
  `/metrics` are private-network only.
- `GLOBAL_SEND_ENABLED` stays `false` until the steps below are done.

## 3. MTA hosts

1. DNS for each stream IP: PTR = its `helo_name`, and the A record back (FCrDNS).
2. For each sending domain: SPF, DKIM (public key), DMARC; MX for each bounce domain → the MTA.
3. Fill `inventories/<env>/group_vars/mta.yml` (networks, stream IPs) and the vault.
4. From a machine **outside** the app networks:
   ```sh
   cd infrastructure/ansible
   ansible-playbook -i inventories/production playbooks/mta.yml --ask-vault-pass --check --diff
   ansible-playbook -i inventories/production playbooks/mta.yml --ask-vault-pass
   ```
   The play runs `postfix check`, `opendkim -n`, `opendkim-testkey` against DNS,
   and finishes with the open-relay test (`scripts/relay_test.py`). Any failure
   stops it.
5. Set `MTA_SMTP_HOST` in `app.env` to the MTA's app-network address.

Staging must run with `postfix_sink_mode: true` (the role refuses otherwise):
every message is discarded at the MTA instead of reaching a recipient.

## 4. Monitoring

- Prometheus with `monitoring/prometheus/prometheus.yml` and `rules/`; write
  target files in `/etc/prometheus/targets/` (`mailer.json`, `rabbitmq.json`,
  `postgres.json`, `redis.json`, `postfix.json`, `node.json`,
  `alertmanager.json` — same shape as `monitoring/prometheus/targets.local/`).
- Exporters: node_exporter (every host, with the textfile collector at
  `/var/lib/node_exporter/textfile`), postgres-exporter, redis_exporter,
  postfix_exporter on MTA hosts. RabbitMQ's built-in plugin serves `:15692`.
- Alertmanager with `monitoring/alertmanager/alertmanager.yml` (replace the
  example addresses). Grafana with `monitoring/grafana/provisioning` and `dashboards`.
- Every alert links its section in `docs/runbooks.md`.

## 5. Backups

On one app node:

```sh
docker build -f deploy/backup/Dockerfile -t email-platform-backup:latest .
cp deploy/backup/*.service deploy/backup/*.timer /etc/systemd/system/
systemctl enable --now email-platform-backup.timer email-platform-restore-check.timer
systemctl start email-platform-backup        # first run now; check its output
```

Turn on bucket versioning and object lock so a compromised host cannot delete
history. The weekly restore test needs a separate Postgres server
(`RESTORE_DATABASE_URL`); it never touches the production database.

## 6. Going live

1. Staging end to end with the sink, including a load test at the expected volume.
2. Rate limits and warm-up plans entered on the console (Q-POL-1).
3. Feedback loops and postmaster tools registered for each sending domain.
4. Set `GLOBAL_SEND_ENABLED=true` in `app.env`, `docker compose up -d`. Start
   with a small, recent, consented audience and watch the dashboard.

## CI

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Proves |
|---|---|
| test | lint, types, migrations complete, the full test suite |
| image | the production image builds, carries no secrets or tests, boots under production settings, passes `check --deploy`, answers probes by IP and redirects plain HTTP |
| mta | rendered config passes `postfix check` / `opendkim -n`; outside clients cannot relay (incl. percent hack, bang path, source route); mail is DKIM-signed and leaves on its stream's transport; a bounce reaches the intake signed |
| monitoring | Prometheus config and alert rules are valid and the rule unit tests pass; Alertmanager config is valid |
| backup | a real database is dumped, restored into a scratch database and verified |
| ansible | the MTA playbook parses with both inventories |

Run the same locally: `make lint test image mta-test monitoring-check backup-test`.
