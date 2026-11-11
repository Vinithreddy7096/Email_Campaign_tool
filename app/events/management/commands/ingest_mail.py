"""Ingest one bounce (DSN) or complaint (ARF) email from stdin (BUILD.md §10.8.1).

Postfix delivers mail for the bounce domain to this command, e.g. in master.cf:

    bounces unix - n n - - pipe
      user=mailer argv=/srv/app/manage.py ingest_mail

Always exits 0 once the message is stored, so Postfix never retries or bounces
a bounce; an unrecognised message is kept for operator review.
"""

from __future__ import annotations

import sys
from typing import Any

from django.core.management.base import BaseCommand

from workers.services.events import ingest_mail


class Command(BaseCommand):
    help = "Record a bounce or complaint email read from stdin."

    def handle(self, *args: Any, **opts: Any) -> None:
        raw = sys.stdin.buffer.read()
        outcomes = ingest_mail(raw)
        for o in outcomes:
            state = "recorded" if o.created else "already known"
            self.stdout.write(f"{o.event.type} {state}")
