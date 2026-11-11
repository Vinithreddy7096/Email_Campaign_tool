# Email Platform

Self-hosted, permission-based outbound email infrastructure targeting 100,000
messages/day.

> **Deliverability is an operational outcome, not a software feature.**
>
> The application's job is to make it *impossible* to send without consent,
> authentication, suppression checks, and rate governance — and to make provider
> responses visible fast enough that a human can react.

**Source of truth:** `Turnkey_High_Volume_Email_Infrastructure_Developer_Specification.docx`
**Build instructions:** `BUILD.md` (§10.5 onward: [docs/BUILD-10.5-onward.md](docs/BUILD-10.5-onward.md))

---

## Current state

**The application is built; it has not yet sent real mail.** Every stage from
contact import to bounce handling exists and is tested, but only against the
local MailHog sink and recorded samples. A first production send is blocked by
decisions and infrastructure, not application code — see
[What is not done](#what-is-not-done). What exists:

| Area | State |
| --- | --- |
| Repository tree (§3) | ✅ complete |
| Env configuration (§4.1) | ✅ complete |
| Boot-time safety guards (§4.2) | ✅ implemented + tested |
| Local dev stack (§5.1) | ✅ compose file with MailHog sink |
| Celery queues + loss prevention (§9.2) | ✅ configured + tested |
| Identity & access (§6.1) — users, roles, API tokens, `/api/v1/whoami` | 🟡 **provisional** — see Q-DATA-1 |
| MFA (TOTP) — enrolment, verification, replay protection, lockout, reset | ✅ enforced for `MFA_REQUIRED_ROLES` and all superusers |
| Domain DNS verification (§5.4) — SPF, DKIM, DMARC, bounce MX, FCrDNS | ✅ `make dns-check`, admin action, every-6h beat task; pauses an `ACTIVE` domain whose DNS breaks |
| Django admin for every model | ✅ append-only tables are view-only; deletes disabled where rows are never deleted |
| Data model (§6) — domains, contacts, consent, suppression, audit | 🟡 **provisional** — built without the §6 field list; see Q-DATA-1 |
| Templates (BUILD §10.5) — versions, approval, safe rendering, preflight | ✅ `email_templates/` integrated: roles → permissions, audit log, admin review page |
| Policy service (BUILD §10.9 checks 1–5) — kill switch, pauses, suppression (address + domain), consent, domain, template governance | ✅ `workers/services/policy.py`; every decision logged append-only; pauses in admin. Checks 6–7 (rate limits, warm-up) wait on Q-POL-1 |
| Campaigns (BUILD §10.6) — model, preflight, approval snapshot, schedule/pause/cancel, API | ✅ `app/campaigns/`, `/api/v1/campaigns…`; admin review page with real-recipient samples. Dispatch (SCHEDULED → SENDING) arrives with the delivery pipeline |
| Delivery pipeline (BUILD §10.7) — expand, render, policy at handoff, MIME, SMTP submission, retries, review queue | ✅ `workers/services/delivery.py` + Celery tasks on the expand/render/deliver queues; every job's history append-only |
| Unsubscribe (BUILD §10.8.4) — signed link, confirm page, RFC 8058 one-click | ✅ `/u/<token>`; built early because rendering needs a working `unsubscribe_url` |
| Operations console — Today, campaigns, templates, approvals, contacts (CSV import, segments), domains, delivery, suppression, controls, audit | ✅ `app/console/`, design from `docs/design/console-mockup.html`. Adding sending domains, single contacts, lists, users, roles and API tokens is done in the Django admin |
| Event pipeline (BUILD §10.8) — Postfix log, DSN/ARF mail, signed provider webhook → delivery events, suppression, per-campaign stats | ✅ `workers/services/events.py`; `manage.py ingest_postfix_log` / `ingest_mail`; `POST /api/v1/events/provider` |
| Transactional API (BUILD §10.6.6) — one message per request, idempotent, through the same policy and delivery pipeline | ✅ `POST /api/v1/messages`, `GET /api/v1/messages/{id}`; `workers/services/transactional.py` |
| Rate limits + warm-up (BUILD §10.9 checks 6–7) — the mechanism; **no numbers are built in** (Q-POL-1) | ✅ `workers/services/ratelimit.py`; configured on the console's Controls page |
| Mail server (BUILD §10.10) — Postfix + OpenDKIM templates, Ansible roles, signed bounce/log forwarders, open-relay test | ✅ `mta/`, `infrastructure/ansible/`, `scripts/relay_test.py`; checked end to end by `make mta-test` |
| Production image, gunicorn, nginx, app-node compose | ✅ `Dockerfile` (target `production`), `deploy/` |
| Monitoring: `/metrics`, alert rules (+ unit tests), Alertmanager, Grafana dashboard | ✅ `app/common/metrics.py`, `monitoring/`; runbooks in `docs/runbooks.md` |
| Backups: nightly dump to S3/MinIO + weekly automated restore test | ✅ `scripts/backup/pgbackup.py`, `deploy/backup/` |
| CI: tests, image smoke test, MTA, monitoring, backup, Ansible | ✅ `.github/workflows/ci.yml` — deployment steps in `docs/DEPLOYMENT.md` |
| Data hosts: Postgres (pgBackRest), Redis, RabbitMQ | ✅ Ansible roles + `playbooks/data.yml` |

### What is not done

Blocking a first real send (details in [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md)):

| Item | State |
| --- | --- |
| Hosting provider and IPs (Q-HOST-1) | ❌ undecided — `infrastructure/terraform/` is placeholders, Ansible inventories hold example addresses |
| Sending, bounce and Message-ID domains (Q-DNS-1) | ❌ not supplied |
| Rate-limit and warm-up numbers (Q-POL-1) | ❌ mechanism built, no numbers. Do not start production traffic without them |
| Secret management, DKIM key custody and rotation (Q-SEC-1, Q-MTA-1) | ❌ undecided; rotation is manual |
| Legal sign-off: retention, consent age, transactional exceptions (Q-LEGAL-1) | ❌ open; no retention or purge job exists |
| End-to-end run against a live Postfix, load test at 100k/day | ❌ `tests/e2e/` is empty |

Not built:

- Ansible roles `app`, `worker`, `monitoring` (empty folders).
- Open and click tracking (`DEFAULT_TRACKING_DOMAIN` is read but unused).
- Soft-bounce auto-suppression (Q-EVT-1) and automatic pause on complaint or
  bounce spikes (Q-EVT-2) — both alert/count only, by decision.
- Adapters for real provider webhooks; the endpoint accepts this project's own payload format.
- An API for contacts, consent capture or templates, and a public signup form —
  consent arrives by CSV import or the admin.
- Password reset and MFA recovery codes (recovery goes through an admin).
- The core data model is still **provisional** (Q-DATA-1): the source
  specification and `BUILD.md` before §10.5 are not in this repository.

## Safety posture

`MOCK_MODE=true` is the default and is never removed. In mock mode all SMTP goes
to a local MailHog sink and nothing reaches the public internet. `GLOBAL_SEND_ENABLED`
defaults to `false` in *every* environment including production — turning it on is
a deliberate, audited human action through the admin UI, not a deploy.

The application refuses to boot if:

- production is configured with `MOCK_MODE=true`, `DEBUG=true`, a weak
  `SECRET_KEY`, a non-HTTPS `BASE_URL`, or missing sending identity / abuse
  contact / webhook secret;
- DKIM private keys are missing or world-readable;
- a non-production environment could send to arbitrary recipients (neither
  `MOCK_MODE` nor a recipient-domain allowlist is set).

## Quick start

```bash
cp .env.example .env     # then fill SECRET_KEY
make up                  # docker compose up -d --build
make migrate
make superuser
```

- **Operations console — http://localhost:8000** (sign in at `/login/`). The main UI, built to
  [docs/design/console-mockup.html](docs/design/console-mockup.html); every page shows live data.
- Django admin — http://localhost:8000/admin/ — low-level edits only, themed to match
- **MailHog — http://localhost:8025** — every "sent" message lands here
- RabbitMQ management — http://localhost:15672 (mailer/mailer)
- MinIO console — http://localhost:9001

`make seed` creates a test sending domain (`mail.example.test`) and 200 contacts
on `example.test` with consent. It is idempotent and refuses to run outside
`local`. Roles come from a migration, not the seed.

Issue an API token (the secret is printed once):

```bash
python app/manage.py issue_api_token you@example.com --name my-client --scope reporting:read
curl -H "Authorization: Bearer ept_…" http://localhost:8000/api/v1/whoami
```

Run `make help` for the full target list.

### Running on Windows / small machines

- **Port 5432 already taken** (a local Postgres): set `POSTGRES_HOST_PORT=5433` in
  `.env`. Containers still use `postgres:5432`.
- **Little memory for Docker** (~4 GB): set `RENDER_CONCURRENCY=2` and
  `DELIVER_CONCURRENCY=4` in `.env`. The §9.2 defaults (8/16) push RabbitMQ into
  its memory alarm.
- **Git Bash** rewrites container paths like `/srv/...` into Windows paths. Prefix
  such commands with `MSYS_NO_PATHCONV=1`, e.g.
  `MSYS_NO_PATHCONV=1 docker compose run --rm -w /srv web pytest -q`.
- `make` may not exist; each target is a plain `docker compose` command (see the
  `Makefile`).
- `GLOBAL_SEND_ENABLED=false` stays the default even locally: the deliver worker
  holds every message until it is switched on. With `MOCK_MODE=true` everything
  then lands in MailHog.

## Tests

```bash
make test         # pytest -x -q
make test-cov     # 80% floor
make lint         # ruff + mypy
```

## Hard prohibitions

Per BUILD.md §0.2, this system must never implement, and contributors must
refuse to add:

- ingestion of purchased, scraped, harvested, or rented recipient lists;
- IP/domain rotation or content mutation intended to evade spam filters,
  blocklists, or provider enforcement;
- an open relay — SMTP submission is restricted to trusted app hosts or
  authenticated scoped clients;
- sending to a suppressed address on a marketing stream, under any override;
- removing, hiding, or breaking the unsubscribe mechanism;
- header spoofing, deceptive `From` identities, or misleading subject generation.

If a request conflicts with the above, stop and escalate rather than implement.
