"""Message construction (BUILD.md §10.7.2 item 4, spec §10.2).

Built with ``email.message.EmailMessage`` and the SMTP policy (CRLF, folded
headers) — never by string concatenation, so a value cannot inject a header.

- Message-ID on an organization-controlled domain, and Date.
- multipart/alternative with the text part first, quoted-printable.
- From / Reply-To from the SenderIdentity.
- The bounce address is the SMTP envelope sender (VERP); receiving MTAs write
  Return-Path from it, so no Return-Path header is set here.
- Marketing: List-Unsubscribe (https) + List-Unsubscribe-Post (RFC 8058).

Line length: the policy allows RFC 5322's 998-character limit, not the default
78. A long unsubscribe URL has no whitespace to fold at, and under the 78 limit
the email package would RFC 2047-encode the header (``=?utf-8?q?=3Chttps...``),
which mailbox providers do not decode for List-Unsubscribe — breaking one-click
unsubscribe. (Found by inspecting a real message in MailHog.)
"""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr

from email_templates.rendering import RenderedMessage

_POLICY = SMTP.clone(max_line_length=998)


def build_message(
    *,
    rendered: RenderedMessage,
    from_address: str,
    display_name: str,
    reply_to: str,
    recipient: str,
    message_id: str,
    date: datetime,
    unsubscribe_url: str | None,
) -> EmailMessage:
    msg = EmailMessage(policy=_POLICY)
    msg["From"] = formataddr((display_name, from_address))
    msg["To"] = recipient
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = rendered.subject
    msg["Date"] = format_datetime(date)
    msg["Message-ID"] = message_id
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.set_content(rendered.text, subtype="plain", charset="utf-8", cte="quoted-printable")
    msg.add_alternative(rendered.html, subtype="html", charset="utf-8", cte="quoted-printable")
    return msg


def make_message_id(job_hex: str, attempt: int, domain: str) -> str:
    """Unique per submission: a resend after review gets a new Message-ID."""
    return f"<{job_hex}.{attempt}@{domain}>"
