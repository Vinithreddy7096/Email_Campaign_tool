"""Applying DNS results to SendingDomain: promote, pause, and never guess."""

import json

import pytest

from audit.models import AuditEvent
from common.models import Stream
from domains.models import IpPool, SendingDomain, SendingIp
from workers.services.domain_verification import verify_domain

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def domain():
    pool = IpPool.objects.create(name="mkt", stream=Stream.MARKETING)
    SendingIp.objects.create(address="203.0.113.10", ptr_hostname="mta1.example.com", pool=pool)
    return SendingDomain.objects.create(
        domain="mail.example.com",
        stream=Stream.MARKETING,
        return_path_domain="bounce.example.com",
        dkim_selector="s1",
        ip_pool=pool,
    )


def test_good_dns_promotes_pending_domain_to_verified(domain, resolver):
    verify_domain(domain, resolver)
    domain.refresh_from_db()
    assert domain.status == SendingDomain.Status.VERIFIED
    assert domain.dns_verified and domain.last_dns_check_at is not None
    event = AuditEvent.objects.get(action="domain.dns_check")
    assert event.after["status"] == "VERIFIED"
    assert event.actor is None and event.actor_label == "celery:domains.verify_all"


def test_manual_check_is_attributed_to_the_user(domain, resolver, django_user_model):
    user = django_user_model.objects.create_user("ops@example.com", "pw")
    verify_domain(domain, resolver, actor=user)
    assert AuditEvent.objects.get(action="domain.dns_check").actor == user


def test_verification_never_activates_a_domain(domain, resolver):
    verify_domain(domain, resolver)
    verify_domain(domain, resolver)
    domain.refresh_from_db()
    assert domain.status == SendingDomain.Status.VERIFIED


def test_reverse_dns_failure_blocks_promotion(domain, resolver, dns_zone):
    dns_zone["10.113.0.203.in-addr.arpa"]["PTR"] = ["wrong.example.net"]
    verify_domain(domain, resolver)
    domain.refresh_from_db()
    assert domain.dns_verified  # SPF/DKIM/DMARC themselves are fine
    assert domain.status == SendingDomain.Status.PENDING_VERIFICATION


def test_broken_dkim_pauses_an_active_domain(domain, resolver, dns_zone):
    verify_domain(domain, resolver)
    SendingDomain.objects.filter(pk=domain.pk).update(status=SendingDomain.Status.ACTIVE)

    dns_zone["s1._domainkey.mail.example.com"]["TXT"] = ["v=DKIM1; k=rsa; p="]
    verify_domain(domain, resolver)
    domain.refresh_from_db()
    assert domain.status == SendingDomain.Status.PAUSED
    assert domain.dkim_verified_at is None and domain.spf_verified_at is not None
    # Not latest("created_at"): two events can share a timestamp on a coarse clock.
    paused = AuditEvent.objects.get(action="domain.dns_check", before__status="ACTIVE")
    assert paused.after["status"] == "PAUSED"


def test_lookup_errors_change_nothing(domain, resolver, dns_zone):
    verify_domain(domain, resolver)
    SendingDomain.objects.filter(pk=domain.pk).update(status=SendingDomain.Status.ACTIVE)
    domain.refresh_from_db()
    verified_at = domain.dkim_verified_at

    dns_zone["s1._domainkey.mail.example.com"]["TXT"] = "SERVFAIL"
    report = verify_domain(domain, resolver)
    domain.refresh_from_db()
    assert report.has_unknown
    assert domain.status == SendingDomain.Status.ACTIVE  # a timeout is not evidence
    assert domain.dkim_verified_at == verified_at


def test_unchanged_results_are_not_re_audited(domain, resolver):
    verify_domain(domain, resolver)
    verify_domain(domain, resolver)
    assert AuditEvent.objects.filter(action="domain.dns_check").count() == 1


def test_retired_ips_are_not_checked(domain, resolver):
    SendingIp.objects.create(
        address="192.0.2.50",
        ptr_hostname="old.example.com",
        pool=domain.ip_pool,
        status=SendingIp.Status.RETIRED,
    )
    assert verify_domain(domain, resolver).ok


def test_task_checks_every_non_retired_domain(domain, monkeypatch, resolver):
    from workers.tasks import domains as task_module

    monkeypatch.setattr(task_module, "dnspython_resolver", lambda: resolver)
    SendingDomain.objects.create(
        domain="old.example.com",
        stream=Stream.MARKETING,
        return_path_domain="bounce.example.com",
        status=SendingDomain.Status.RETIRED,
    )
    assert task_module.verify_all_domains() == {"mail.example.com": True}


def test_cli_exit_codes(monkeypatch, resolver, dns_zone, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "validate_dns.py"
    spec = importlib.util.spec_from_file_location("validate_dns", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "dnspython_resolver", lambda timeout: resolver)

    args = ["--domain", "mail.example.com", "--return-path", "bounce.example.com",
            "--ip", "203.0.113.10=mta1.example.com"]  # fmt: skip
    assert cli.main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    dns_zone["s1._domainkey.mail.example.com"]["TXT"] = ["v=DKIM1; p="]
    assert cli.main(args) == 1
    assert "[FAIL] dkim" in capsys.readouterr().out

    dns_zone["s1._domainkey.mail.example.com"]["TXT"] = "SERVFAIL"
    assert cli.main(args) == 2
