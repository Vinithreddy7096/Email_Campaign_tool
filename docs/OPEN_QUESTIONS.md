# Open Questions

Per BUILD.md ground rule §0.1.3: no config value, DNS record, IP, or credential
has been invented. Everything unsupplied is listed here, expressed as a required
env var that fails loudly at boot.

**Status key:** 🔴 blocking · 🟠 blocks a later phase · 🟡 decision to ratify

---

## Q-BUILD-1 🔴 BUILD.md is truncated — the phase plan is missing

The handoff document supplied to the agent was cut at 50,000 characters, ending
mid-sentence in §10.5 ("It must fail closed on suppression (if Postgres is
unre…"). The following sections were never received:

| Section | Subject | Consequence of absence |
| --- | --- | --- |
| §10.5 (partial) | Policy service fail-closed behaviour | Authoritative suppression check underspecified |
| §11 | Warm-up ramp algorithm | `SendingDomain.daily_cap` progression is undefined |
| §12–§16 | Unknown — likely observability, security, runbooks, testing | Unknown |
| **§17** | **Phase order and gates** | **Ground rule §0.1.1 makes this the governing build sequence** |

What is inferable from surviving cross-references only:

- §5.3 → "Every phase before Phase 6 runs with `MOCK_MODE=true`" ⇒ Phase 6 is the
  first phase permitted to send real mail.
- §5.1 → "Postfix is exercised in the staging environment only (Phase 5)."
- Therefore §17 has at least six phases; phases 1–4 are unknown.

**Needed:** the remainder of BUILD.md from §10.5 onward. Until then the build is
sequenced against the source spec's §16 four-phase plan as a fallback, which is
coarser than BUILD.md's own gating and is *not* an equivalent substitute.

## Q-DNS-1 🟠 Sending identity is entirely unspecified

`.env.example` ships `example.com` values, which are RFC 2606 reserved and
deliberately non-functional. Required before any staging send:

| Variable | Needs |
| --- | --- |
| `MESSAGE_ID_DOMAIN` | An organization-controlled domain for `Message-ID` |
| `DEFAULT_RETURN_PATH_DOMAIN` | Dedicated bounce subdomain, MX pointed at the bounce mailbox |
| `DEFAULT_TRACKING_DOMAIN` | Dedicated link/tracking subdomain |
| — | The list of sending subdomains, split by transactional vs marketing stream |
| — | DNS administrator contact and the change-request process |

The production boot guard already refuses to start without the first two.

> Cost, paid-service justification, and free alternatives for every item below are
> analysed in [COST_AND_ALTERNATIVES.md](COST_AND_ALTERNATIVES.md).

## Q-HOST-1 🟠 Hosting provider and IP allocation not confirmed

Spec §13 and §18 require *written* confirmation before purchase. None supplied:

- Outbound email permitted under the provider's AUP.
- Port 25 egress available and unfiltered.
- Dedicated static IPs with customer-controlled PTR records.
- IP reputation history evaluated before acceptance.
- Defined abuse-handling process and contact.

Nothing about the MTA tier can be provisioned until this is answered. This is
risk #1 in spec §18 ("Deployment blocked or service terminated").

## Q-SEC-1 🟠 Secret management strategy

- `SECRET_KEY` — unsupplied. Local dev uses a clearly-marked insecure constant;
  the production guard requires ≥50 chars.
- `WEBHOOK_SIGNING_SECRET` — unsupplied. Required in production.
- **Field encryption key.** `EncryptedCharField` currently derives its key from
  `SECRET_KEY` (see [app/common/fields.py](app/common/fields.py)). Rotating
  `SECRET_KEY` would make every stored MFA secret undecryptable. A dedicated,
  independently-rotatable key in Vault is the correct design. Decide before any
  MFA secret is written.
- DKIM private keys — generation, custody, and Vault path undefined. Keys must
  never enter the repo; `.gitignore` blocks `**/opendkim/keys/`.
- Vault vs. cloud secret manager vs. Ansible Vault — not chosen.

## Q-LEGAL-1 🟠 Consent and suppression rules need compliance sign-off

Spec §20 requires these approved by compliance/legal before build completion:

- The **transactional exception allowlist** (§8.3): which contractual bases
  qualify, and who signs off. Code must never offer a flag that lets a
  transactional message override a `GLOBAL` suppression (§8.2).
- `CONSENT_MAX_AGE_DAYS` — added to `.env.example` as empty (no ceiling).
  BUILD.md §8.3 references it conditionally but §4.1 omits it. Confirm whether a
  consent-age ceiling applies in the relevant jurisdictions.
- Data-retention periods for contacts, events, and raw provider payloads.
- GDPR erasure procedure: §6.3 retains `email_hash` after `deleted_at`. Confirm
  that hash retention is acceptable to counsel.

## Q-OPS-1 🟠 Warm-up audience and provider volume estimates

Spec §20 requires provider-specific volume estimates and a warm-up audience
definition. Neither supplied. Combined with the missing §11, the entire warm-up
ramp is currently unspecified. `SendingDomain.daily_cap` defaults to 50.

## Q-ENV-1 🟡 `ENVIRONMENT` is derived, not configured

BUILD.md §4.2 branches on `ENVIRONMENT`, but §4.1 defines no such variable.
Resolved by having each settings module declare its own literal and pass it to
`run_boot_guards()`. This is stricter than an env var — the environment cannot
drift from the settings module in use. Flagged for ratification.

## Q-ENV-2 🟡 Boot guards raise instead of asserting

§4.2 is written with bare `assert`. Bare asserts are removed when Python runs
under `-O`, which would silently disable the interlock keeping `MOCK_MODE` off in
production. Implemented as explicit `ImproperlyConfigured` raises instead. The
guard set was also extended to cover `MESSAGE_ID_DOMAIN`,
`DEFAULT_RETURN_PATH_DOMAIN`, `ABUSE_CONTACT_EMAIL`, and
`WEBHOOK_SIGNING_SECRET`, none of which have a safe production default.

## Q-RUN-1 🟡 `CELERY_BROKER_URL` was missing — found only by running the stack

§4.1 defines `RABBITMQ_URL`, and `config/celery.py` loads settings under the
`CELERY_` namespace. Nothing connected the two, so `broker_url` resolved to
`None` and Celery fell back to its default `amqp://guest@localhost`.

The failure mode is the dangerous kind: `docker compose ps` reported all five
workers as **running** while every one of them retried a refused connection
forever. `manage.py check`, `mypy`, `ruff`, and 33 passing tests all went green.
Only `GET /health/ready` caught it, which is the argument for the readiness probe
checking the broker rather than just the process.

Fixed by `CELERY_BROKER_URL = RABBITMQ_URL` in `base.py`, with a regression test
asserting `celery_app.conf.broker_url == settings.RABBITMQ_URL`.

## Q-RUN-2 🟡 A `maintenance` worker was added; §5.1 omits one

§9.2 specifies a `maintenance` queue at concurrency 2, and `config/celery.py`
sets `task_default_queue = "maintenance"` so nothing lands on `deliver` by
accident. But §5.1's local service table lists only `worker-expand`,
`worker-render`, `worker-deliver`, `worker-events`, and `beat`.

With no consumer, the default routing target silently accumulates work — beat
would schedule aggregations, DNS checks, cleanup, and warm-up ticks that never
execute. `worker-maintenance` was added to `docker-compose.yml`. Confirm this is
the intended topology, or confirm §5.1 is authoritative and the default queue
should be something else.

## Q-ENV-3 🟡 `.env.example` inline comments were moved to their own lines

BUILD.md §4.1 writes three values with a trailing `#` comment on the same line:

```
DKIM_KEY_DIR=/etc/opendkim/keys        # private keys NEVER in the repo
COMPLAINT_RATE_ALERT_THRESHOLD=0.001    # 0.1%
HARD_BOUNCE_RATE_ALERT_THRESHOLD=0.02   # 2%
```

`django-environ` does not strip inline comments — it takes everything after `=`
as the value. `env.float("COMPLAINT_RATE_ALERT_THRESHOLD")` would therefore
receive `"0.001    # 0.1%"` and raise at import, and `DKIM_KEY_DIR` would point
at a path with a comment glued to it, defeating the production key-permissions
guard.

The comments were moved to the preceding line. No values changed.

`CONSENT_MAX_AGE_DAYS` ships empty for the same reason — it is read as a string
and converted, because an empty value cannot be cast to `int` directly.

## Q-REPO-1 🟡 Repository root

BUILD.md §3 names the root `email-platform/`. The supplied working directory is
`email_app/`, which was used as the root directly rather than nesting a second
project folder inside it. Everything below the root matches §3 exactly.

## Q-SCRIPT-1 🟡 Deliberately unimplemented scripts

`scripts/seed_dev.py`, `scripts/validate_dns.py`, and `scripts/relay_test.py`
exist but exit non-zero with an explanation. They depend on later phases. They
fail loudly by design — a missing open-relay test must never be mistaken for a
passing one.
