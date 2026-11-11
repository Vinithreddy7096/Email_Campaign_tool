"""Read Postfix delivery-status lines and record them (BUILD.md §10.8.1).

    manage.py ingest_postfix_log /var/log/mail.log      # a file
    journalctl -u postfix -f | manage.py ingest_postfix_log -   # stdin
    manage.py ingest_postfix_log /var/log/mail.log --follow     # tail -F

Idempotent: re-reading a file records nothing twice.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Iterator
from typing import Any, TextIO

from django.core.management.base import BaseCommand, CommandParser

from workers.services.events import ingest_log_lines

BATCH = 500


def _follow(handle: TextIO) -> Iterator[str]:
    handle.seek(0, 2)
    while True:
        line = handle.readline()
        if line:
            yield line
        else:
            time.sleep(0.5)


class Command(BaseCommand):
    help = "Record Postfix delivery-status log lines as delivery events."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("path", help="log file, or - for stdin")
        parser.add_argument("--follow", action="store_true", help="keep reading new lines")

    def handle(self, *args: Any, **opts: Any) -> None:
        with contextlib.ExitStack() as stack:
            if opts["path"] == "-":
                handle: TextIO = sys.stdin
            else:
                handle = stack.enter_context(open(opts["path"], encoding="utf-8", errors="replace"))
            totals = self._ingest(handle, follow=opts["follow"])
        self.stdout.write(f"{totals['events']} events recorded, {totals['duplicates']} already known, "
                          f"{totals['ignored']} other lines ignored.")  # fmt: skip

    def _ingest(self, handle: TextIO, *, follow: bool) -> dict[str, int]:
        lines = _follow(handle) if follow else iter(handle)
        totals = {"events": 0, "duplicates": 0, "ignored": 0}
        batch: list[str] = []
        for line in lines:
            batch.append(line)
            if len(batch) >= BATCH or follow:
                for k, v in ingest_log_lines(batch).items():
                    totals[k] += v
                batch = []
        if batch:
            for k, v in ingest_log_lines(batch).items():
                totals[k] += v
        return totals
