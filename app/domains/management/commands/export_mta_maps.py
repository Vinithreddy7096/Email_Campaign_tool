"""Print what the MTA hosts need to know about our sending domains, as JSON
(BUILD.md §10.10). The Ansible postfix/opendkim roles render their maps from it:

    {
      "generated_at": "2026-10-15T12:00:00+00:00",
      "signing":          [{"domain": "news.example.com", "selector": "s2026"}],
      "sender_transport": [{"return_path_domain": "bounce.news.example.com",
                            "stream": "marketing"}],
      "bounce_domains":   ["bounce.news.example.com"]
    }

signing          OpenDKIM KeyTable / SigningTable (keys come from the secret store)
sender_transport Postfix routes each envelope-sender domain to its stream's
                 transport, which binds that stream's pool IP (§10.3)
bounce_domains   Postfix accepts mail for these and pipes it to the event intake

RETIRED domains are left out. PAUSED ones stay: mail already in the Postfix
queue must still be signed and routed.
"""

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from domains.models import SendingDomain


def mta_maps() -> dict[str, Any]:
    domains = list(
        SendingDomain.objects.exclude(status=SendingDomain.Status.RETIRED).order_by("domain")
    )
    routes: dict[str, str] = {}
    for d in domains:
        stream = d.stream.lower()
        if routes.setdefault(d.return_path_domain, stream) != stream:
            # One return-path domain cannot route to two streams' IPs.
            raise CommandError(f"return-path domain {d.return_path_domain} is shared by a "
                               "marketing and a transactional domain; give each stream its own")  # fmt: skip
    bounce = set(routes)
    if settings.DEFAULT_RETURN_PATH_DOMAIN:
        bounce.add(settings.DEFAULT_RETURN_PATH_DOMAIN.lower())
    return {
        "generated_at": timezone.now().isoformat(),
        "signing": [{"domain": d.domain, "selector": d.dkim_selector} for d in domains],
        "sender_transport": [
            {"return_path_domain": k, "stream": v} for k, v in sorted(routes.items())
        ],
        "bounce_domains": sorted(bounce),
    }


class Command(BaseCommand):
    help = "Print the Postfix / OpenDKIM maps for the MTA hosts as JSON."

    def handle(self, *args: Any, **opts: Any) -> None:
        self.stdout.write(json.dumps(mta_maps(), indent=2))
