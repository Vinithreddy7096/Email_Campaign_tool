**Email SchedulerOperations console**

Overview [Today](#overview)

Sending [Campaigns 7](#campaigns) [Templates 12](#templates) [Approvals 2](#review) [Contacts and consent](#audience)

Deliverability [Domains and senders](#domains) [Bounces and complaints 3](#events) [Suppression list](#suppressions) [Sending controls](#controls)

Governance [Audit log](#audit) [Unsubscribe page](#unsubscribe)

Viewing as

All sending running ·Production · acme-mail Mockup · example data

# Today

Tue 14 Oct 2026, 00:00 to now (UTC). Business and reputation telemetry from spec §14.

backend planned · §10.8.3

Scheduled84,2104 campaigns + API

Accepted61,90498.6% of submitted

Deferred (4xx)712Postfix retrying

Hard bounced2030.32% · suppressed

Complaints190.031% · suppressed

Unsubscribed141median 4 s to honor

## Needs attention

4 items

**news.acme.example: DKIM selector es2026b not found**

DNS checker 23:40 UTC. Domain held; 1 scheduled campaign blocked.

[Open domain](#domains)

**No rate limits configured**

Policy checks 6 and 7 apply nothing until limits are set. Q-POL-1

[Review](#controls)

**3 delivery reports could not be classified**

Stored raw; no suppression applied until reviewed.

[Review](#events)

**2 template versions waiting for approval**

October newsletter v4, Password reset v2

[Open](#review)

## Queues

spec §14 Queue

App queue depth

4,118 jobs

Oldest job

2 m 40 s

Held by policy

1,206 (domain not active)

MTA queue (active)

388

MTA queue (deferred)

712

Held for review

2 (lost SMTP reply after DATA) Q-DEL-1

## Campaigns in flight

[All campaigns](#campaigns)

| Campaign | Status | Domain | Audience | Progress | Accepted | Complaints |
| --- | --- | --- | --- | --- | --- | --- |
| October newsletter | Sending | mail.acme.example | 48,920 |  | 35,118 | 11 |
| Webinar reminder, 15 Oct | Scheduled 09:00 | mail.acme.example | 6,402 |  | 0 | 0 |
| Product digest, EU | Held · domain | news.acme.example | 1,206 |  | 0 | 0 |
| Transactional API | Running | notify.acme.example | - | continuous | 26,786 | 8 |

# Campaigns

Creating a campaign never sends. It sends only after preflight passes, an approver signs off, and it is scheduled on an active domain.

backend planned · §10.6[New campaign](#campaign)

| Name | Status | Template | Sender | Eligible | Suppressed | Schedule | Owner |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Black Friday early access | Ready for review | BF early access v2 | offers@mail.acme.example | 52,310 | 1,874 | not set | Priya N. |
| October newsletter | Sending | Newsletter v3 | news@mail.acme.example | 48,920 | 2,102 | 14 Oct 06:00 | Priya N. |
| Webinar reminder, 15 Oct | Scheduled | Event reminder v5 | events@mail.acme.example | 6,402 | 88 | 15 Oct 09:00 | Sam O. |
| Product digest, EU | Held · domain | Digest v7 | digest@news.acme.example | 1,206 | 41 | 14 Oct 00:30 | Sam O. |
| Re-engagement test | Preflight failed | Winback v1 | hello@acme.example | 3,000 | - | not set | Lee W. |
| September newsletter | Completed | Newsletter v2 | news@mail.acme.example | 47,655 | 1,990 | 16 Sep 06:00 | Priya N. |
| Spring sale | Canceled | Spring v4 | offers@mail.acme.example | - | - | canceled 2 Apr | Lee W. |

[Campaigns](#campaigns) / Black Friday early access

# Black Friday early access

Marketing · owner Priya N. · draft created 12 Oct 2026

backend planned · §10.6Ready for review

1. Sender
2. Audience
3. Template
4. Preflight
5. Approval
6. Schedule
7. Sending

## Setup

Sender identity

Acme Offers \<offers@mail.acme.example> · reply-to support@acme.example

Sending domain

mail.acme.example Active

Audience

Segment “Opted-in shoppers, last 12 months” · 54,184 members

Template

BF early access **v2** Approved `sha256:9f3c…a11e`

Purpose

Marketing (unsubscribe and footer required)

## Campaign preflight

0 fail · 1 warn

PASS

**Sender alignment**

From domain is mail.acme.example; SPF, DKIM (es2026a) and DMARC alignment pass.

PASS

**Template preflight**

Re-run on v2: unsubscribe link in HTML and text, no empty links, 38 KB HTML.

PASS

**Consent**

Every marketing recipient has an active consent record (source, timestamp, system).

WARN

**1,874 recipients excluded by suppression**

1,201 unsubscribed, 512 hard bounce, 161 complaint. Excluded, not failed.

PASS

**Sample renders**

5 of 5 real recipients rendered without missing variables. [View samples](#review)

## Audience breakdown

at snapshot time

Segment members

54,184

Suppressed

1,874

No consent

0

Eligible

52,310

## Approval

Approver / Compliance only

Approving freezes an immutable snapshot: template v2 and its checksum, sender values, the 52,310 contact ids with their consent record ids, and this preflight report.

Note (required to reject or request changes)

## Schedule

Start (UTC)

Schedule is refused if the domain, authentication, consent or warm-up checks fail at the time you schedule. Approval comes first.

Warm-up limits for mail.acme.example are not configured, so nothing caps this send. Q-POL-1

Needs approval

## While sending

Pause stops future dispatch. Messages the mail server already accepted still complete.

# Templates

Each change is a new version. Only approved versions can be sent; a running campaign keeps the exact version it was approved with.

backend built · §10.5[New template](#editor)

| Template | Category | Live version | In progress | Owner | Updated |
| --- | --- | --- | --- | --- | --- |
| **October newsletter** Monthly roundup | Marketing | v3 Approved | v4 Pending approval | Priya N. | 13 Oct 18:02 |
| **Password reset** Sent by API | Transactional | v1 Approved | v2 Pending approval | Dev team | 13 Oct 11:40 |
| **BF early access** | Marketing | v2 Approved | - | Priya N. | 12 Oct 09:15 |
| **Winback** | Marketing | none | v1 Changes requested | Lee W. | 10 Oct 16:20 |
| **Order receipt** | Transactional | v6 Approved | v7 Draft | Dev team | 9 Oct 14:03 |
| **Spring sale** | Marketing | v4 Retired | - | Lee W. | 2 Apr 10:00 |

[Templates](#templates) / Order receipt

# Order receipt · v7

Transactional · draft by Dev team · copied from v6

backend built · §10.5Draft

1. Draft
2. Pending approval
3. Approved
4. Retired

## Content

Editable while Draft; frozen once submitted

Subject HTML body Plain-text body

Variables

| Name | Required | Default |
| --- | --- | --- |
| first_name | yes | - |
| order_id | yes | - |
| total | yes | - |
| tracking_url | no | - |

Sample data (JSON)

Allowed: `if` `for` `with` `firstof` `cycle` `spaceless` `comment`. No includes, no `|safe`, nothing that reads the clock or database.

## Preflight

1 fail · 1 warn

## Submit

Submitting runs preflight again and freezes this version. Any FAIL blocks it.

[Preview](#review)

[Templates](#templates) / October newsletter

# Review October newsletter · v4

Marketing · submitted by Priya N. 13 Oct 18:02 · checksum `sha256:4b0e…72d9`

backend built · §10.5.4Pending approval

1. Draft
2. Pending approval
3. Approved
4. Retired

## Preview with sample data

From

Acme News \<news@mail.acme.example>

Subject

**Dana, your October picks are here**

Acme · October

#### Hi Dana, here’s what’s new

Three things we shipped this month, picked for people who bought trail gear in the last year.

Our autumn layering guide covers base layers through shells, with sizes for every fit.

[Read the guide](#review)

Image: autumn-hero.jpg has no alt text

You get this because you opted in at acme.example on 3 Mar 2025.\
[Unsubscribe](#unsubscribe) from Acme marketing email.

```
Hi Dana, here's what's new

Three things we shipped this month, picked for people who
bought trail gear in the last year.

Read the guide: https://acme.example/guides/autumn-layers

--
You get this because you opted in at acme.example on 3 Mar 2025.
Unsubscribe: https://mail.acme.example/u/eyJjIjo0ND…
```

Rendered in a sandboxed frame: no scripts, no same-origin access.

## Preflight

0 fail · 2 warn

WARN

`IMG_MISSING_ALT`

\<img src="autumn-hero.jpg"> has no alt attribute.

WARN

`UNDECLARED_VARIABLE`

Sample data key “segment_name” is not declared.

PASS

Unsubscribe link in HTML and text, required footer, no empty or unsafe links, 41 KB HTML, subject has no line breaks.

## Changes from v3

Subject

changed

HTML

+18 / −11 lines

Text

+6 / −4 lines

Variables

unchanged

## Decision

needs approve permission

Note (required to reject or request changes)

Preflight runs again on approval. Approve is disabled while any check fails.

# Contacts and consent

Every marketing recipient needs a recorded basis or opt-in source, timestamp and source system. Imports are checked before anything is saved.

backend planned

## Import check · webinar-signups-oct.csv

Review before saving

Rows6,544

Valid and consented6,402

Bad syntax31

Duplicates47

Missing consent fields22

On suppression list42

| Row | Email | Problem |
| --- | --- | --- |
| 118 | jordan@@example.org | Bad syntax |
| 904 | m.chen@example.com | No consent source column `opt_in_source` empty |
| 2210 | ops@example.net | Suppressed · complaint will be skipped |

## Lists and segments

| Name | Type | Members | With consent | Refreshed | Owner |
| --- | --- | --- | --- | --- | --- |
| Opted-in shoppers, last 12 months | Segment | 54,184 | 54,184 | daily 02:00 | Priya N. |
| Newsletter subscribers | List | 51,022 | 51,022 | static | Priya N. |
| Webinar registrants Oct | List | 6,490 | 6,402 | import 13 Oct | Sam O. |

# Domains and senders

A domain can send only while it is Active: verified, authenticated and not paused. The DNS checker runs every 20 minutes.

DNS checker built (your machine)

| Domain | Status | SPF | DKIM | DMARC | Return-path | PTR | Warm-up |
| --- | --- | --- | --- | --- | --- | --- | --- |
| mail.acme.example | Active | pass | es2026a | p=quarantine | bounce.mail… | ok | complete |
| notify.acme.example | Active | pass | es2026a | p=reject | bounce.notify… | ok | complete |
| news.acme.example | Held | pass | missing | p=none | bounce.news… | ok | day 6 Q-POL-1 |
| promo.acme.example | Pending verification | - | - | - | - | - | not started |

## news.acme.example · DNS records

**SPF**

TXT news.acme.example\
**v=spf1 ip4:203.0.113.20 -all**

pass

**DKIM**

TXT es2026b.\_domainkey.news.acme.example\
expected **v=DKIM1; k=rsa; p=MIIBIjAN…** (2048-bit)\
found: NXDOMAIN

missing

**DMARC**

TXT \_dmarc.news.acme.example\
**v=DMARC1; p=none; rua=mailto:dmarc@acme.example**

p=none

**Return-path**

MX bounce.news.acme.example\
**10 mx-bounce.acme.example**

ok

**PTR**

203.0.113.20 → **out2.acme.example** (matches HELO)

ok

## Sender identities

| From | Use | Owner |
| --- | --- | --- |
| Acme News news@mail.acme.example | Marketing | Priya N. |
| Acme Offers offers@mail.acme.example | Marketing | Priya N. |
| Acme no-reply@notify.acme.example | Transactional | Dev team |
| Acme Digest digest@news.acme.example | Marketing | Sam O. |

# Bounces and complaints

Delivery logs, bounce messages, feedback loops and unsubscribes become one event stream. Hard bounces, complaints and unsubscribes suppress immediately.

backend planned · §10.8

## By recipient provider · today

spec §14 Provider

| Provider | Submitted | Accepted | 4xx | 5xx | Complaints | TLS | Top code |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gmail.com | 28,410 | 99.1% | 0.6% | 0.3% | 0.02% | 100% | 4.7.28 |
| outlook.com / hotmail.com | 17,902 | 97.4% | 2.1% | 0.5% | 0.05% | 100% | 4.7.650 |
| yahoo.com | 8,115 | 98.8% | 0.9% | 0.3% | 0.04% | 100% | 4.7.0 |
| other | 8,341 | 98.2% | 1.0% | 0.8% | 0.01% | 93% | 5.1.1 |

## Unclassified reports · 3

no suppression until reviewed

| Received | Recipient | Raw status |  |
| --- | --- | --- | --- |
| 23:58 | k.ito@example.jp | 550 Message rejected by policy |  |
| 23:41 | info@example.de | Delivery Status Notification (no code) |  |
| 22:19 | a.r@example.com | 552-5.2.2 mailbox full |  |

Policy rejections and invalid recipients both suppress today. Q-EVT-3 Soft bounces are counted, never auto-suppressed. Q-EVT-1

## Latest events

| Time | Type | Code |
| --- | --- | --- |
| 00:14:52 | Accepted | 250 2.0.0 |
| 00:14:51 | Deferred | 421 4.7.0 |
| 00:14:47 | Hard bounce | 550 5.1.1 |
| 00:14:40 | Complaint | FBL · ARF |
| 00:14:33 | Unsubscribe | one-click POST |

# Suppression list

A suppressed address gets no marketing email, whatever list it is on. Transactional mail is suppressed too until exceptions are reviewed. Q-POL-2

backend planned · §10.9

Total18,342

Unsubscribe11,906

Hard bounce5,118

Complaint1,071

Manual / domain247

| Address or domain | Reason | Scope | Source | Added | Expires |
| --- | --- | --- | --- | --- | --- |
| ops@example.net | Complaint | Marketing | FBL, Oct newsletter | 00:14 | never |
| old.user@example.com | Hard bounce | All | 550 5.1.1 | 00:14 | never |
| dana.k@example.org | Unsubscribe | Marketing | one-click, Oct newsletter | 00:14 | never |
| \*@competitor.example | Manual | All | Admin, legal request | 2 Oct | never |
| \*@example-school.edu | Temporary | Marketing | Operator, blocklist | 10 Oct | 24 Oct |

## Add a suppression

Address or domain

Scope Expires

Reason (kept in the audit log)

Removing a suppression needs an administrator and is logged with before and after values.

# Sending controls

Emergency pause holds new handoffs to the mail server immediately. Messages it already accepted still complete.

backend planned · §10.9

## All sending

**Pause every campaign, API client and route?**

4,118 queued jobs will be held. Nothing is lost; resume releases them in order.

Running. Last paused 2 Sep 2026 by Alex R. for 14 minutes.

## Pauses

campaign, domain, client, pool, route

**Domain news.acme.example**Held automatically: DKIM missing

Held

**API client “storefront”**Transactional, 26,786 today

**IP pool marketing**203.0.113.10, .11

**Route outlook.com**Postfix transport “outlook”

## Rate limits

**No limits are configured.** Tenant, campaign, domain, provider, IP and credential limits all apply nothing until someone sets them. Do not start production traffic before this is decided. Q-POL-1

| Applies to | Per hour | Per day |
| --- | --- | --- |
| No limits yet. |  |  |

## Open decisions that affect sending

from BUILD.md §10.12; each has a safe default

| ID | Question | Default until answered |
| --- | --- | --- |
| Q-POL-1 | Rate limits and warm-up volumes | No limits applied; console warns |
| Q-EVT-2 | Complaint-rate alert and auto-pause thresholds | Alert only, no auto-pause |
| Q-CMP-1 | What counts as high volume for mandatory approval | All marketing campaigns need approval |
| Q-DEL-1 | Lost SMTP reply after DATA: hold or resend | Hold for review |
| Q-TPL-1 | What a marketing footer must contain | Only the unsubscribe link is required |

# Audit log

Material changes to templates, audiences, DNS, mail server settings, suppression and permissions. Append-only.

writer built (your machine)

| When (UTC) | Actor | Action | Object | Change | Source IP |
| --- | --- | --- | --- | --- | --- |
| 14 Oct 00:14 | system | suppression.add | ops@example.net | reason complaint, scope marketing | - |
| 13 Oct 23:40 | dns-checker | domain.hold | news.acme.example | Active → Held (DKIM es2026b missing) | - |
| 13 Oct 18:02 | Priya N. | template_version.submit | October newsletter v4 | Draft → Pending approval, 2 warnings | 198.51.100.7 |
| 13 Oct 11:40 | Dev team | template_version.submit | Password reset v2 | Draft → Pending approval | 198.51.100.22 |
| 12 Oct 16:05 | Alex R. | user.role_change | Lee W. | + Campaign Manager | 198.51.100.3 |
| 12 Oct 09:15 | Morgan T. | template_version.approve | BF early access v2 | Pending → Approved, checksum 9f3c…a11e | 198.51.100.9 |

# Unsubscribe page

What a recipient sees from the link in the footer. No login. Mail clients that support one-click unsubscribe skip this page entirely.

backend planned · §10.8.4

Acme · mail.acme.example

## Stop marketing email to dana.k@example.org?

You’ll stop getting newsletters and offers from Acme. Receipts and password resets still arrive.

Scope: all Acme marketing email. Q-EVT-4

Acme · mail.acme.example

## You’re unsubscribed

dana.k@example.org won’t get Acme marketing email from now on. If one is already on its way, it was sent before you clicked.

Visiting this link again changes nothing.