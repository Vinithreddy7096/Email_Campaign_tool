# ADR-0001: Stack selection

- **Status:** Accepted
- **Source:** BUILD.md §2 / §2.1, spec §7

## Context

The platform must deliver up to 100,000 messages/day. That is ~1.16 msg/sec
sustained, which is trivial for any competent MTA. The engineering difficulty is
not throughput; it is per-provider rate governance, retry semantics without
duplicate sends, event normalization from four dissimilar sources, suppression
correctness under concurrency, and warm-up discipline.

The stack is therefore selected for operational legibility and correct delivery
semantics, not for raw speed.

## Decision

| Layer | Choice |
| --- | --- |
| Web/API | Django 5.x + DRF |
| Frontend | Django templates + HTMX + Alpine.js + Tailwind |
| Database | PostgreSQL 16 |
| Broker | RabbitMQ 3.13 |
| Workers | Celery 5.x |
| MTA | Postfix 3.8 |
| Signing | OpenDKIM |
| Policy | Custom Postfix policy service (Python) |
| Cache/locks | Redis 7 (**not** the broker) |
| Object storage | S3-compatible — MinIO local, S3 prod |
| Metrics / logs | Prometheus + Grafana + Alertmanager / Loki + Promtail |
| IaC | Terraform (infra) + Ansible (config) |

## Rejected alternatives

**Serverless / Vercel for the app tier.** The app tier *could* run there, but the
MTA cannot: port 25 egress, a static IP under our control, and PTR record
ownership are all mandatory and none are available on serverless platforms.
Splitting the app onto a different platform from the workers adds a network
boundary for no gain. The whole app tier deploys on the same hosts as the workers.

**Redis as the broker.** Acceptable per the spec for a smaller system, but
RabbitMQ's per-message ack semantics and dead-letter exchanges are precisely what
prevent silent message loss — an explicit acceptance criterion (spec §19: "No
accepted message is silently lost"). Redis remains, for rate-limit counters,
idempotency locks, and the policy-service suppression cache.

**Postfix in a disposable container.** The mail queue is durable state. Postfix
runs on a hardened host, or in a container with a persistent volume, host
networking, and an explicit drain procedure. Never `docker compose down` a
production MTA with a non-empty queue.

**Haraka / PowerMTA.** Haraka needs custom plugin work we do not need. PowerMTA's
licence cost fights the cost-reduction objective. Revisit only above ~2M/day.

## Consequences

- Django's admin, migrations, and RBAC give us the approval workflow in days
  rather than weeks. The cost is Django's conventions throughout.
- Running our own MTA means we own PTR records, IP reputation, blocklist
  remediation, and a 24/7 operational burden that an ESP would otherwise absorb.
  Spec §18 flags "operational complexity exceeds savings" as a live risk; the TCO
  model must be maintained, not assumed.
- RabbitMQ adds a component over Redis-as-broker, justified solely by delivery
  semantics.
- The local development stack contains **no Postfix**. Local dev delivers to
  MailHog; Postfix is first exercised in staging.
