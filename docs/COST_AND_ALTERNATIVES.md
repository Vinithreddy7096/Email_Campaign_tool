# Cost, Paid Services, and What Can Be Eliminated

**Question this answers:** which parts of this platform actually require paying
someone, why, and what can be removed or replaced.

**Target volume:** 100,000 messages/day = ~3,000,000/month = 1.16 msg/sec sustained.

⚠️ **All prices are order-of-magnitude estimates as of the last update and must be
re-verified before any purchase.** Provider pricing and port-25 policies change.
Nothing here is a quote.

---

## 1. The short answer

| Category | Can it be eliminated? | Floor cost |
| --- | --- | --- |
| TLS certificates | ✅ Yes — Let's Encrypt | $0 |
| Monitoring stack | ✅ Yes — self-hosted Prometheus/Grafana/Loki | $0 |
| Secrets management | ✅ Yes — Ansible Vault or Vault OSS | $0 |
| Object storage | ✅ Mostly — MinIO on your own disk | $0 |
| Feedback loops / postmaster data | ✅ Yes — all free from providers | $0 |
| Error tracking | ✅ Yes — optional, or free tier | $0 |
| Broker (RabbitMQ) | ⚠️ Replaceable, but read §4.6 first | $0 |
| Domain registration | ❌ No — you must own the sending domain | ~$15/yr |
| Compute (app + DB + MTA) | ⚠️ Compressible to one VM | ~$5–50/mo |
| **Port 25 + dedicated IP + PTR** | ❌ **No — this is the real floor** | ~$5–20/mo |
| **Operational staff time** | ❌ **No. This is the actual cost.** | see §6 |

**Infrastructure floor for a genuine self-hosted platform: roughly $30–80/month.**

That number is almost irrelevant. See §6 — the money question is the wrong
question, and §7 explains why an ESP may still win on total cost.

---

## 2. The one thing you cannot eliminate

To deliver mail yourself, you must connect to recipient mail servers on **port 25**
from an IP address whose **PTR record you control**. There is no way around this:

- **Port 25 outbound is blocked by default** on AWS, Google Cloud, Azure, and
  DigitalOcean, and on essentially every consumer ISP. Some providers will unblock
  it on request with justification; some never will.
- **PTR (reverse DNS) must match your HELO hostname.** Most large receivers
  penalise or reject mail from IPs with generic or mismatched PTR. Only the IP's
  owner can set PTR — you cannot do it from your DNS panel.
- **Serverless and PaaS cannot host an MTA.** Vercel, Netlify, Cloud Run, Lambda,
  Heroku: none give you port 25, a static IP you own, or PTR control. This is why
  BUILD.md §2.1 rejects them for the MTA tier specifically.

**Providers that commonly permit port 25** (verify current policy, and get it in
writing per spec §13): Hetzner, OVH, Contabo, Scaleway, Vultr (on request),
Akamai/Linode (on request). Cost is typically $5–20/month for a small VPS.

If no acceptable provider will confirm this in writing, the self-hosted design is
dead on arrival and you should go to §7 (hybrid).

### Do you need a *dedicated* IP?

Yes, at this volume. Dedicated IPs need sustained, consistent traffic to build
reputation — a common rule of thumb is that below roughly 100k messages/month a
dedicated IP stays "cold" and performs worse than a well-managed shared pool. At
3M/month you are comfortably above that line, so a dedicated IP is the right call
and gives you reputation isolation you control.

⚠️ Do not buy *many* IPs. Spec Appendix C is explicit: too many low-volume IPs
dilute reputation and add complexity. One or two, segmented by stream, is right at
this scale.

---

## 3. Cost of the free path, in time

Two costs are unavoidable and are paid in weeks, not dollars:

- **IP/domain warm-up: 4–8 weeks.** You cannot send 100k on day one from a new IP.
  Volume ramps gradually while you watch provider responses. Budget for this in the
  project plan, not just the infrastructure plan.
- **Reputation recovery: unbounded.** If you damage a domain's reputation through
  a bad list or a complaint spike, there is no purchase that fixes it. Spec §10.3
  is blunt: never move traffic to new IPs to escape a poor reputation — fix the
  underlying list, consent, content, or rate problem.

---

## 4. Item by item

### 4.1 TLS certificates — eliminate entirely

**Why needed:** HTTPS for the app, API, unsubscribe endpoint, and tracking domain.
Opportunistic TLS on outbound SMTP.

**Alternative:** Let's Encrypt via certbot or Caddy. Free, automated renewal.
Outbound SMTP TLS uses the recipient's certificate — costs nothing.

**Verdict:** $0. Just monitor expiry (spec §10.1).

### 4.2 Monitoring and logs — eliminate entirely

**Why needed:** Spec §14 requires queue depth/age, provider error rates,
authentication trends, complaint/bounce rates, and host health. Without these you
cannot react to a deferral spike, which is the entire operational premise.

**Alternative:** Prometheus + Grafana + Loki + Alertmanager, all open source, all
already in `docker-compose.yml`. Runs on the same VM. Grafana Cloud has a free
tier if you prefer hosted.

**Verdict:** $0. Do not skip the capability — skip the invoice.

### 4.3 Secrets management — eliminate entirely

**Why needed:** DKIM private keys, database credentials, API secrets, webhook
signing secret. Spec §12 requires encryption at rest, access logging, rotation, and
exclusion from source control.

**Alternatives, cheapest first:**
- **Ansible Vault** — free, file-based, explicitly endorsed by spec §6 "encrypted
  Ansible secrets" and BUILD.md §2 "acceptable at small scale". Good enough here.
- **HashiCorp Vault OSS** — free, self-hosted, more operational overhead.
- Cloud secret manager — a few dollars/month, only if you're already in that cloud.

**Verdict:** $0 with Ansible Vault. See Q-SEC-1 in OPEN_QUESTIONS — the field
encryption key currently derives from `SECRET_KEY`, which needs fixing before MFA
secrets are stored regardless of which tool you pick.

### 4.4 Object storage — eliminate almost entirely

**Why needed:** Frozen audience snapshots at approval time, raw provider payloads
(kept for dispute resolution), consent evidence, exports.

**Alternatives:**
- **MinIO on your own disk** — free, already in the compose stack, S3-compatible so
  the application code never changes.
- **Cloudflare R2** — no egress fees, cheap storage; good if you want off-box
  durability.
- **Backblaze B2** — cheapest per GB.
- AWS S3 — most expensive of these, mainly worth it if already on AWS.

**Volume reality:** raw event payloads at 3M messages/month are on the order of a
few GB/month. This is a rounding error whichever you choose.

**Verdict:** $0 self-hosted, or a few dollars/month for off-box durability. Note
that keeping backups on the same disk as the primary is not a backup.

### 4.5 Feedback loops, postmaster data, blocklist checks — all free

This surprises people. The deliverability data that actually matters costs nothing:

| Source | Cost | What it gives you |
| --- | --- | --- |
| Google Postmaster Tools | Free | Domain/IP reputation, spam rate, authentication rates |
| Microsoft SNDS + JMRP | Free | Complaint feedback and IP data for Outlook/Hotmail |
| Yahoo Complaint Feedback Loop | Free | Complaint reports |
| DNSBL lookups (Spamhaus etc.) | Free at low query volume | Blocklist status |
| DMARC aggregate reports | Free — you self-parse them | Authentication failures, spoofing |

**Paid and genuinely optional:** inbox-placement testing (GlockApps, mail-tester
beyond the free tier), managed DMARC platforms (Dmarcian, Valimail), and sender
certification programmes. All are conveniences. Spec Appendix C warns that public
testing tools are validation aids only — production decisions must rest on real
provider responses and complaint data, which you get free.

**Verdict:** $0. Register for the free programmes; they are not optional
operationally even though they're free.

### 4.6 Broker — replaceable, but understand the trade

**Why RabbitMQ:** BUILD.md §2.1 chose it over Redis specifically for per-message
acknowledgement semantics and dead-letter exchanges, which are what prevent silent
message loss. "No accepted message is silently lost" is spec §19 acceptance
criteria — a contractual requirement, not a preference.

**Alternative:** Redis Streams. Spec §6 calls it "acceptable for a smaller system."
It would remove one component from the stack.

**Cost impact: zero either way.** Both are free and both run on the same VM. You
would be trading a documented acceptance criterion for a marginally simpler
deployment. **Not recommended** — there's no money saved.

**Verdict:** keep RabbitMQ. This is not a cost decision.

### 4.7 Database — compressible

**Why needed:** Everything. Contacts, consent, suppression, campaign state, event
log, audit trail.

**Alternatives:**
- **Self-managed PostgreSQL on your VM** — free. Needs you to configure backups,
  WAL archiving, and *test the restore* (spec §19 requires a tested restore).
- **Managed Postgres** — typically $15–50/month at this size. You're buying
  point-in-time recovery, automated backups, and failover you don't have to build.

**Volume reality:** 3M messages/month with an append-only partitioned event table
is modest — tens of GB/year. Not a scaling problem; a durability problem.

**Verdict:** $0 self-managed, but only if you genuinely run and test restores.
Managed Postgres is the single best-value paid upgrade in this whole document,
because the failure it prevents (losing your suppression list) is unrecoverable.

### 4.8 Compute — compressible to one machine

**Why needed:** Web/API, five Celery worker pools, beat scheduler, Postgres,
RabbitMQ, Redis, monitoring — plus the separate MTA node.

**Reality check:** 1.16 msg/sec is *nothing*. The engineering difficulty in this
system is rate governance and event normalization, not throughput. A single 4–8GB
VM runs the entire application tier comfortably. Spec §13 permits this explicitly:
"Start simpler if volume and SLA permit."

**Minimum viable topology:**
- 1 × app VM (4–8GB) running everything except the MTA — ~$10–30/month
- 1 × MTA VM (2GB, port 25, dedicated IP, PTR) — ~$5–20/month

Keep the MTA separate. That separation is a security and reputation boundary, not
a capacity one — the app tier should never talk to the public internet on port 25.

**Verdict:** ~$15–50/month total. Add a second app VM only when your SLA demands it.

### 4.9 Domains — irreducible but trivial

You must own the domain you send from. Sending, bounce, and tracking identities are
all **subdomains** of a domain you already own, so they cost nothing extra.

**Verdict:** ~$15/year. Do not skimp by sending from a shared or free domain — you
need DNS control for SPF/DKIM/DMARC, and reputation attaches to the domain.

---

## 5. Three viable architectures

### Option A — Full self-host (BUILD.md default)

Everything above, both VMs, direct-to-MX delivery.

- **Cost:** ~$30–80/month infrastructure
- **You own:** IP reputation, blocklist remediation, warm-up, port 25 relationship
- **Best when:** volume is stable, someone owns operations, and vendor independence
  or data residency genuinely matters

### Option B — Hybrid (spec Appendix C endorses this)

Self-host the management application, consent records, templates, suppression, and
analytics. Use an ESP purely as the **delivery transport** — Postfix relays to their
SMTP endpoint instead of direct-to-MX.

- **Eliminates:** port 25 sourcing, dedicated IP, PTR control, IP warm-up,
  blocklist remediation, MTA queue operations
- **Keeps:** your consent system, your suppression list, your audit trail, your data
- **Adds:** per-message cost, vendor dependency
- **Critical property:** because you keep the consent and suppression layer, you can
  switch transports — or move to full self-host later — without re-platforming.
  The `MTA_SMTP_HOST` setting is already the only thing that changes.

Spec Appendix C: *"A hybrid design may be economically safer."* This is the
lowest-risk path to a working system.

### Option C — Minimum viable self-host

Single app VM, self-managed Postgres, MinIO on local disk, Ansible Vault, one MTA
VM. ~$20–40/month. Acceptable for a pilot; not acceptable as a permanent posture
without tested backups and a second pair of hands.

---

## 6. The cost that actually decides this

**Infrastructure is not the expense. Operations is.**

Spec §18 lists this as a named project risk: *"Operational complexity exceeds
savings — higher total cost than an ESP."*

Recurring work that does not go away:

| Cadence | Work |
| --- | --- |
| Daily | Queue depth/age review, deferral and bounce review, alert response |
| Weekly | Deliverability review, complaint rate trends, capacity check |
| Monthly | OS/dependency patching, restore test, access review |
| Periodic | DKIM key rotation, DR exercise, blocklist remediation, provider policy changes |
| On incident | 2am pages when a major provider starts deferring |

Estimate this honestly at a few hours per week in steady state, plus incident
spikes. At any realistic loaded engineering rate, **staff time exceeds
infrastructure cost by one to two orders of magnitude.**

This is why spec §20 requires a *named operational owner* before launch. If you
cannot name that person, self-hosting is not viable regardless of budget.

---

## 7. The break-even nobody runs

Compare honestly at 3M messages/month:

| Path | Monthly |
| --- | --- |
| Self-hosted infrastructure | ~$30–80 |
| Amazon SES (~$0.10 per 1,000) | ~$300 + transfer |
| Mid-market ESP (~$0.50–1.00 per 1,000) | ~$1,500–3,000 |

The delta between self-hosting and **SES specifically** is roughly $250/month.
That is on the order of **two to three hours of engineer time**. If self-hosting
costs you more than three hours a month — and §6 says it will — then measured
purely on cost, SES wins at this volume.

The delta against a **full-featured ESP** is $1,500–3,000/month, which does
comfortably justify self-hosting on cost alone.

**So the honest framing is:** self-hosting at 100k/day is justified by *control,
data ownership, vendor independence, and per-message economics against expensive
ESPs* — not by beating commodity SMTP relay pricing. Decide on those grounds. If
the driver is purely "reduce the ESP bill" and your alternative is SES, run this
arithmetic with your real numbers before committing.

---

## 8. Hidden costs people miss

- **Warm-up is 4–8 weeks of reduced sending.** Plan the launch around it.
- **A blocklisting costs days, not dollars.** Delisting is a process, not a purchase.
- **Backups are worthless until a restore is tested.** Losing the suppression list
  is unrecoverable — you cannot reconstruct who unsubscribed, and sending to them
  again is both a compliance breach and a reputation event.
- **Someone must be reachable.** Provider deferrals do not wait for business hours.
- **Compliance review is not free** even though it isn't infrastructure. Q-LEGAL-1.
- **The `List-Unsubscribe-Post` endpoint must stay up.** Providers POST to it with
  no user interaction; if it fails, complaint rates rise.

---

## 9. What to do next

**Free, do now — unblocks everything and commits you to nothing:**

1. Get written confirmation from a candidate host: bulk email permitted, port 25
   open, dedicated IP, PTR control, abuse process. Spec §13 — *before* purchase.
2. Register for Google Postmaster Tools, Microsoft SNDS/JMRP, and Yahoo CFL.
3. Decide Option A / B / C above.
4. Name the operational owner (spec §20).
5. Get compliance sign-off on consent rules (Q-LEGAL-1).

**Then, and only then, spend:** domain (~$15/yr), MTA VM, app VM. Roughly
$30–80/month total.

**Recommendation:** if this is your first self-hosted sending platform, start at
**Option B (hybrid)**. You build and validate the entire consent, suppression,
approval, and audit system — which is the genuinely hard and valuable part — while
someone else carries IP reputation risk during your first months. Because the
application owns suppression and consent, moving to Option A later changes one
setting, not the architecture.

---

## 10. Open items this document depends on

See [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md): **Q-HOST-1** (hosting confirmation),
**Q-OPS-1** (warm-up audience and provider volume estimates), **Q-LEGAL-1**
(compliance sign-off), **Q-SEC-1** (secrets strategy).
