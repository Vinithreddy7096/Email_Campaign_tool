# Email Platform

Self-hosted, permission-based outbound email infrastructure targeting 100,000
messages/day.

> **Deliverability is an operational outcome, not a software feature.**
>
> The application's job is to make it *impossible* to send without consent,
> authentication, suppression checks, and rate governance — and to make provider
> responses visible fast enough that a human can react.

**Source of truth:** `Turnkey_High_Volume_Email_Infrastructure_Developer_Specification.docx`
**Build instructions:** `BUILD.md`

---

## Current state

**Repository foundation only.** No message can be sent, because nothing that
sends messages has been built yet. What exists:

| Area | State |
| --- | --- |
| Repository tree (§3) | ✅ complete |
| Env configuration (§4.1) | ✅ complete |
| Boot-time safety guards (§4.2) | ✅ implemented + tested |
| Local dev stack (§5.1) | ✅ compose file with MailHog sink |
| Celery queues + loss prevention (§9.2) | ✅ configured + tested |
| `accounts.User` | ✅ minimal — `AUTH_USER_MODEL` must be right before the first migration |
| Data model (§6) | ❌ not started |
| Delivery pipeline (§9) | ❌ not started |
| MTA / Postfix / OpenDKIM (§10) | ❌ not started |
| Everything else | ❌ not started |

See [docs/OPEN_QUESTIONS.md](docs/OPEN_QUESTIONS.md) — in particular **Q-BUILD-1**,
the missing §17 phase plan.

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

- App — http://localhost:8000
- **MailHog — http://localhost:8025** — every "sent" message lands here
- RabbitMQ management — http://localhost:15672 (mailer/mailer)
- MinIO console — http://localhost:9001

`make seed` is not yet functional; it depends on the §6 data model and exits
non-zero rather than pretending otherwise.

Run `make help` for the full target list.

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
