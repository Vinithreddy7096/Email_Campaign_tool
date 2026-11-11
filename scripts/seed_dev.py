#!/usr/bin/env python
"""
Development seed: a test sending domain and 200 fake contacts (§5.2).

Roles are not seeded here — migration accounts/0003 creates them in every
environment.

Every address is on ``example.test`` (RFC 2606 reserved, and in the default
ALLOWED_TEST_RECIPIENT_DOMAINS), so nothing seeded can ever be a real mailbox.
Idempotent: re-running adds nothing that already exists. Refuses to run outside
the local environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.db import transaction  # noqa: E402

from common.models import Stream  # noqa: E402
from consent.models import ConsentRecord  # noqa: E402
from contacts.models import Contact, ContactList, ListMembership  # noqa: E402
from domains.models import IpPool, SenderIdentity, SendingDomain  # noqa: E402

CONTACT_COUNT = 200
SEED_DOMAIN = "example.test"


def main() -> None:
    if settings.ENVIRONMENT != "local" or not settings.MOCK_MODE:
        sys.exit("seed_dev.py only runs in the local environment with MOCK_MODE=true")

    with transaction.atomic():
        pool, _ = IpPool.objects.get_or_create(
            name="dev-marketing", defaults={"stream": Stream.MARKETING}
        )
        domain, _ = SendingDomain.objects.get_or_create(
            domain=f"mail.{SEED_DOMAIN}",
            defaults={
                "stream": Stream.MARKETING,
                "return_path_domain": f"bounce.{SEED_DOMAIN}",
                "ip_pool": pool,
            },
        )
        SenderIdentity.objects.get_or_create(
            domain=domain, local_part="news", defaults={"display_name": "Dev Newsletter"}
        )
        newsletter, _ = ContactList.objects.get_or_create(
            name="Dev newsletter", defaults={"stream": Stream.MARKETING}
        )

        created = 0
        for i in range(1, CONTACT_COUNT + 1):
            email = f"user{i:03d}@{SEED_DOMAIN}"
            if Contact.objects.by_email(email).exists():
                continue
            contact = Contact.objects.create(
                email=email, first_name="Test", last_name=f"User {i:03d}"
            )
            ListMembership.objects.create(contact=contact, contact_list=newsletter)
            ConsentRecord.objects.create(
                contact=contact,
                stream=Stream.MARKETING,
                contact_list=newsletter,
                action=ConsentRecord.Action.GRANTED,
                method=ConsentRecord.Method.MANUAL,
                source="scripts/seed_dev.py",
                evidence={"seed": True},
            )
            created += 1

    print(f"Seeded {domain} (sender news@{domain}), list '{newsletter}'.")
    print(f"Contacts: {created} created, {CONTACT_COUNT - created} already present.")


if __name__ == "__main__":
    main()
