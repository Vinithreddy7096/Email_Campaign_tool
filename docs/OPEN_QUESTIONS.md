# Open Questions

Per BUILD.md ground rule §0.1.3: no config value, DNS record, IP, or credential
has been invented. Everything unsupplied is listed here, expressed as a required
env var that fails loudly at boot.

**Status key:** 🔴 blocking · 🟠 blocks a later phase · 🟡 decision to ratify

---

## Q-BUILD-1 🟠 BUILD.md received from §10.5 onward — numbering differs from the truncated copy

The remainder of BUILD.md was supplied on 2026-10-13 and is kept at
[BUILD-10.5-onward.md](BUILD-10.5-onward.md). It defines templates (§10.5,
built and integrated), campaigns (§10.6), delivery (§10.7), events (§10.8), the
policy service (§10.9), the mail server (§10.10), a build order (§10.11) and its
own open questions (§10.12, copied below).

It is **not a literal continuation** of the truncated copy the build started
from. That copy's §10.5 was the policy service ("It must fail closed on
suppression…"); the new file puts templates at §10.5 and policy at §10.9. The
new file also does not mention the "Phase 5 / Phase 6" gates that §5.1 and §5.3
of the truncated copy referred to. **Treated as authoritative:** the new file's
§10.11 build order. **Still open:** whether the Phase-6 rule ("first phase
permitted to send real mail") still applies — until told otherwise, MOCK_MODE
stays on everywhere except production, as the boot guards already enforce.

## Q-DATA-1 🔴 Core data model was built without the §6 field list

Neither BUILD.md nor the source `.docx` is in the repository. Only the section
headings of §6 survive (in each app's docstring). On the owner's instruction,
the low-ambiguity apps — `domains` (§6.2), `contacts` (§6.3), `consent` (§6.4),
`suppression` (§6.5), `audit` (§6.10) — were built as **provisional** models.
`templating`, `campaigns`, `delivery` and `events` were untouched
until the spec arrives.

Each module is marked PROVISIONAL. When the spec is available, reconcile it
against these assumptions; anything that disagrees is a new migration, not an
edit to `0001`:

| Assumption | Where |
| --- | --- |
| Address normalization lowercases the **local part** too, and punycode-encodes the domain | `common/email.py` |
| `email_hash` is **unkeyed SHA-256** — a match key, not anonymization; re-identifiable by hashing a known address. Also a Q-LEGAL-1 question | `common/email.py` |
| Contacts are soft-deleted; `erase()` nulls PII and keeps `email_hash`. Uniqueness is on `email_hash` among live contacts only | `contacts/models.py` |
| Imports require a non-empty `consent_source` and `attestation_text` plus a named `attested_by` user | `contacts/models.py` |
| `Segment.definition` is opaque JSON; the rule language is unspecified | `contacts/models.py` |
| Consent state = latest record per (contact, stream, list); `NULL` list = stream-wide. `CONSENT_MAX_AGE_DAYS` applies to marketing only | `consent/models.py` |
| On an `occurred_at` tie between a grant and a withdrawal, **the withdrawal wins** (fail closed). Ties are real: Windows clocks tick every ~15 ms, and UUIDv7 ids are unordered within a millisecond | `consent/models.py` |
| Suppression scopes are `GLOBAL` / `STREAM` / `LIST`. `GLOBAL` can never be lifted (CHECK constraint); scoped rows can be lifted by a named user with a reason, which is audited | `suppression/models.py` |
| One active suppression per (address, scope, target). Relies on `NULLS NOT DISTINCT` — **requires Postgres ≥ 15** | `suppression/models.py` |
| A sending domain cannot be `ACTIVE` until SPF, DKIM and DMARC have all been verified; a domain may not use the other stream's IP pool | `domains/models.py` |
| From addresses are composed from a local part plus a `SendingDomain`, so a From on a domain we don't control cannot be stored | `domains/models.py` |
| Status and reason enums (domain, IP, import, consent method, suppression reason) are reasonable defaults, not spec values | all of the above |
| **Roles:** `SYSTEM_ADMIN`, `APPROVER`, `CAMPAIGN_MANAGER`, `CONTENT_EDITOR`, `ANALYST`. Only the first two are attested (by `MFA_REQUIRED_ROLES` in §4.1); the other three are guesses. Created by migration `accounts/0003` | `accounts/models.py` |
| Roles are a flat set with no permission matrix yet — nothing checks them except `mfa_required`. What each role may do is unspecified | `accounts/models.py` |
| **API tokens:** `ept_<prefix>_<secret>`, SHA-256 of the secret stored, default TTL from `API_TOKEN_DEFAULT_TTL_DAYS`. Scope names are free-form strings with no vocabulary yet; empty scopes grant nothing, but no endpoint checks scopes yet | `accounts/models.py`, `api/authentication.py` |
| **MFA:** TOTP (SHA-1, 6 digits, 30 s, ±1 step drift), as every authenticator app expects. Required for `MFA_REQUIRED_ROLES` **and all superusers**; anyone who has enrolled is always challenged. Each login starts unverified | `accounts/totp.py`, `accounts/middleware.py` |
| 5 consecutive bad codes lock the account for 15 minutes; an expired lock restarts the count. A code's time step can be used once (replay protection) | `accounts/models.py` |
| Lost device: `manage.py reset_mfa <email>` or the admin "Reset MFA" action. **No recovery codes** — every recovery goes through an admin. Confirm that is acceptable | `accounts/` |
| MFA applies to browser sessions only. API tokens are separate credentials and are not MFA-gated | `accounts/middleware.py` |
| **DNS verification:** a domain is promoted `PENDING_VERIFICATION → VERIFIED` only when SPF, DKIM, DMARC, bounce MX and reverse DNS for every pool IP all pass. An `ACTIVE` domain whose SPF, DKIM or DMARC fails is **automatically `PAUSED`**; going back to `ACTIVE` is always a human action. A DNS timeout changes nothing | `workers/services/domain_verification.py` |
| Re-verification runs every 6 hours (`crontab(minute=17, hour="*/6")`) — the interval is a guess | `config/celery.py` |
| DMARC's organizational domain is approximated by walking labels, not the Public Suffix List — wrong for `co.uk`-style suffixes. SPF evaluation is a posture check, not a full RFC 7208 evaluator (no macros; ignores mechanism order) | `workers/services/dns_checks.py` |
| DKIM: RSA < 1024 bits fails (RFC 8301); 1024–2047 warns; ed25519 passes. DMARC `p=none` and missing `rua=` warn but still verify | `workers/services/dns_checks.py` |

**Immutability enforcement.** UPDATE/DELETE on `consent_record` and
`audit_event`, and DELETE on `suppression`, are blocked by Postgres triggers
(`common/db.py`) as well as in the ORM. `TRUNCATE` is *not* trigger-blocked,
because Django's `flush` and transactional tests rely on it. The production
database role used by the app must therefore **not** own these tables and must
lack `TRUNCATE` (and ideally `UPDATE`/`DELETE` on the append-only ones). That
grant layout is not yet written — it belongs with the infrastructure phase.

## BUILD.md §10.12 open questions

Copied from [BUILD-10.5-onward.md](BUILD-10.5-onward.md) §10.12. Each has a safe
default that sends nothing extra while unanswered.

| ID | Question | Default until answered |
| --- | --- | --- |
| Q-TPL-1 | What must a marketing footer contain (postal address, sender name, reason for receiving)? | Only `unsubscribe_url` is required; add names to `REQUIRED_MARKETING_VARIABLES` when decided. |
| Q-TPL-2 | Size limit for "oversized message"? | 102,400 bytes HTML and text. |
| Q-TPL-3 | Are attachments ever allowed? | No attachments in templates. |
| Q-TPL-4 | Must the approver be a different person from the author? | Not enforced. (In practice the role mapping already separates them: `CAMPAIGN_MANAGER` cannot approve, `APPROVER` cannot author. `SYSTEM_ADMIN` can do both.) |
| Q-CMP-1 | What counts as "high-volume" for mandatory approval? | All marketing campaigns need approval. |
| Q-CMP-2 | Expansion batch size and cadence. | Setting, 1,000 per batch. ⚠️ `.env.example` already has `CAMPAIGN_EXPANSION_BATCH_SIZE=500` — reconcile when §10.6 is built. |
| Q-DEL-1 | On a lost SMTP reply after DATA: hold for review or resend? | Hold for review. |
| Q-DEL-2 | Retry schedule, max attempts, max queue age. | Postfix defaults (5 days). ⚠️ The app already defines `RETRY_BACKOFF_SECONDS`, `MAX_DELIVERY_ATTEMPTS=6`, `MESSAGE_MAX_AGE_HOURS=72` from the earlier §9.4 — reconcile when §10.7 is built. |
| Q-DEL-3 | RabbitMQ or Redis Streams? | Whatever the existing Celery setup uses — RabbitMQ. |
| Q-EVT-1 | Soft-bounce threshold and window. | Count only; never auto-suppress on soft bounces. ⚠️ Conflicts with `SOFT_BOUNCE_SUPPRESS_THRESHOLD=5` in `.env.example` and with `Suppression.Reason.SOFT_BOUNCE_LIMIT` — the §10.12 default wins when §10.8 is built. |
| Q-EVT-2 | Complaint-rate and volume alert/auto-pause thresholds. | Alert only, no auto-pause. |
| Q-EVT-3 | 5xx policy rejections vs invalid recipients. | Suppress both. |
| Q-EVT-4 | Unsubscribe scope: all marketing, or per list? | All marketing from that organization — i.e. `Suppression(scope=STREAM, stream=MARKETING)`. |
| Q-POL-1 | Rate limits and warm-up volumes. | No limits applied; admin warns that none are configured. **Do not start production traffic before this is answered.** |
| Q-POL-2 | Which suppressions apply to transactional mail? | All suppressions apply to transactional mail too. Applied by the policy service: transactional mail is blocked by **every** active row for the address or its domain, including marketing-stream and list rows. |
| Q-MTA-1 | DKIM key rotation period. | Manual rotation only. |

### Policy service (§10.9) — interpretations

Built in `workers/services/policy.py`. Where §10.9 left room, these choices were
made, each erring towards not sending:

| Choice | Why |
| --- | --- |
| `decide()` takes a `SendRequest`, not a `RecipientJob` | RecipientJob is §10.7, built later; it will construct a `SendRequest`. `PolicyDecision.job_ref` becomes a FK then |
| The first failing check, in spec order, decides; every failing check is logged | A paused-and-suppressed recipient is HOLD now and SUPPRESS when sending resumes — both safe |
| An exception in a check → HOLD (`POLICY_ERROR`); a SEND that cannot be logged → HOLD (`POLICY_LOG_FAILED`) | Fail closed; §10.9 requires every decision to be logged |
| State is re-read from the database on every decision | A job holding a stale `ACTIVE` domain must not send after it was paused |
| Consent: a stream-wide or this-list grant suffices; **any** current withdrawal (stream-wide or this list) blocks | A later list re-subscribe does not override an "unsubscribe from all" — that needs a fresh stream-wide grant |
| `STREAM_MISMATCH`: a domain may only carry its own stream | §10.3 fixed stream → pool routing |
| `TEMPLATE_PURPOSE_MISMATCH`: template category must match the purpose | A transactional template has no unsubscribe link; it must never go out as marketing |
| `TEMPLATE_TAMPERED`: stored checksum must match the content | Catches edits made in SQL, bypassing the frozen-version rule |
| Non-production with `MOCK_MODE=false` only sends to `ALLOWED_TEST_RECIPIENT_DOMAINS` | Enforces the §4.2 guard at send time, not only at boot |
| Pause flags (`SendPause`): GLOBAL, STREAM, DOMAIN, IP_POOL, CAMPAIGN, API_CLIENT; never deleted, resume is audited | §10.9 check 1. Automatic shutdown (§12) will create these; thresholds are Q-EVT-2 |
| `Suppression` can target a whole recipient domain (`Suppression.suppress_domain`) | §10.9 check 2: "address or domain" |
| `SYSTEM_ADMIN` holds every model permission in the platform apps (not `django.contrib.auth`) | §10.5.4 "System Administrator: all"; admin view-only/no-delete rules still apply |
| Checks 6–7 (rate limits, warm-up) not built | Their limits are Q-POL-1; the spec says not to start production traffic before it is answered |

### Campaigns (§10.6) — interpretations

| Choice | Why |
| --- | --- |
| Audience is a `ContactList` only | `Segment` rules have no defined language or evaluator (Q-DATA-1); add `audience_segment` when they do |
| **Every** campaign needs approval, transactional included | Q-CMP-1's default covers marketing; transactional bulk campaigns are rare and the snapshot only exists after approval |
| A marketing recipient without valid consent **fails** preflight (`MISSING_CONSENT`); suppressed recipients are excluded and counted (`SUPPRESSED_EXCLUDED`, INFO) | §10.6.3 says exactly this: consent "present for every marketing recipient"; suppressed "excluded, not failed". One stale contact blocks the campaign until the list is cleaned |
| The snapshot stores recipients as rows (`CampaignSnapshotRecipient`: contact, email hash, the consent record relied on), not a JSON array | 100k ids in one JSON blob is the wrong shape; rows are append-only like the snapshot |
| Approval re-runs preflight; if it now fails the campaign goes back to `PREFLIGHT_FAILED` | The list or consent may have changed since submission |
| Scheduling rejects: domain not `ACTIVE`, SPF/DKIM/DMARC not all verified, template no longer `APPROVED`, time in the past, or **no** recipient still has consent. Recipients who lost consent since approval are a warning — the policy suppresses them at send time | §9 "rejects when domain, authentication, consent or warm-up controls fail"; rejecting a 10k campaign over one unsubscribe would be unworkable |
| Warm-up controls cannot be checked: schedule always warns "No warm-up or rate limits are configured (Q-POL-1)" | Q-POL-1 |
| Pause raises a `SendPause(CAMPAIGN)` so already-queued jobs are held at handoff; resume lifts only that pause, never an operator's separate one. Cancel raises one that is never lifted | §10.6.2 "pause stops future dispatch"; the policy service is the single gate |
| Cancel is allowed from `APPROVED` as well as `SCHEDULED`/`SENDING`/`PAUSED` | An approved-but-unscheduled campaign should be cancellable |
| Approvers may pause and cancel; only managers schedule | An emergency stop should not need the author |
| API token scopes: `campaigns:read`, `campaigns:write`, `campaigns:approve` (new vocabulary). Approve needs its own scope **and** the approver role | API tokens bypass MFA; approval is the step the spec reserves for approvers |
| `POST /api/v1/campaigns/{id}/submit` added; `GET /{id}` added | §10.6.5 lists create/approve/schedule/pause/metrics, but an API-only client cannot reach `READY_FOR_REVIEW` without submit |
| `GET /{id}/metrics` returns snapshot audience counts; delivery counters are `null` until §10.8.3 | No delivery data exists yet |
| `CAMPAIGN_PREFLIGHT_SAMPLE_SIZE=5` (new setting) | §10.6.3 "N = 5 default, setting" |
| Merge data per recipient: contact `attributes`, then `first_name`, `last_name`, `email` (named fields win) | Not specified; the delivery pipeline will reuse `campaigns.preflight.contact_merge_data` |
| `consent.evaluation` and `Suppression.send_scope` are the single source of the consent and suppression rules for both preflight and the policy service; a test asserts the bulk and single consent paths agree on every history | Preflight and send time must never disagree |

### Delivery pipeline (§10.7) — interpretations

| Choice | Why |
| --- | --- |
| **Q-DEL-1 default applied:** a reply lost *after* the message body was sent → job stays `SUBMITTED_TO_MTA` with `review_required`; never resent automatically. Operators resolve it in the admin: accepted / cancel / send again | §10.7.2 item 5. The SMTP dialogue is driven step by step so "before the body" (safe to retry) and "after the body" (maybe sent) are distinguishable |
| The job is committed as `SUBMITTED_TO_MTA` **before** the SMTP call; a sweep flags jobs left there past 2 × `MTA_SUBMISSION_TIMEOUT_S` + 60 s | A worker that dies mid-send must look "maybe sent", not "not sent" |
| App-side retries (submission 4xx, connection errors) use the existing `RETRY_BACKOFF_SECONDS` / `MAX_DELIVERY_ATTEMPTS` / `MESSAGE_MAX_AGE_HOURS`. Once the MTA accepts, Postfix owns retry | Q-DEL-2 is still open; these were already configured from the earlier §9.4 |
| A 5xx to **MAIL FROM**, auth or TLS failures defer — never bounce | Those are our configuration, not the recipient |
| A 5xx to RCPT/DATA → `BOUNCED`, **no suppression added here** | Suppressing on permanent failures is the event pipeline's rule (§10.8.2, Q-EVT-3) |
| Policy HOLD → job stays put and is re-asked after `DELIVERY_HOLD_RETRY_SECONDS` (new setting, 300 s) | §10.7.2 "stays RENDERED with a next_attempt_at"; interval not specified |
| Handoff re-renders (deterministic, §10.2). If the recipient's own data changed since render, the new content is sent and its hash recorded | The template itself is pinned by checksum in policy check 5 |
| Envelope sender is VERP: `b-<job>-<hmac>@<return-path domain>`, signed with `SECRET_KEY` | Lets §10.8 map a bounce to exactly one job; a `SECRET_KEY` rotation orphans in-flight bounces (they go to the unknown-DSN queue) |
| Message-ID `<job.attempt@MESSAGE_ID_DOMAIN>`, falling back to the sending domain when unset (non-production only — production refuses to boot without it) | §10.2 organization-controlled domain |
| A snapshot recipient erased before expansion gets a `SUPPRESSED` job with code `CONTACT_ERASED` and no address | Every snapshot recipient gets exactly one job, so campaign completion can be counted |
| Campaign `COMPLETED` = one job per snapshot recipient and none open; a job awaiting review keeps the campaign `SENDING` | Completion must not hide a maybe-sent message |
| **Unsubscribe (§10.8.4) built now**, not in step 5; token has no timestamp (`Signer`, not `signing.dumps`) | Rendering needs a working `unsubscribe_url`; a timestamped token would change the content between render and handoff |
| Unsubscribe applies Q-EVT-4's default (all marketing): `Suppression(STREAM=MARKETING)` + a `WITHDRAWN` consent record; GET only confirms | Link scanners follow GETs |
| Verified end to end under Docker Compose: beat → expand → render → deliver workers → MailHog (200/200 accepted, campaign `COMPLETED`). Not yet against Postfix | Postfix/OpenDKIM are §10.10 |
| Headers are built with a 998-column policy, not the default 78 | A long `List-Unsubscribe` URL was otherwise RFC 2047-encoded on the wire, which providers do not decode — found in MailHog, now covered by a raw-bytes test |

### Event pipeline (§10.8) — interpretations

| Choice | Why |
| --- | --- |
| A 4xx reported **after** the MTA accepted the message is an event only; the job stays `ACCEPTED` | §10.8.2 says "Job DEFERRED", but our job states describe the app → MTA hand-off. Moving an accepted job back would make the app resend while Postfix is already retrying — a duplicate |
| A bounce after acceptance moves the job `ACCEPTED → BOUNCED` (new transition) | The message's real outcome |
| Hard bounce `5.1.x` (no such address) → **GLOBAL** suppression; any other 5xx → **stream** suppression (liftable) | Q-EVT-3 default is "suppress both"; but `5.7.x` is often a block on *our* reputation, and a GLOBAL row can never be lifted |
| Soft bounce (Postfix `expired`, DSN `failed` with 4.x) → job `BOUNCED`, counted, never suppressed | Q-EVT-1 default. `SOFT_BOUNCE_SUPPRESS_THRESHOLD` / `SOFT_BOUNCE_WINDOW_DAYS` are therefore **unused** until Q-EVT-1 is answered |
| Complaint → marketing suppression immediately; complaint/hard-bounce **rates** over `COMPLAINT_RATE_ALERT_THRESHOLD` / `HARD_BOUNCE_RATE_ALERT_THRESHOLD` show on Today and in the log — **never** an automatic pause | Q-EVT-2 default: alert only. There is no minimum sample, so a tiny campaign can alert on one complaint |
| **A bounce or complaint *email* only acts if it carries our signed VERP address** (or, for a complaint, one of our Message-IDs); otherwise it is UNKNOWN for operator review | Anyone can mail the bounce domain; a forged DSN naming a real customer must not suppress them. Postfix's own log and signed webhooks are trusted directly |
| Webhook signature: `X-Webhook-Signature` = hex HMAC-SHA256(`WEBHOOK_SIGNING_SECRET`, `"<timestamp>.<body>"`), `X-Webhook-Timestamp` within `WEBHOOK_REPLAY_WINDOW_S` (300 s); a replayed event id answers 200 "duplicate" | §12 "signature check, replay protection (timestamp + nonce window)": the event id is the nonce. The payload format is ours — real providers will each need a small adapter |
| Events store an email **hash** and recipient domain, never the address; diagnostics have the address replaced with `<recipient>` | `DeliveryEvent` is append-only, so it must not hold personal data an erasure request would need removed. Raw DSN/ARF/webhook bodies (which do contain addresses) are in `RawPayload`, which is never deleted but **can** be redacted. Retention is Q-LEGAL-1 |
| Unsubscribe-link clicks are also recorded as events (telemetry only; a failure there never blocks unsubscribing) | So Today and campaign stats count them |
| Per-campaign stats are recomputed wholesale every 5 minutes | §10.8.3 "periodic task"; recomputing avoids drift |
| No live Postfix yet: verified with recorded log lines, sample DSN/ARF messages and a signed webhook through the real Celery worker | Postfix is §10.10 |

### Transactional API (§10.6.6) — interpretations

| Choice | Why |
| --- | --- |
| API token only (scope `messages:send`); browser sessions are refused | A storefront or app calls this, not a person; a session cookie would make it CSRF-reachable |
| Template is named, and the **newest approved** version is used | Callers should not have to track version numbers; an unapproved draft can never go out |
| Everything checkable is checked on arrival (template, sender, address, data, a render dry-run) and refused with `422 {code, detail, field}` | The caller gets an answer it can fix, instead of a job that fails later in a worker |
| `idempotency_key` is required (body or `Idempotency-Key` header) and scoped to the token's **user**, so it survives token rotation | §5.2 / §10.7.1. Same key + same body → `200` with the original message; same key + different body → `409`, so a retry can never silently send something else |
| Suppressed recipients, paused domains/clients and `GLOBAL_SEND_ENABLED=false` still get `202`, with a `warnings` list | The policy service decides at hand-off, as for campaigns; refusing at the door would leak suppression status and diverge from campaign behaviour |
| Every suppression applies to transactional mail | Q-POL-2 default. Changing it is a one-line change in the policy service |
| Per-token rate limit `MESSAGES_API_RATE_PER_MINUTE`, **0 (off) by default**; `429` + `Retry-After` when set | Q-POL-1: no number has been agreed |
| Status (`GET /api/v1/messages/{id}`) is visible only to the user whose tokens sent the message; anyone else gets `404` | Message status reveals a recipient's delivery history |
| Merge data is stored on the job (≤ 64 KB) | Needed to render; it is personal data, so its retention falls under Q-LEGAL-1 |

### Rate limits and warm-up (§10.9 checks 6–7) — interpretations

| Choice | Why |
| --- | --- |
| Built as a mechanism only: `RateLimit` and `WarmupPlan` rows, added on the console's Controls page. **No numbers ship with the code**; with no rows nothing is limited and the console and scheduling warn | §10.9 and Q-POL-1: "no default numbers"; "do not start production traffic before this is answered" |
| Removed `SendingDomain.daily_cap` (default 50) and `warmup_started_at` | They were never enforced; a number shown in the admin that does nothing is misleading. A `WarmupPlan` replaces them |
| Scopes: whole instance ("tenant" — this is single-tenant), campaign, sending domain, mailbox provider, **IP pool** (not single IP), API client (credential). An empty target means "each one separately" | Postfix chooses the IP within a pool, so the app can only count per pool; per-IP limits belong in Postfix (§10.10) |
| A provider is a named set of recipient domains (e.g. gmail = gmail.com, googlemail.com) | There is no reliable way to infer the provider from an address; MX lookups at send time would add DNS to the hot path |
| Rate limits are a smooth sliding window in Redis, not a token bucket | Same effect for this purpose (no bursts at window edges) using only cache operations; works with any Django cache backend |
| Checks 6–7 run only after checks 1–5 pass, and allowance is taken from all applicable limits or none | A message held for a pause or suppressed must not use up a limit |
| A send that is then deferred or fails still counts against the limit | Errs on the side of sending less |
| Held jobs get their own slot (window ÷ max apart) instead of all retrying when the window reopens | Without this, 50 000 held jobs would each be re-decided — and logged — every window |
| Warm-up: day 1 is `started_on`; day *n* allows `daily_limits[n-1]` accepted messages; after the last day the plan is finished; before day 1 nothing goes out on that route. Days are in `TIME_ZONE` | §10.4 gives no ramp (Q-OPS-1); the operator enters it |
| A warm-up day's count is re-seeded from `SendAttempt` if Redis lost it | A Redis restart must not reset the day to zero |
| If limits are configured and Redis is unreachable, sends HOLD (`POLICY_ERROR`); with nothing configured Redis is not touched | Fail closed, without making Redis a dependency for instances that use no limits |
| Scheduling **refuses** a campaign whose audience is larger than what the all-recipient warm-up plans allow before jobs expire (`MESSAGE_MAX_AGE_HOURS`); provider-specific plans and rate limits only **warn** | §9 "schedule rejects when … warm-up controls fail". Rate limits only slow a send. Other campaigns sharing the domain are not counted |
| `GET /api/v1/messages/{id}` returns `held: {reasons, next_attempt_at}` while a message waits | A transactional caller needs to know whether a message is late because of a limit or a pause |

### Production pieces (§10.10, §13) — interpretations

| Choice | Why |
| --- | --- |
| MTA hosts run no application code. `forward-bounce` (Postfix pipe) and `ship-postfix-log` (systemd) POST to `/api/v1/events/mail` and `/api/v1/events/postfix-log`, HMAC-signed with **`MTA_INGEST_SECRET`** (distinct from the provider-webhook secret) | §10.8.1 suggested piping into `manage.py ingest_mail`, which needs Django and database access on the MTA. A leaked webhook secret must not let anyone inject bounces |
| A bounce the app cannot take makes Postfix retry (exit 75); the log shipper saves its journal cursor only after the app accepts a batch | Nothing is lost while the app is down; the app ignores repeats |
| Stream routing: the envelope-sender (return-path) domain picks the stream transport, which binds the stream's IP and HELO; any other sender is refused (`default_transport = error`) | §10.3: never send from whichever IP is handy. Each return-path domain must belong to one stream (`export_mta_maps` refuses otherwise) |
| OpenDKIM failure → mail waits (`milter_default_action = tempfail`) | Unsigned mail would fail DMARC |
| Per-provider limits in Postfix are per destination (`<stream>_destination_concurrency_limit` / `_rate_delay`), left unset until Q-POL-1 | Postfix's own per-destination scheduling is the natural place for them |
| `DKIM_KEY_DIR` is no longer required on app nodes (only checked for permissions if present) | OpenDKIM signs on the MTA; app nodes never read the keys and should not hold them |
| Probes and `/metrics` are served before host validation and the HTTPS redirect (`ProbeMiddleware`); `/metrics` needs `METRICS_TOKEN` and answers 404 otherwise | Load balancers and Prometheus address containers by IP over the private network |
| Production settings trust `X-Forwarded-Proto` from the reverse proxy | TLS ends at nginx; without it every request would redirect forever. The proxy must overwrite the header (`deploy/nginx.conf` does) |
| Backups are nightly logical dumps (`pg_dump -Fc`) with a weekly automated restore test that checks the append-only tables never lost rows; no point-in-time recovery yet | Up to a day of data can be lost. WAL archiving (pgBackRest / WAL-G) on the database host is the next step |
| Metrics are business state computed from the database at scrape time (one scrape a minute, ~12 queries) | Every web instance reports the same numbers and nothing resets on restart |

### Integration deviations in `email_templates`

| Deviation | Why |
| --- | --- |
| Integer primary keys, not UUIDv7 | The component ships that way and `RenderedMessage.template_version_id` is typed `int`. Changing it would fork the component. Every other table uses UUIDv7. |
| Runs on Django 5.1 (verified on 5.2) | Project pin is `<5.2`. All 41 component tests pass on 5.1.15 + Postgres 16. |
| Permissions come from platform roles via `accounts.backends.RolePermissionBackend`, not Django groups | One reviewable mapping; grants and revokes take effect immediately. Mapping follows §10.5.4. |
| `CONTENT_EDITOR` role is not in the spec | Kept from the earlier provisional role set; can author, cannot submit. Remove if the spec's roles are exhaustive. |
| Ruff formatting applied; two style fixes in `models.py` | No behaviour change; both test modes pass. |

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
- **Field encryption key — resolved in code.** `EncryptedCharField` now uses
  `FIELD_ENCRYPTION_KEYS` (comma-separated Fernet keys; first encrypts, all
  decrypt), independent of `SECRET_KEY`. Production refuses to boot without a
  valid key. Outside production an empty value still falls back to a
  `SECRET_KEY`-derived key for convenience. **Still open:** where the key is
  held (see the last bullet) and the rotation runbook — prepend the new key,
  re-save every encrypted row, then drop the old key.
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

`scripts/relay_test.py` exists but exits non-zero with an explanation — it
needs a running MTA (Phase 5). `scripts/seed_dev.py` and `scripts/validate_dns.py`
are now implemented. They depend on later phases. They
fail loudly by design — a missing open-relay test must never be mistaken for a
passing one.
