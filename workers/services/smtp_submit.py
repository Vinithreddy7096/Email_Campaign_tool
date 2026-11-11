"""One SMTP submission, classified (BUILD.md §10.7.2 item 5).

The SMTP dialogue is driven step by step rather than with ``sendmail()`` so we
know whether the message body was fully transmitted when something fails:

- failure before the body was sent        → DEFERRED (nothing was delivered)
- failure after the body was sent, before
  the server's reply arrived              → UNKNOWN ("maybe sent", Q-DEL-1:
                                             held for operator review, never
                                             resent automatically)
- 2xx / 4xx / 5xx reply                   → ACCEPTED / DEFERRED / REJECTED

QUIT happens after the result is decided, and its failure is ignored: a server
that accepted the message and then dropped the connection still accepted it.
"""

from __future__ import annotations

import re
import smtplib
from dataclasses import dataclass
from typing import Any

ACCEPTED, DEFERRED, REJECTED, UNKNOWN = "ACCEPTED", "DEFERRED", "REJECTED", "UNKNOWN"
_QUEUE_ID = re.compile(r"queued as ([A-Za-z0-9]+)", re.IGNORECASE)


@dataclass(frozen=True)
class SubmitResult:
    outcome: str
    code: int | None
    response: str
    queue_id: str = ""


def _classify(code: int, response: bytes | str) -> SubmitResult:
    text = response.decode("utf-8", "replace") if isinstance(response, bytes) else response
    if 200 <= code < 300:
        match = _QUEUE_ID.search(text)
        return SubmitResult(ACCEPTED, code, text, match.group(1) if match else "")
    if 400 <= code < 500:
        return SubmitResult(DEFERRED, code, text)
    if 500 <= code < 600:
        return SubmitResult(REJECTED, code, text)
    return SubmitResult(DEFERRED, code, f"unexpected reply: {text}")


def dot_stuff(payload: bytes) -> bytes:
    """RFC 5321 §4.5.2 transparency, plus the terminating CRLF.CRLF."""
    lines = payload.replace(b"\r\n", b"\n").split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    stuffed = [b"." + line if line.startswith(b".") else line for line in lines]
    return b"\r\n".join(stuffed) + b"\r\n.\r\n"


def submit(
    *,
    envelope_from: str,
    recipient: str,
    payload: bytes,
    host: str,
    port: int,
    timeout: float,
    use_tls: bool = False,
    username: str = "",
    password: str = "",
    smtp_class: Any = smtplib.SMTP,
) -> SubmitResult:
    body_sent = False
    server = None
    try:
        server = smtp_class(host, port, timeout=timeout)
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        if username:
            server.login(username, password)
        code, response = server.mail(envelope_from)
        if code != 250:
            # A refused sender is our configuration, never the recipient's
            # fault: defer (and let attempts run out) rather than bounce.
            text = response.decode("utf-8", "replace") if isinstance(response, bytes) else response
            return SubmitResult(DEFERRED, code, f"sender refused: {text}")
        code, response = server.rcpt(recipient)
        if code not in (250, 251):
            return _classify(code, response)
        code, response = server.docmd("DATA")
        if code != 354:
            return _classify(code, response)
        server.send(dot_stuff(payload))
        body_sent = True
        code, response = server.getreply()
        return _classify(code, response)
    except (smtplib.SMTPException, OSError) as exc:
        if body_sent:
            return SubmitResult(UNKNOWN, None, f"reply lost after the message was sent: {exc!r}")
        code = getattr(exc, "smtp_code", None)
        if (
            isinstance(code, int)
            and 500 <= code < 600
            and not isinstance(exc, smtplib.SMTPAuthenticationError | smtplib.SMTPSenderRefused)
        ):
            return SubmitResult(REJECTED, code, str(exc))
        # Connection, TLS, auth or sender problems are ours, not the recipient's.
        return SubmitResult(DEFERRED, code if isinstance(code, int) else None, repr(exc))
    finally:
        if server is not None:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                server.close()
