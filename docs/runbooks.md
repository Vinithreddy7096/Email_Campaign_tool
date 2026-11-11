# Runbooks

One section per alert in `monitoring/prometheus/rules/mailer.rules.yml` (each
alert's `runbook` annotation links here), plus the routine procedures. Each
section: what it means, how to confirm, what to do. Commands assume the app
node layout in `deploy/docker-compose.prod.yml`:

```sh
DC="docker compose -f /opt/email-platform/deploy/docker-compose.prod.yml"
$DC run --rm web python manage.py shell      # a Django shell against production
```

**First rule:** when in doubt, pause. Console → Sending controls → *Pause all
sending*. Nothing queued is lost; resuming releases it. Messages the MTA already
accepted still go out.

---

## Pause everything

*Alert: SendingPausedEverywhere (ticket) — paused for over an hour.*

- To pause: console → Sending controls → *Pause all sending*, with a reason (it
  goes into the audit log). Narrower pauses — one stream, domain, IP pool,
  campaign or API client — are on the same page.
- To resume: same page, *Resume sending*.
- Held mail expires after `MESSAGE_MAX_AGE_HOURS` (default 72 h). A pause longer
  than that means those messages are never sent; tell the senders.
- `GLOBAL_SEND_ENABLED=false` in the environment is a harder stop (no worker
  hands anything to the MTA). Changing it is a deploy, not a click.

## App down

*Alert: MailerMetricsDown (page).*

1. `curl -fsS http://<app-node>:8000/health/live` — no answer: the web container
   is down. `$DC ps`, `$DC logs --tail 100 web`.
2. Live but `/metrics` 404: `METRICS_TOKEN` in `/etc/email-platform/app.env` and
   Prometheus's `/etc/prometheus/secrets/metrics_token` differ.
3. `/health/ready` reports which dependency (database, cache, broker) is failing.
4. A boot-guard failure prints `Refusing to boot in 'production'` with the
   list of what is missing — fix the env file, `$DC up -d`.

## Handoff backlog

*Alerts: HandoffBacklog (page), RenderBacklog, QueueBacklog (ticket),
QueueHasNoConsumer (page).*

Mail is due but not reaching the MTA.

1. Dashboard "Email platform — pipeline": which state is growing?
   - `QUEUED` growing → render workers. `RENDERED` growing → deliver workers or the MTA.
2. `$DC ps` — are `worker-render` / `worker-deliver` up? `QueueHasNoConsumer`
   names the queue with nobody reading it. `$DC up -d worker-deliver`.
3. Workers up but slow: `$DC logs --tail 200 worker-deliver`. Timeouts → the MTA
   (see [MTA deferring](#mta-deferring)). Policy holds are normal while a pause,
   rate limit or warm-up plan applies — check console → Sending controls.
4. Scale: raise `DELIVER_CONCURRENCY` / `RENDER_CONCURRENCY` or add an app node.

## Beat stopped

*Alert: PeriodicTaskStopped (page).*

`delivery.sweep` (every minute) retries deferred mail, re-asks held jobs, flags
stuck submissions and completes campaigns. `events.recompute_stats` (every
5 minutes) updates campaign counters and rate alerts.

1. `$DC ps beat` — exactly **one** beat must run in the whole deployment.
2. `$DC logs --tail 50 beat`; restart with `$DC up -d beat`.
3. Beat up but the task never runs: the `maintenance`/`deliver` worker that
   executes it is down — see [Handoff backlog](#handoff-backlog).

## Jobs need review

*Alert: JobsNeedReview (ticket).*

A job is flagged when the SMTP reply was lost after DATA (the message may or
may not have been sent — Q-DEL-1: never resent automatically), when a worker
died mid-submission, or when rendering failed.

Console → the campaign → jobs needing review. Choose *mark sent*, *resend* or
*cancel* per job. If many appear at once, look for a cause first (MTA restart,
network blip, a template change) before resolving them in bulk.

## MTA deferring

*Alert: SubmissionDeferralsHigh (page).*

The app → MTA hand-off is getting 4xx or timeouts.

1. On the MTA: `systemctl status postfix opendkim`, `journalctl -u postfix -n 200`.
2. `milter ... service unavailable` → OpenDKIM is down. Postfix tempfails on
   purpose (`milter_default_action = tempfail`) so nothing leaves unsigned.
   `systemctl restart opendkim`; check `/etc/opendkim/keys` ownership (opendkim, 0600).
3. Disk full on the MTA (`df -h /var/spool/postfix`) → Postfix refuses mail.
4. Connection refused → Postfix down, or `postfix_app_networks` no longer
   includes the app nodes' network (after a re-IP). Fix the inventory, re-run
   `playbooks/mta.yml`.

## Complaint spike

*Alerts: ComplaintRateOverThreshold (page), HardBounceRateOverThreshold (ticket).*

Nothing pauses automatically (Q-EVT-2: alert only). Decide now:

1. Console → Bounces & complaints, and the campaign's page: which campaign,
   which provider (recipient domain)?
2. If one campaign: pause it (campaign page → Pause). If one provider is
   complaining or blocking: pause that domain or the stream.
3. Complainers are already suppressed for marketing. Before resuming, check the
   list's source and consent (a purchased or stale list is the usual cause).
4. Hard bounces over threshold usually mean an old list — the bounced addresses
   are suppressed already; review the list before the next send.

## Unrecognised bounces

*Alert: UnrecognisedBouncesWaiting (ticket).*

A bounce or complaint email arrived that carries no signed VERP address or
known Message-ID. It is **not** acted on automatically (a forged bounce must not
suppress a real customer). Console → Bounces & complaints → *Needs review*:
classify as hard bounce, soft bounce, complaint, or ignore.

## Domain paused

*Alert: SendingDomainPaused (page).*

The DNS checker found SPF, DKIM or DMARC broken for an ACTIVE domain and paused
it; its mail is held.

1. Console → Domains: the failing record and what was found.
2. `python scripts/validate_dns.py --domain <domain>` from any machine.
3. Fix the DNS record; after it propagates, reactivate the domain in the admin.

## Rate limits

*Alert: SendingWithoutRateLimits (ticket).*

Sending is enabled with no rate limits configured. The numbers are Q-POL-1 and
must be decided before production traffic. Console → Sending controls → Rate
limits / Warm-up. Every change is audited; limits apply from the next hand-off.

## IP blocklisted

*Alert: PostfixDeferredQueueLarge (ticket); often with complaint or deferral alerts.*

1. On the MTA: `postqueue -p | tail`, `qshape deferred` — which destinations?
2. Read the deferral reasons (`postcat -q <id>` / the log): "blocked",
   "listed", "rate limited", a URL to a provider's postmaster page.
3. Check the stream's IP on public blocklists (Spamhaus, Barracuda,
   Microsoft SNDS, Google Postmaster Tools).
4. Pause the affected stream or campaign. **Do not** move mail to another IP to
   get around a block (spec §10.3) — fix the cause, then follow the
   provider's delisting process.
5. Expired mail returns as bounces and is recorded as soft bounces.

## RabbitMQ alarm

*Alert: RabbitMQResourceAlarm (page).*

RabbitMQ blocks publishers when it is short of memory or disk, so the app
cannot enqueue work.

1. `rabbitmq-diagnostics status` / management UI: memory or disk?
2. A large backlog is usually the cause — see [Handoff backlog](#handoff-backlog);
   draining it clears the alarm.
3. Disk: free space on the RabbitMQ volume. Memory: raise the host's memory or
   `vm_memory_high_watermark`.

## Redis down

*Alert: RedisDown (page).*

Redis holds rate-limit and warm-up counters and the cache. With any rate limit
or warm-up plan configured, every send **holds** while Redis is down (fail
closed). Restart Redis; warm-up counters are re-seeded from the database
automatically, rate-limit windows simply start fresh.

## Restore

*Alerts: PostgresDown (page), BackupMissing / BackupStale (page),
RestoreTestStale (ticket).*

Backups: `scripts/backup/pgbackup.py`, run nightly by
`deploy/backup/email-platform-backup.timer`; a weekly restore test
(`email-platform-restore-check.timer`) restores the newest dump into a scratch
database and checks the append-only tables.

**Backup stale or missing:** `systemctl status email-platform-backup.service`,
`journalctl -u email-platform-backup -n 50`. Usual causes: expired bucket
credentials, database password changed, disk full in the container's temp dir.
Run it now: `systemctl start email-platform-backup`.

**Restoring production** (after data loss — not for a database that is merely down):

1. Pause all sending; stop workers and beat: `$DC stop worker-expand worker-render worker-deliver worker-events worker-maintenance beat`.
2. Pick the dump: newest is `postgres/production/YYYY/MM/DD/mailer-<ts>.dump`
   in the bucket. Check its manifest's `sha256`.
3. Restore into a **new** database, never over the damaged one:
   ```sh
   createdb -h <db> -U postgres mailer_restored
   pg_restore -h <db> -U postgres --no-owner --role=mailer --exit-on-error -d mailer_restored mailer-<ts>.dump
   ```
4. Point `DATABASE_URL` at `mailer_restored`, `$DC run --rm migrate`, `$DC up -d`.
5. Anything sent after the dump was taken is unknown to the restored database.
   Jobs from that window may be re-attempted; check `delivery_message` against
   the MTA logs for that window before resuming sending.
6. Resume sending once checked. Record the incident in the audit log.

A nightly logical dump loses up to a day of data. For less, add continuous WAL
archiving (pgBackRest or WAL-G to the same bucket) on the database host — not
built yet.

## Rotate a DKIM key

Manual (Q-MTA-1 default).

1. `opendkim-genkey -b 2048 -d <domain> -s <new-selector>` on a trusted machine.
2. Publish `<new-selector>._domainkey.<domain>` TXT from the `.txt` file. Wait
   for it to resolve.
3. Add the private key to the vault (`vault_dkim_private_keys.<domain>.<new-selector>`).
4. Set the domain's DKIM selector to the new one in the admin.
5. Re-run `playbooks/mta.yml` (it refuses to run if a key is missing, and checks
   each key against DNS).
6. Keep the old DNS record for a week (mail in flight), then remove it and the
   old key.
