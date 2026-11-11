#!/usr/bin/env python
"""
SPF / DKIM / DMARC / bounce-MX / FCrDNS checker (§5.4 `make dns-check`).

Standalone: needs only dnspython and cryptography, no database. Uses the same
checks as the periodic domain-verification task.

    python scripts/validate_dns.py --domain mail.example.com \\
        --return-path bounce.example.com --selector s1 \\
        --ip 203.0.113.10=mta1.example.com

Exit codes: 0 every check acceptable · 1 at least one FAIL · 2 a lookup error
left a check undetermined (never read 2 as a pass).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workers.services.dns_checks import Status, check_domain, dnspython_resolver  # noqa: E402

SYMBOL = {Status.PASS: "PASS", Status.WARN: "WARN", Status.FAIL: "FAIL", Status.UNKNOWN: "????"}


def parse_ip(value: str) -> tuple[str, str]:
    ip, sep, host = value.partition("=")
    if not sep or not host:
        raise argparse.ArgumentTypeError("use IP=PTR-HOSTNAME, e.g. 203.0.113.10=mta1.example.com")
    return ip.strip(), host.strip().rstrip(".").lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--domain", required=True, help="the From (sending) domain")
    parser.add_argument(
        "--selector", default=os.environ.get("DKIM_SELECTOR_DEFAULT", "s1"), help="DKIM selector"
    )
    parser.add_argument(
        "--return-path",
        default=os.environ.get("DEFAULT_RETURN_PATH_DOMAIN") or None,
        help="envelope/bounce domain; SPF is checked here (default: --domain)",
    )
    parser.add_argument("--tracking", help="link-tracking hostname")
    parser.add_argument(
        "--ip", type=parse_ip, action="append", default=[], help="IP=PTR-HOSTNAME (repeatable)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    report = check_domain(
        dnspython_resolver(args.timeout),
        domain=args.domain.lower().rstrip("."),
        selector=args.selector,
        return_path_domain=args.return_path,
        tracking_domain=args.tracking,
        sending_ips=dict(args.ip),
    )

    if args.json:
        print(
            json.dumps(
                {
                    "domain": report.domain,
                    "ok": report.ok,
                    "results": [
                        {
                            "check": r.check,
                            "status": r.status.value,
                            "detail": r.detail,
                            "record": r.record,
                        }
                        for r in report.results
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"DNS posture for {report.domain}\n")
        for r in report.results:
            print(f"  [{SYMBOL[r.status]}] {r.check:<16} {r.detail}")
            if r.record:
                print(f"         {'':<16} {r.record}")
        print()

    if report.has_unknown:
        print("Some lookups failed — result undetermined, NOT a pass.", file=sys.stderr)
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
