"""§5.4 DNS posture checks, against a recorded zone. No network access."""

import pytest

from workers.services.dns_checks import (
    Status,
    check_dkim,
    check_dmarc,
    check_domain,
    check_fcrdns,
    check_return_path_mx,
    check_spf,
    check_tracking_host,
)

DOMAIN = "mail.example.com"
RETURN_PATH = "bounce.example.com"
IP = "203.0.113.10"
PTR = "10.113.0.203.in-addr.arpa"


def full_report(resolver):
    return check_domain(
        resolver,
        domain=DOMAIN,
        selector="s1",
        return_path_domain=RETURN_PATH,
        tracking_domain="click.example.com",
        sending_ips={IP: "mta1.example.com"},
    )


def test_correctly_configured_domain_passes_everything(resolver):
    report = full_report(resolver)
    assert report.ok, [(r.check, r.status, r.detail) for r in report.results]
    assert [r.check for r in report.results] == [
        "spf", "dkim", "dmarc", "return_path_mx", "tracking", f"fcrdns:{IP}",
    ]  # fmt: skip


# ─── SPF ─────────────────────────────────────────────────────────────────


def test_spf_follows_includes(resolver):
    assert check_spf(resolver, RETURN_PATH, ["198.51.100.7"]).status is Status.PASS


def test_spf_unauthorized_ip_fails(resolver):
    result = check_spf(resolver, RETURN_PATH, ["192.0.2.1"])
    assert result.status is Status.FAIL and "192.0.2.1" in result.detail


@pytest.mark.parametrize(
    ("records", "needle"),
    [
        ([], "no SPF"),
        (["v=spf1 -all", "v=spf1 ~all"], "multiple"),
        (["v=spf1 +all"], "every host"),
        (["v=spf1 ip4:not-an-ip -all"], "invalid"),
    ],
)
def test_spf_failures(resolver, dns_zone, records, needle):
    dns_zone[RETURN_PATH]["TXT"] = records
    result = check_spf(resolver, RETURN_PATH, [IP])
    assert result.status is Status.FAIL and needle in result.detail


def test_spf_lookup_limit(resolver, dns_zone):
    includes = " ".join(f"include:i{n}.example.com" for n in range(11))
    dns_zone[RETURN_PATH]["TXT"] = [f"v=spf1 ip4:203.0.113.0/28 {includes} -all"]
    for n in range(11):
        dns_zone[f"i{n}.example.com"] = {"TXT": ["v=spf1 -all"]}
    result = check_spf(resolver, RETURN_PATH, [IP])
    assert result.status is Status.FAIL and "11" in result.detail


def test_spf_without_hard_fail_warns(resolver, dns_zone):
    dns_zone[RETURN_PATH]["TXT"] = ["v=spf1 ip4:203.0.113.0/28 ?all"]
    assert check_spf(resolver, RETURN_PATH, [IP]).status is Status.WARN


def test_spf_redirect_is_followed(resolver, dns_zone):
    dns_zone[RETURN_PATH]["TXT"] = ["v=spf1 redirect=_spf.example.com"]
    assert check_spf(resolver, RETURN_PATH, ["198.51.100.7"]).status is Status.PASS


def test_spf_a_and_mx_mechanisms(resolver, dns_zone):
    dns_zone[RETURN_PATH]["TXT"] = ["v=spf1 a:mta1.example.com -all"]
    assert check_spf(resolver, RETURN_PATH, [IP]).status is Status.PASS


def test_spf_lookup_error_is_unknown_not_fail(resolver, dns_zone):
    dns_zone[RETURN_PATH]["TXT"] = "SERVFAIL"
    assert check_spf(resolver, RETURN_PATH, [IP]).status is Status.UNKNOWN


# ─── DKIM ────────────────────────────────────────────────────────────────


def test_dkim_key_strength(resolver):
    assert check_dkim(resolver, DOMAIN, "s1").status is Status.PASS
    weak = check_dkim(resolver, DOMAIN, "weak")
    assert weak.status is Status.WARN and "RSA-1024" in weak.detail
    assert check_dkim(resolver, DOMAIN, "ed").status is Status.PASS


@pytest.mark.parametrize(
    ("record", "needle"),
    [
        (None, "no key"),
        ("v=DKIM1; k=rsa; p=", "revoked"),
        ("v=DKIM1; k=rsa; p=bm90IGEga2V5", "not a valid"),
        ("v=DKIM2; p=abc", "not DKIM1"),
        ("v=DKIM1; k=dsa; p=abc", "unsupported"),
    ],
)
def test_dkim_failures(resolver, dns_zone, record, needle):
    if record is None:
        del dns_zone[f"s1._domainkey.{DOMAIN}"]
    else:
        dns_zone[f"s1._domainkey.{DOMAIN}"]["TXT"] = [record]
    result = check_dkim(resolver, DOMAIN, "s1")
    assert result.status is Status.FAIL and needle in result.detail


# ─── DMARC ───────────────────────────────────────────────────────────────


def test_dmarc_is_inherited_from_parent(resolver):
    result = check_dmarc(resolver, DOMAIN, RETURN_PATH)
    assert result.status is Status.PASS and "inherited from example.com" in result.detail


def test_dmarc_subdomain_policy_applies_when_inherited(resolver, dns_zone):
    dns_zone["_dmarc.example.com"]["TXT"] = [
        "v=DMARC1; p=reject; sp=none; rua=mailto:x@example.com"
    ]
    result = check_dmarc(resolver, DOMAIN, RETURN_PATH)
    assert result.status is Status.WARN and "policy none" in result.detail


@pytest.mark.parametrize(
    ("record", "status", "needle"),
    [
        ("v=DMARC1; p=none; rua=mailto:x@example.com", Status.WARN, "monitoring only"),
        ("v=DMARC1; p=reject", Status.WARN, "rua"),
        ("v=DMARC1; p=reject; pct=50; rua=mailto:x@example.com", Status.WARN, "pct=50"),
        ("v=DMARC1; p=reject; aspf=s; rua=mailto:x@example.com", Status.WARN, "aspf=s"),
        ("v=DMARC1; p=bogus", Status.FAIL, "invalid policy"),
    ],
)
def test_dmarc_policy_quality(resolver, dns_zone, record, status, needle):
    dns_zone["_dmarc.example.com"]["TXT"] = [record]
    result = check_dmarc(resolver, DOMAIN, RETURN_PATH)
    assert result.status is status and needle in result.detail


def test_dmarc_missing_fails(resolver, dns_zone):
    del dns_zone["_dmarc.example.com"]
    assert check_dmarc(resolver, DOMAIN, RETURN_PATH).status is Status.FAIL


def test_dmarc_flags_return_path_in_other_org(resolver):
    result = check_dmarc(resolver, DOMAIN, "bounce.other-company.net")
    assert result.status is Status.WARN and "cannot align" in result.detail


# ─── MX, tracking, FCrDNS ────────────────────────────────────────────────


def test_bounce_domain_without_mx_fails(resolver, dns_zone):
    del dns_zone[RETURN_PATH]["MX"]
    assert check_return_path_mx(resolver, RETURN_PATH).status is Status.FAIL


def test_unresolvable_tracking_host_warns(resolver):
    assert check_tracking_host(resolver, "nope.example.com").status is Status.WARN


def test_fcrdns_wrong_ptr_fails(resolver, dns_zone):
    dns_zone[PTR]["PTR"] = ["static-203-0-113-10.isp.example.net"]
    result = check_fcrdns(resolver, IP, "mta1.example.com")
    assert result.status is Status.FAIL and "expected mta1.example.com" in result.detail


def test_fcrdns_forward_mismatch_fails(resolver, dns_zone):
    dns_zone["mta1.example.com"]["A"] = ["203.0.113.99"]
    assert check_fcrdns(resolver, IP, "mta1.example.com").status is Status.FAIL


def test_report_with_lookup_error_is_not_ok_and_flags_unknown(resolver, dns_zone):
    dns_zone["_dmarc.example.com"]["TXT"] = "SERVFAIL"
    report = full_report(resolver)
    assert not report.ok and report.has_unknown
