#!/usr/bin/env python
"""
Open-relay assertion against an MTA host (§5.4 `make relay-test`, spec §12, §13).

Run it from a machine OUTSIDE the MTA's postfix_app_networks — from inside,
relaying is allowed by design. Each probe is a fresh SMTP session that stops
at RCPT: no DATA is ever sent, so no mail is created.

Must be REFUSED (5xx):
  external sender → external recipient, null sender, a sender spoofing our own
  bounce domain, and sender-specified routing through our bounce domain
  (percent hack, bang path, source route, quoted local part), an IP literal,
  and VRFY.
Must be ACCEPTED (2xx), with --bounce-domain:
  mail to our bounce domain — otherwise bounces and complaints are lost.

Exit status: 0 every check passed; 1 the host relays (or refuses bounces);
2 inconclusive (could not connect, or a 4xx "try later" answer). Only 0 is a pass.

    python scripts/relay_test.py --host mta1.example.com --bounce-domain bounce.example.com
"""

from __future__ import annotations

import argparse
import smtplib
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

EXTERNAL_SENDER = "relay-test@example.net"
EXTERNAL_RECIPIENT = "relay-test@example.org"
PASS, OPEN, INCONCLUSIVE = 0, 1, 2


@dataclass(frozen=True)
class Probe:
    name: str
    sender: str | None  # None: a VRFY probe
    recipient: str
    expect_accept: bool = False


@dataclass(frozen=True)
class Result:
    probe: Probe
    code: int | None  # None: no answer
    reply: str

    @property
    def verdict(self) -> int:
        if self.code is None or 400 <= self.code < 500:
            return INCONCLUSIVE
        accepted = 200 <= self.code < 300
        if self.probe.expect_accept:
            return PASS if accepted else OPEN
        if self.probe.sender is None:  # VRFY: 252 "cannot verify" is a refusal too
            return PASS if self.code >= 500 or self.code == 252 else OPEN
        return OPEN if accepted else PASS


def probes(bounce_domain: str | None) -> list[Probe]:
    out = [
        Probe("external → external", EXTERNAL_SENDER, EXTERNAL_RECIPIENT),
        Probe("null sender → external", "", EXTERNAL_RECIPIENT),
        Probe("IP literal recipient", EXTERNAL_SENDER, "relay-test@[192.0.2.1]"),
        Probe("VRFY", None, "root"),
    ]
    if bounce_domain:
        d = bounce_domain
        out += [
            Probe(f"spoofed @{d} sender → external", f"relay-test@{d}", EXTERNAL_RECIPIENT),
            Probe("percent hack", EXTERNAL_SENDER, f"relay-test%example.org@{d}"),
            Probe("bang path", EXTERNAL_SENDER, f"example.org!relay-test@{d}"),
            Probe("source route", EXTERNAL_SENDER, f"@{d}:relay-test@example.org"),
            Probe("quoted local part", EXTERNAL_SENDER, f'"relay-test@example.org"@{d}'),
            Probe(
                f"bounce to @{d} is accepted",
                EXTERNAL_SENDER,
                f"b-relay-test@{d}",
                expect_accept=True,
            ),  # fmt: skip
        ]
    return out


def run_probe(probe: Probe, *, host: str, port: int, helo: str, timeout: float,
              smtp_factory: Callable[..., Any] = smtplib.SMTP) -> Result:  # fmt: skip
    try:
        smtp = smtp_factory(host, port, timeout=timeout)
    except (OSError, smtplib.SMTPException) as exc:
        return Result(probe, None, f"connect failed: {exc}")
    try:
        smtp.ehlo(helo)
        if probe.sender is None:
            code, msg = smtp.docmd("VRFY", probe.recipient)
            return Result(probe, code, _text(msg))
        code, msg = smtp.docmd(f"MAIL FROM:<{probe.sender}>")
        if code >= 400:
            # Refused at MAIL FROM: nothing can be relayed in this session.
            return Result(probe, code, _text(msg))
        code, msg = smtp.docmd(f"RCPT TO:<{probe.recipient}>")
        return Result(probe, code, _text(msg))
    except (OSError, smtplib.SMTPException) as exc:
        return Result(probe, None, f"session failed: {exc}")
    finally:
        try:
            smtp.docmd("RSET")
            smtp.quit()
        except (OSError, smtplib.SMTPException):
            smtp.close()


def _text(msg: bytes | str) -> str:
    return (msg.decode("utf-8", "replace") if isinstance(msg, bytes) else msg).replace("\n", " ")


def main(argv: list[str] | None = None, smtp_factory: Callable[..., Any] = smtplib.SMTP) -> int:
    parser = argparse.ArgumentParser(description="Assert an MTA host is not an open relay.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=25)
    parser.add_argument("--bounce-domain", help="one of our bounce domains (adds routing checks)")
    parser.add_argument("--helo", default=socket.getfqdn() or "relay-test.invalid")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)

    results = [run_probe(p, host=args.host, port=args.port, helo=args.helo, timeout=args.timeout,
                         smtp_factory=smtp_factory) for p in probes(args.bounce_domain)]  # fmt: skip
    labels = {PASS: "ok", OPEN: "FAIL", INCONCLUSIVE: "??"}
    for r in results:
        want = "accept" if r.probe.expect_accept else "refuse"
        print(
            f"{labels[r.verdict]:>4}  {r.probe.name:<34} want {want:<6} got {r.code}  {r.reply[:90]}"
        )
    worst = max(r.verdict for r in results)
    print({PASS: f"PASS: {args.host} is not an open relay.",
           OPEN: f"FAIL: {args.host} relays or refuses bounces — fix before sending anything.",
           INCONCLUSIVE: f"INCONCLUSIVE: could not prove {args.host} refuses relaying."}[worst])  # fmt: skip
    return worst


if __name__ == "__main__":
    sys.exit(main())
