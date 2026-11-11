# BUILD.md: §10.5 onward

This file continues the project's `BUILD.md` from §10.5. Sections up to §10.4
(accounts, sending domains, DNS checker, audit log, etc.) live in the copy on
the developer's machine; paste this file's sections after them.

Source of truth: *Turnkey High-Volume Email Infrastructure, Developer-Ready
Business and Technical Specification* (`spec.docx`). References like
"spec §5.2" point at that document. Anything the spec does not define is
**not invented here**: it is either a configurable setting with no sending
effect until someone decides, or an open question in §10.12.

Status legend: **BUILT** (code exists in this folder), **PLANNED** (design
agreed from the spec, not built), **BLOCKED** (needs an answer from §10.12).

---

## 10.5 Templates (BUILT)

Spec basis: §8 "Template / Version", §5.1 steps 3-4, §10.2 message
construction, §12 (output encoding, header injection), §17 Templates epic,
§5.2 Auditability.

### 10.5.1 Data model

Django app `app/email_templates/` (label `email_templates`). Appendix A names
the folder `app/templates/`; it is renamed so it can't be confused with
Django's own `templates/` directories.

| Model | Fields | Notes |
|---|---|---|
| `EmailTemplate` | `name` (unique), `category` (`transactional` / `marketing`), `description`, `owner` (FK user), `created_at`, `updated_at`, `archived_at` | The logical template. Category drives the unsubscribe/footer checks (spec §10.2 applies them to promotional mail only). |
| `TemplateVersion` | `template`, `number` (1, 2, 3 … per template), `subject`, `html_body`, `text_body`, `variables` (JSON), `sample_data` (JSON), `status`, `checksum` (SHA-256), `preflight_report` (JSON), `created_by`, `created_at`, `submitted_by/at`, `reviewed_by/at`, `review_note` | Spec §8: "Subject, HTML, text, variables, owner, approval status, checksum, created date." |

Version status machine:

```
DRAFT --submit--> PENDING_APPROVAL --approve--> APPROVED --retire--> RETIRED
                        |  \--reject-----------> REJECTED
                        \----request changes---> CHANGES_REQUESTED
```

Rules:

- Only `DRAFT` versions can be edited. Once submitted, subject/HTML/text/
  variables/sample data are frozen; the model's `save()` refuses changes.
  Editing an approved template means creating a new version (copied from the
  latest one), which is how "store the exact template version used for each
  send" (spec §10.2) stays true.
- `checksum` is SHA-256 over a canonical JSON of the content fields, so two
  identical versions have the same checksum and any byte change shows.
- Submitting runs preflight (§10.5.3). A version with any `FAIL` cannot be
  submitted. Warnings are stored and shown to the approver.
- Approve, reject and request changes need the `email_templates.approve_templateversion`
  permission (Approver/Compliance Reviewer role, spec §5). Preflight is re-run
  at approval to make sure the stored report matches the frozen content.
- Only `APPROVED` versions can be rendered for a real send. Drafts can only be
  previewed. Several approved versions of one template may exist at once
  (a running campaign keeps its version); `RETIRED` stops new use.
- Every create, edit, submit, approve, reject, request-changes and retire
  writes an audit event (spec §5.2 Auditability) through
  `email_templates.audit.record()`, which calls the project's audit writer
  configured in `EMAIL_TEMPLATES["AUDIT_FUNC"]`.

### 10.5.2 Rendering

`email_templates.rendering.render(version, merge_data, system_data, *, require_approved=True)`
returns a `RenderedMessage(subject, html, text, content_hash, template_version_id, template_checksum)`.

- Engine: Django's template language in a locked-down standalone `Engine`
  (no `{% load %}`, `{% include %}`, `{% extends %}`, `{% url %}`, `{% now %}`,
  no `{% autoescape off %}`, no `|safe`, no `|random`). Allowed tags: `if`,
  `for`, `with`, `comment`, `firstof`, `cycle`, `spaceless`. The engine is
  built with only allowlisted tags and filters loaded, so anything else is a
  syntax error at preflight, not at send time. No new dependency is added.
- Optional content uses `{% if name %}`; a variable that may be absent is
  declared with `"required": false` (and optionally a `default`). Django's
  `|default` filter does **not** cover a missing variable here: a missing
  variable is always an error, so declare it instead.
- Determinism (spec §10.2): no tags or filters that read the clock, random
  numbers, the database or the request. Merge data must be JSON (dict, list,
  str, int, float, bool, null), so templates cannot call Python methods.
- Output encoding (spec §12): HTML is auto-escaped; subject and text are not
  HTML and are rendered without escaping.
- Header injection (spec §12): a rendered subject containing CR or LF, or
  longer than 998 characters (RFC 5322 line limit), is a render error.
- Missing variables: any variable that resolves to nothing is a render
  error naming the variable. A declared variable with a `default` uses it.
- System variables are passed separately from merge data and win over it
  (a recipient's merge data cannot override `unsubscribe_url`). Reserved
  names: `unsubscribe_url` (filled by the unsubscribe component, §10.8.4).
- `content_hash` is SHA-256 of subject + html + text as rendered. The delivery
  pipeline stores it with the send (spec §10.2 "rendered content hash").

### 10.5.3 Preflight

`email_templates.preflight.run(version)` renders the version with its
`sample_data` and returns a list of `Finding(code, level, message)` with
level `FAIL` or `WARN`. Spec §5.1 step 4 lists the checks; the template-level
ones are:

| Spec check | Code | Level | Rule |
|---|---|---|---|
| Missing variables | `MISSING_VARIABLE` | FAIL | Sample render references a variable absent from sample data and with no default. |
| | `UNDECLARED_VARIABLE` | WARN | Sample data key not declared in `variables`. |
| Template syntax / unsafe tags | `SYNTAX_ERROR`, `DISALLOWED_TAG`, `DISALLOWED_FILTER` | FAIL | |
| Unsubscribe mechanism | `NO_UNSUBSCRIBE_HTML`, `NO_UNSUBSCRIBE_TEXT` | FAIL | Marketing only: HTML has an `<a href>` whose value is `{{ unsubscribe_url }}`, and the text part contains `{{ unsubscribe_url }}`. |
| Required footer | `MISSING_FOOTER_VARIABLE` | FAIL | Marketing only: each variable in `EMAIL_TEMPLATES["REQUIRED_MARKETING_VARIABLES"]` appears in both parts. Default list is `["unsubscribe_url"]`; footer content is Q-TPL-1. |
| Empty links | `EMPTY_LINK` | FAIL | Rendered `<a>` with no href, `href=""`, or `href="#"`. |
| | `UNSAFE_LINK` | FAIL | `javascript:`, `data:` or `vbscript:` hrefs. |
| Oversized message | `HTML_TOO_LARGE`, `TEXT_TOO_LARGE` | FAIL | Over `MAX_HTML_BYTES` / `MAX_TEXT_BYTES` (defaults 102,400 / 102,400; Q-TPL-2). |
| Prohibited attachments | `ATTACHMENTS_UNSUPPORTED` | n/a | Templates carry no attachments in this build (spec §10.2: avoid unnecessary attachments). Nothing to check; Q-TPL-3. |
| Plain-text alternative (§10.2) | `EMPTY_TEXT`, `EMPTY_SUBJECT`, `EMPTY_HTML` | FAIL | |
| Header injection (§12) | `SUBJECT_HEADER_INJECTION` | FAIL | |
| Unsafe/hidden content (§10.2, §12) | `UNSAFE_ELEMENT` | FAIL | `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, `on*=` handlers. |
| | `HIDDEN_CONTENT` | WARN | `display:none` / `visibility:hidden` in inline styles. |
| URL shorteners (§10.2) | `URL_SHORTENER` | WARN | Host in `URL_SHORTENER_HOSTS`. |
| Accessibility (§10.2) | `IMG_MISSING_ALT` | WARN | `<img>` without `alt`. |

Sender alignment (also in §5.1 step 4) is a campaign check, §10.6.3.

### 10.5.4 Admin and permissions

- Django admin for `EmailTemplate` with inline version list; `TemplateVersion`
  admin with actions **Submit for approval**, **Approve**, **Reject**,
  **Request changes**, **Retire**, **New version from this one**, and a
  **Preview** page that renders the sample in a sandboxed `<iframe srcdoc>`
  (no scripts, no same-origin) next to the text part and the preflight report.
- Permissions (Django model permissions; map them to the project's roles):
  Campaign Manager: `add/change_emailtemplate`, `add/change_templateversion`,
  `submit_templateversion`. Approver: `approve_templateversion`.
  Read-Only Analyst: `view_*`. System Administrator: all.
- No public API for templates: spec §9 has none. The transactional API
  (§10.6.6) refers to templates by name and uses the newest approved version.

### 10.5.5 Integration points (developer machine)

1. Copy `app/email_templates/` into the project and add it to `INSTALLED_APPS`.
   If apps are not imported as `app.<name>`, change `name` in `apps.py`.
2. Set `EMAIL_TEMPLATES["AUDIT_FUNC"]` to the dotted path of the existing audit
   writer (the one the DNS checker uses). The adapter calls it as
   `func(actor=user, action=str, obj=model_instance, before=dict|None, after=dict|None)`;
   wrap the real writer if its signature differs. Unset, it logs to the
   `email_templates.audit` logger.
3. `python manage.py migrate email_templates`.
4. `python manage.py test app.email_templates` (or the project's runner).

---

## 10.6 Campaigns (PLANNED)

Spec basis: §5.1, §5.2, §8 Campaign, §9 campaign endpoints, §19.

### 10.6.1 Model

`Campaign`: `name`, `owner`, `sender_identity` (FK SenderIdentity),
`template_version` (FK, must be APPROVED), `audience` (FK List/Segment),
`purpose` (`marketing`/`transactional`), `status`, `scheduled_at`,
`approved_by/at`, `snapshot` (FK `CampaignSnapshot`), `created_at`.

`CampaignSnapshot` (immutable, spec §9 approve): template version id +
checksum, sender identity values, audience contact ids + consent record ids
at approval time, preflight report, approver.

Status: `DRAFT -> PREFLIGHT_FAILED | READY_FOR_REVIEW -> APPROVED | REJECTED |
CHANGES_REQUESTED -> SCHEDULED -> SENDING -> COMPLETED`, with `PAUSED` and
`CANCELED` reachable from `SCHEDULED`/`SENDING`.

### 10.6.2 Rules (all from spec)

- Creating a campaign never sends (spec §9).
- Approve is restricted to the approver role and freezes the snapshot (§9).
- Schedule rejects when domain, authentication, consent or warm-up controls
  fail (§9). Domain must be `ACTIVE` (verified + not paused, per the DNS
  checker already built).
- Pause stops future dispatch; already-accepted SMTP messages still complete,
  and the admin says so (§9).
- High-volume or new-domain sends need approval and passing preflight (§5.2).
  The "high-volume" threshold is Q-CMP-1; until answered, every marketing
  campaign needs approval.

### 10.6.3 Campaign preflight (spec §5.1 step 4)

Re-runs template preflight on the chosen version, plus: sender alignment
(From domain equals or is a subdomain of the `SendingDomain`, which is
`VERIFIED`/`ACTIVE`, and DMARC alignment from the DNS checker passed), audience
non-empty, consent present for every marketing recipient (spec §5.2), count of
suppressed recipients shown (excluded, not failed), sample renders for N
recipients' real merge data (N = 5 default, setting).

### 10.6.4 Audience expansion

Scheduler expands the snapshot into `RecipientJob` rows in batches (spec §5.1
step 6). Batch size is a setting (Q-CMP-2). Expansion is idempotent: unique
`(campaign, contact, template_version)` (§5.2 Idempotency).

### 10.6.5 API (spec §9)

`POST /api/v1/campaigns`, `/{id}/approve`, `/{id}/schedule`, `/{id}/pause`,
`GET /{id}/metrics` via DRF, using the same service functions as the admin.

### 10.6.6 Transactional API

`POST /api/v1/messages`: scoped token, approved sender + approved template,
caller-supplied idempotency key, recipient validation, rate limits (§9).
Creates one `RecipientJob` with `purpose=transactional`. Which suppressions
apply to transactional mail is Q-POL-2.

---

## 10.7 Delivery pipeline (PLANNED)

Spec basis: §6.1 data flow, §8 RecipientJob/Message, §5.2 Idempotency,
Appendix B state machine, §11 2xx/4xx/5xx rows.

### 10.7.1 RecipientJob state machine (Appendix B)

```
DRAFT -> ELIGIBLE -> QUEUED -> RENDERED -> SUBMITTED_TO_MTA
      -> ACCEPTED | DEFERRED -> ACCEPTED | BOUNCED | EXPIRED | CANCELED | SUPPRESSED
```

- Transitions are appended to `RecipientJobTransition` (append-only) and
  projected onto `RecipientJob.state` in the same transaction.
- Retries add `SendAttempt` rows; they never create a second RecipientJob.
- `idempotency_key` = SHA-256 of `campaign_id|contact_id|template_version_id`
  (transactional: client key + API client id), unique in the database.

### 10.7.2 Workers (Celery, spec §6)

1. **expand** (campaign scheduler): snapshot -> RecipientJobs in batches.
2. **render**: calls `email_templates.rendering.render()` with the contact's
   merge data and `unsubscribe_url`; stores `content_hash` and the version
   checksum on the job; state RENDERED.
3. **final check + handoff**: asks the policy service (§10.9) immediately
   before handoff (spec §5.1 step 7). `SEND` -> builds MIME and submits to
   the MTA; `SUPPRESS` -> SUPPRESSED; `HOLD` -> stays RENDERED with a
   `next_attempt_at`.
4. Message construction (spec §10.2): Message-ID on an organization-controlled
   domain, Date, multipart/alternative with text first, quoted-printable or
   base64 transfer encoding, From/Reply-To from SenderIdentity, Return-Path
   on the bounce domain, `List-Unsubscribe` + `List-Unsubscribe-Post:
   List-Unsubscribe=One-Click` for marketing. Built with Python's
   `email.message.EmailMessage`, never by string concatenation.
5. Submission: SMTP to the local Postfix submission listener (§10.10), one
   message per recipient. Postfix's queue ID from the 250 reply is stored on
   `Message`. A lost reply after DATA is treated as "maybe sent": the job is
   held for operator review instead of resubmitted (prevents duplicates,
   §5.2). Q-DEL-1 covers whether to accept that trade-off.

### 10.7.3 What the spec leaves open

Retry schedule, max attempts and max age for application-side retries
(Q-DEL-2); broker choice RabbitMQ vs Redis Streams (spec allows both,
Q-DEL-3); per-provider connection limits (they live in Postfix, §10.10).

---

## 10.8 Event pipeline (PLANNED)

Spec basis: §11, §9 `/events/provider` and `/unsubscribe`, §12 webhooks,
§5.2 complaint/hard-bounce stop.

### 10.8.1 Sources

- Postfix delivery log lines (status=sent/deferred/bounced with DSN codes),
  shipped by a log tailer that posts to an internal ingest task.
- DSN messages to the bounce domain's MX (the bounce MX the DNS checker
  already requires), piped to an ingest command.
- FBL / provider webhooks at `POST /api/v1/events/provider`: signature
  check, replay protection (timestamp + nonce window), store raw payload
  reference, normalize asynchronously (§9, §12).
- Unsubscribe clicks and one-click POSTs (§10.8.4).

### 10.8.2 Normalization

Every source becomes a `DeliveryEvent` (spec §8: type, SMTP code, enhanced
code, provider, raw source reference, event time). Ingest is idempotent on a
source-specific key (queue ID + status + timestamp, webhook event id).

| Event (spec §11) | Effect |
|---|---|
| 2xx accepted | Job ACCEPTED, Message.accepted_at. |
| 4xx | Job DEFERRED (MTA retries; Postfix owns retry). |
| 5xx / hard bounce | Job BOUNCED; add Suppression(reason=hard_bounce). |
| Soft bounce | Count per contact in a window; suppress over threshold (numbers: Q-EVT-1). |
| Complaint / FBL | Suppression(reason=complaint, scope=marketing) immediately; alert on rate threshold (Q-EVT-2). |
| Unsubscribe | Suppression by scope; record withdrawal on ConsentRecord. |
| Unknown DSN | Stored raw, classified `unknown`, shown in an operator review queue; no suppression. |

How to classify 5xx "policy rejections" vs "invalid recipient" (spec §11
says they "may require different remediation") is Q-EVT-3; until answered,
every permanent failure suppresses that recipient, as the spec requires
"repeated delivery must stop when permanent".

### 10.8.3 Aggregation

Per-campaign counters for spec §14 Business telemetry, computed from
DeliveryEvents by a periodic task; `GET /campaigns/{id}/metrics` reads them.

### 10.8.4 Unsubscribe

Token = signed (Django `signing`) contact id + scope + campaign id, no expiry
by default (spec: "remain functional"). `GET` shows a one-button confirm page,
`POST` (including RFC 8058 one-click) suppresses immediately, idempotent, no
login (§9, §11). The token URL is what rendering receives as `unsubscribe_url`.
Scope choices (all marketing vs. one list) are Q-EVT-4.

---

## 10.9 Policy service (PLANNED, partly BLOCKED)

Spec basis: §5.2 rules, §6 "Policy/Filtering", §10.3, §10.4, §12 quotas.

One function, `policy.decide(job) -> Decision(SEND | SUPPRESS | HOLD, reasons)`,
called by the handoff worker immediately before MTA submission (spec §5.1
step 7). It only combines rules the spec states:

| Order | Check | Result on failure | Spec |
|---|---|---|---|
| 1 | Global kill switch / pause of campaign, domain, API client, IP pool, route | HOLD | §5.2 Emergency pause |
| 2 | Suppression (address or domain, scope matches purpose) | SUPPRESS | §5.2 Global suppression wins |
| 3 | Marketing: active ConsentRecord (not withdrawn) | SUPPRESS | §5.2 Consent required |
| 4 | Sending domain `ACTIVE` | HOLD | DNS checker + §9 schedule |
| 5 | Template version still APPROVED or RETIRED-after-snapshot, checksum matches snapshot | HOLD | §5.2 Governance |
| 6 | Rate limits by tenant, campaign, domain, provider, IP, credential | HOLD with `next_attempt_at` | §5.2 Rate governance |
| 7 | Warm-up limit for the domain/IP/provider | HOLD | §10.4 |

Every decision is logged with its reasons. Checks 1-5 are buildable now.
Checks 6-7 are built as a mechanism (token buckets in Redis/DB, limits read
from a `RateLimit` table) with **no default numbers**: an empty table means
"no limit configured" and the admin shows a warning; the limits themselves are
Q-POL-1. The spec forbids rotation to evade reputation (§10.3), so routing
uses a fixed `stream -> IP pool` assignment, never random selection.

Automatic shutdown on abnormal volume or complaint spikes (§12) sets the
pause flags in check 1; thresholds are Q-EVT-2.

---

## 10.10 Mail server (PLANNED)

Spec basis: §6 MTA/DKIM rows, §7 baseline, §10.1, §12, §13.

Delivered as versioned config under `mta/` plus Ansible under
`infrastructure/ansible/` (Appendix A). Not containerised (spec §13).

- **Postfix**: submission listener bound to the application network only;
  `smtpd_client_restrictions`/`mynetworks` limited to app hosts or SASL
  clients (no open relay, §12). `smtp_tls_security_level = may` outbound,
  TLS logging on. Per-stream transports (`marketing`, `transactional`) with
  `smtp_bind_address` set to that stream's pool IP and `smtp_helo_name`
  matching its PTR (§10.1, §10.3). Per-provider `transport_maps` entries for
  connection/rate limits (`*_destination_concurrency_limit`,
  `*_destination_rate_delay`), values from Q-POL-1. `bounce_queue_lifetime`
  and `maximal_queue_lifetime` from Q-DEL-2.
- **DKIM**: OpenDKIM as a milter (spec §6 allows OpenDKIM or Rspamd; OpenDKIM
  is the smaller piece). 2048-bit RSA keys (the DNS checker already warns
  under 2048), selector per domain per rotation period, `SigningTable` /
  `KeyTable` generated from `SendingDomain`. Private keys from the secret
  store, never Git (§6 Secrets, §12). Rotation cadence is Q-MTA-1.
- **Bounce domain**: MX for the return-path domain delivers to a pipe into the
  event pipeline's DSN ingest (§10.8.1).
- **Logs**: Postfix logs to journald/syslog, shipped to Loki/OpenSearch and the
  §10.8 tailer.
- **Validation**: `postfix check`, `postmap` of all maps, and an automated
  open-relay test in CI before activation (§13 configuration promotion, §15).
- Staging Postfix routes everything to a sink (`transport_maps: * discard:`
  or a test mailbox) so staging cannot reach public recipients (§6.2).

---

## 10.11 Build order

1. Templates (§10.5): done.
2. Policy checks 1-5 and suppression lookups (§10.9): needed by everything
   that sends.
3. Campaigns model, preflight, approval, snapshot (§10.6), using templates.
4. Delivery pipeline against a local Postfix sink in staging (§10.7, §10.10).
5. Event pipeline: Postfix log + DSN ingest, unsubscribe (§10.8).
6. Transactional API (§10.6.6).
7. Rate limits and warm-up (§10.9 checks 6-7) once Q-POL-1 is answered.

---

## 10.12 Open questions

Add these to the project's existing open-questions log (next to Q-DATA-1).
Each has a safe default that sends nothing extra while unanswered.

| ID | Question | Default until answered |
|---|---|---|
| Q-TPL-1 | What must a marketing footer contain (postal address, sender name, reason for receiving)? Spec §5.1 says "required footer" without content. | Only `unsubscribe_url` is required; add names to `REQUIRED_MARKETING_VARIABLES` when decided. |
| Q-TPL-2 | Size limit for "oversized message"? | 102,400 bytes HTML and text (Gmail clips HTML above ~102 KB). |
| Q-TPL-3 | Are attachments ever allowed? | No attachments in templates. |
| Q-TPL-4 | Must the approver be a different person from the author? Spec does not say. | Not enforced. |
| Q-CMP-1 | What counts as "high-volume" for mandatory approval (§5.2)? | All marketing campaigns need approval. |
| Q-CMP-2 | Expansion batch size and cadence. | Setting, 1,000 per batch. |
| Q-DEL-1 | On a lost SMTP reply after DATA, hold for review (no duplicates) or resend (no loss)? | Hold for review. |
| Q-DEL-2 | Retry schedule, max attempts, max queue age. | Postfix defaults (5 days); not tuned. |
| Q-DEL-3 | RabbitMQ or Redis Streams as broker? | Whatever the existing Celery setup uses. |
| Q-EVT-1 | Soft-bounce threshold and window. | Count only; never auto-suppress on soft bounces. |
| Q-EVT-2 | Complaint-rate and volume alert/auto-pause thresholds. | Alert only, no auto-pause. |
| Q-EVT-3 | How to treat 5xx policy rejections vs invalid recipients. | Suppress both. |
| Q-EVT-4 | Unsubscribe scope: all marketing, or per list? | All marketing from that organization. |
| Q-POL-1 | Rate limits per tenant/campaign/domain/provider/IP/credential and warm-up volumes. | No limits applied; admin warns that none are configured. Do not start production traffic before this is answered. |
| Q-POL-2 | Which suppressions apply to transactional mail (§5.2 says exceptions need legal review)? | All suppressions apply to transactional mail too. |
| Q-MTA-1 | DKIM key rotation period. | Manual rotation only. |

---

## Appendix: templates component files (this folder)

```
app/email_templates/
  apps.py             AppConfig (name "app.email_templates", label "email_templates")
  conf.py             EMAIL_TEMPLATES settings and defaults
  models.py           EmailTemplate, TemplateVersion (frozen after submit, SHA-256 checksum)
  _library.py         allowlisted template tags and filters
  rendering.py        render() -> RenderedMessage(subject, html, text, content_hash, ...)
  preflight.py        template preflight checks (§10.5.3)
  services.py         create / save_draft / new_version_from / submit / approve / reject /
                      request_changes / retire, with permission checks and audit
  audit.py            adapter to the project's audit writer
  admin.py            admin, actions, review page with sandboxed preview
  templates/admin/email_templates/templateversion/review.html
  migrations/0001_initial.py
  tests/              41 tests; standalone_settings.py runs them without the main project
```

Run standalone (from this folder): `PYTHONPATH=. django-admin test app.email_templates --settings=app.email_templates.tests.standalone_settings`.
Verified on Django 5.2 / Python 3.13. `app/__init__.py` here is empty; don't copy it over the project's own.
