"""
Sending-domain DNS posture checks (§5.4): SPF, DKIM, DMARC, bounce MX, FCrDNS.

Pure functions over an injected resolver — no Django, no Celery — so the same
code backs ``make dns-check``, the periodic verification task, and tests that
never touch real DNS.

These are posture checks, not a full RFC 7208 SPF evaluator. Known
simplifications, all erring towards reporting a problem rather than hiding one:

- SPF: an IP counts as authorized if any ``+`` mechanism covers it, regardless of
  ordering against ``-`` mechanisms earlier in the record. Macros are not expanded.
- DMARC: the organizational domain is approximated by walking up the labels
  rather than consulting the Public Suffix List. Correct for ``mail.example.com``,
  wrong for ``mail.example.co.uk``-style suffixes.
"""

from __future__ import annotations

import base64
import enum
import ipaddress
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

Resolver = Callable[[str, str], list[str]]
"""``resolve(name, rdtype)`` → answers as text. ``[]`` for NXDOMAIN / no answer.
Raises ``DnsLookupError`` when the answer is unknowable (timeout, SERVFAIL)."""

SPF_LOOKUP_LIMIT = 10  # RFC 7208 §4.6.4
DKIM_RSA_MIN_BITS = 1024  # RFC 8301: verifiers MUST NOT accept less
DKIM_RSA_RECOMMENDED_BITS = 2048


class DnsLookupError(Exception):
    """The DNS answer could not be determined. Never treated as a pass or a fail."""


class Status(enum.Enum):
    PASS = "pass"  # noqa: S105 — a check outcome, not a password
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"  # lookup error — retry later, do not act on it

    @property
    def acceptable(self) -> bool:
        return self in (Status.PASS, Status.WARN)


@dataclass(frozen=True)
class CheckResult:
    check: str
    status: Status
    detail: str
    record: str | None = None


@dataclass
class DnsReport:
    domain: str
    results: list[CheckResult] = field(default_factory=list)

    def get(self, check: str) -> CheckResult:
        return next(r for r in self.results if r.check == check)

    @property
    def ok(self) -> bool:
        return all(r.status.acceptable for r in self.results)

    @property
    def has_unknown(self) -> bool:
        return any(r.status is Status.UNKNOWN for r in self.results)


# ─── Record helpers ──────────────────────────────────────────────────────


def _records_with_prefix(resolve: Resolver, name: str, prefix: str) -> list[str]:
    return [r for r in resolve(name, "TXT") if r.lower().startswith(prefix)]


def _tags(record: str) -> dict[str, str]:
    """Parse ``k=v; k=v`` tag lists (DKIM, DMARC). Whitespace is insignificant."""
    tags: dict[str, str] = {}
    for part in record.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            tags[key.strip().lower()] = "".join(value.split())
    return tags


def _addresses(resolve: Resolver, host: str) -> list[str]:
    return resolve(host, "A") + resolve(host, "AAAA")


# ─── SPF ─────────────────────────────────────────────────────────────────


@dataclass
class _SpfState:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    lookups: int = 0
    all_qualifier: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _spf_walk(resolve: Resolver, domain: str, state: _SpfState, *, top: bool, depth: int) -> None:
    if depth > SPF_LOOKUP_LIMIT:
        state.errors.append("include/redirect chain too deep")
        return
    records = _records_with_prefix(resolve, domain, "v=spf1")
    if len(records) != 1:
        state.errors.append(f"{domain}: {'no' if not records else 'multiple'} SPF records")
        return

    redirect: str | None = None
    for term in records[0].split()[1:]:
        lowered = term.lower()
        if lowered.startswith("redirect="):
            redirect = term.split("=", 1)[1]
            continue
        if "=" in lowered.split(":", 1)[0]:
            continue  # exp= and unknown modifiers
        qualifier = term[0] if term[0] in "+-~?" else "+"
        mechanism, _, arg = term.lstrip("+-~?").partition(":")
        mechanism = mechanism.split("/", 1)[0].lower()

        if mechanism == "all":
            if top:
                state.all_qualifier = qualifier
            return  # nothing after `all` is evaluated, including redirect
        if mechanism in ("ip4", "ip6"):
            if qualifier == "+":
                try:
                    state.networks.append(ipaddress.ip_network(arg.strip(), strict=False))
                except ValueError:
                    state.errors.append(f"{domain}: invalid {mechanism} '{arg}'")
        elif mechanism in ("a", "mx"):
            state.lookups += 1
            target = arg.split("/", 1)[0] or domain
            hosts = resolve(target, "MX") if mechanism == "mx" else [target]
            if qualifier == "+":
                for host in hosts[:10]:
                    state.networks.extend(
                        ipaddress.ip_network(ip) for ip in _addresses(resolve, host)
                    )
        elif mechanism == "include":
            state.lookups += 1
            _spf_walk(resolve, arg, state, top=False, depth=depth + 1)
        elif mechanism in ("exists", "ptr"):
            state.lookups += 1
            if mechanism == "ptr":
                state.warnings.append(f"{domain}: 'ptr' is deprecated (RFC 7208 §5.5)")

    if redirect:
        state.lookups += 1
        _spf_walk(resolve, redirect, state, top=top, depth=depth + 1)


def check_spf(resolve: Resolver, domain: str, sending_ips: Iterable[str] = ()) -> CheckResult:
    """SPF is evaluated on the envelope sender — pass the return-path domain."""
    try:
        records = _records_with_prefix(resolve, domain, "v=spf1")
        state = _SpfState()
        _spf_walk(resolve, domain, state, top=True, depth=0)
    except DnsLookupError as exc:
        return CheckResult("spf", Status.UNKNOWN, str(exc))
    record = records[0] if len(records) == 1 else None

    if state.errors:
        return CheckResult("spf", Status.FAIL, "; ".join(state.errors), record)
    if state.lookups > SPF_LOOKUP_LIMIT:
        return CheckResult(
            "spf",
            Status.FAIL,
            f"{state.lookups} DNS-querying terms; receivers permerror above {SPF_LOOKUP_LIMIT}",
            record,
        )
    if state.all_qualifier == "+":
        return CheckResult(
            "spf", Status.FAIL, "'+all' authorizes every host on the internet", record
        )

    ips = list(sending_ips)
    unauthorized = [
        ip for ip in ips if not any(ipaddress.ip_address(ip) in net for net in state.networks)
    ]
    if unauthorized:
        return CheckResult(
            "spf", Status.FAIL, f"sending IPs not authorized: {', '.join(unauthorized)}", record
        )

    notes = list(state.warnings)
    if state.all_qualifier in (None, "?"):
        notes.append("record does not end in '-all' or '~all'; unlisted hosts are neutral")
    if not ips:
        notes.append("no sending IPs registered, so authorization was not checked")
    if notes:
        return CheckResult("spf", Status.WARN, "; ".join(notes), record)
    return CheckResult(
        "spf", Status.PASS, f"{len(ips)} sending IP(s) authorized, {state.lookups} lookups", record
    )


# ─── DKIM ────────────────────────────────────────────────────────────────


def check_dkim(resolve: Resolver, domain: str, selector: str) -> CheckResult:
    name = f"{selector}._domainkey.{domain}"
    try:
        records = resolve(name, "TXT")
    except DnsLookupError as exc:
        return CheckResult("dkim", Status.UNKNOWN, str(exc))
    if not records:
        return CheckResult("dkim", Status.FAIL, f"no key published at {name}")
    if len(records) > 1:
        return CheckResult("dkim", Status.FAIL, f"multiple TXT records at {name}")

    record = records[0]
    tags = _tags(record)
    if tags.get("v", "DKIM1") != "DKIM1":
        return CheckResult("dkim", Status.FAIL, f"v={tags['v']} is not DKIM1", record)
    key_b64 = tags.get("p")
    if key_b64 is None:
        return CheckResult("dkim", Status.FAIL, "no p= (public key) tag", record)
    if key_b64 == "":
        return CheckResult("dkim", Status.FAIL, "empty p= — this key has been revoked", record)

    key_type = tags.get("k", "rsa").lower()
    if key_type == "ed25519":
        return CheckResult("dkim", Status.PASS, "ed25519 key", record)
    if key_type != "rsa":
        return CheckResult("dkim", Status.FAIL, f"unsupported key type k={key_type}", record)

    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        key = load_der_public_key(base64.b64decode(key_b64, validate=True))
    except ValueError:
        return CheckResult("dkim", Status.FAIL, "p= is not a valid public key", record)
    if not isinstance(key, rsa.RSAPublicKey):
        return CheckResult("dkim", Status.FAIL, "k=rsa but p= is not an RSA key", record)
    bits = key.key_size
    if bits < DKIM_RSA_MIN_BITS:
        return CheckResult("dkim", Status.FAIL, f"RSA-{bits} is below the RFC 8301 minimum", record)
    if bits < DKIM_RSA_RECOMMENDED_BITS:
        return CheckResult(
            "dkim", Status.WARN, f"RSA-{bits}; {DKIM_RSA_RECOMMENDED_BITS} recommended", record
        )
    return CheckResult("dkim", Status.PASS, f"RSA-{bits} key", record)


# ─── DMARC ───────────────────────────────────────────────────────────────


def _org_domain(domain: str) -> str:
    return ".".join(domain.split(".")[-2:])


def check_dmarc(resolve: Resolver, domain: str, return_path_domain: str | None) -> CheckResult:
    labels = domain.split(".")
    candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    try:
        for owner in candidates:
            records = _records_with_prefix(resolve, f"_dmarc.{owner}", "v=dmarc1")
            if records:
                break
        else:
            return CheckResult("dmarc", Status.FAIL, f"no DMARC record for {domain} or its parents")
    except DnsLookupError as exc:
        return CheckResult("dmarc", Status.UNKNOWN, str(exc))

    if len(records) > 1:
        return CheckResult("dmarc", Status.FAIL, f"multiple DMARC records at _dmarc.{owner}")
    record = records[0]
    tags = _tags(record)
    inherited = owner != domain
    policy = (tags.get("sp") if inherited else None) or tags.get("p")
    if policy not in ("none", "quarantine", "reject"):
        return CheckResult("dmarc", Status.FAIL, f"missing or invalid policy p={policy}", record)

    notes = []
    if inherited:
        notes.append(f"inherited from {owner}")
    if policy == "none":
        notes.append("p=none is monitoring only; receivers will not act on failures")
    if tags.get("pct", "100") != "100":
        notes.append(f"pct={tags['pct']} applies the policy to only part of the mail")
    if "rua" not in tags:
        notes.append("no rua= — aggregate reports will not be received")
    if return_path_domain and return_path_domain != domain:
        if tags.get("aspf", "r") == "s":
            notes.append("aspf=s: SPF cannot align with a separate return-path domain")
        elif _org_domain(return_path_domain) != _org_domain(domain):
            notes.append("return-path is in a different organizational domain; SPF cannot align")

    warn = any(not n.startswith("inherited") for n in notes)
    summary = f"policy {policy}" + (f" ({'; '.join(notes)})" if notes else "")
    return CheckResult("dmarc", Status.WARN if warn else Status.PASS, summary, record)


# ─── Bounce MX, tracking host, reverse DNS ───────────────────────────────


def check_return_path_mx(resolve: Resolver, return_path_domain: str) -> CheckResult:
    try:
        mx = resolve(return_path_domain, "MX")
    except DnsLookupError as exc:
        return CheckResult("return_path_mx", Status.UNKNOWN, str(exc))
    if not mx:
        return CheckResult(
            "return_path_mx", Status.FAIL, f"{return_path_domain} has no MX; bounces would be lost"
        )
    return CheckResult("return_path_mx", Status.PASS, f"MX → {', '.join(mx)}")


def check_tracking_host(resolve: Resolver, host: str) -> CheckResult:
    try:
        target = resolve(host, "CNAME") or _addresses(resolve, host)
    except DnsLookupError as exc:
        return CheckResult("tracking", Status.UNKNOWN, str(exc))
    if not target:
        return CheckResult(
            "tracking", Status.WARN, f"{host} does not resolve; tracked links would break"
        )
    return CheckResult("tracking", Status.PASS, f"{host} → {', '.join(target)}")


def check_fcrdns(resolve: Resolver, ip: str, expected_host: str) -> CheckResult:
    """Forward-confirmed reverse DNS: PTR(ip) = host, and host resolves back to ip."""
    name = f"fcrdns:{ip}"
    try:
        ptrs = [
            p.rstrip(".").lower() for p in resolve(ipaddress.ip_address(ip).reverse_pointer, "PTR")
        ]
        if expected_host not in ptrs:
            found = ", ".join(ptrs) or "none"
            return CheckResult(name, Status.FAIL, f"PTR is {found}, expected {expected_host}")
        forward = _addresses(resolve, expected_host)
    except DnsLookupError as exc:
        return CheckResult(name, Status.UNKNOWN, str(exc))
    if not any(ipaddress.ip_address(a) == ipaddress.ip_address(ip) for a in forward):
        return CheckResult(name, Status.FAIL, f"{expected_host} does not resolve back to {ip}")
    return CheckResult(name, Status.PASS, f"{ip} ⇄ {expected_host}")


# ─── Whole-domain report ─────────────────────────────────────────────────


def check_domain(
    resolve: Resolver,
    *,
    domain: str,
    selector: str,
    return_path_domain: str | None = None,
    tracking_domain: str | None = None,
    sending_ips: dict[str, str] | None = None,
) -> DnsReport:
    """``sending_ips`` maps each IP to the hostname its PTR must name."""
    ips = sending_ips or {}
    report = DnsReport(domain)
    report.results.append(check_spf(resolve, return_path_domain or domain, ips))
    report.results.append(check_dkim(resolve, domain, selector))
    report.results.append(check_dmarc(resolve, domain, return_path_domain))
    if return_path_domain:
        report.results.append(check_return_path_mx(resolve, return_path_domain))
    if tracking_domain:
        report.results.append(check_tracking_host(resolve, tracking_domain))
    for ip, host in ips.items():
        report.results.append(check_fcrdns(resolve, ip, host))
    return report


def dnspython_resolver(timeout: float = 5.0) -> Resolver:
    """The real resolver. Imported lazily so tests never need network access."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout

    def resolve(name: str, rdtype: str) -> list[str]:
        try:
            answer = resolver.resolve(name, rdtype)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as exc:
            raise DnsLookupError(f"{rdtype} {name}: {exc.__class__.__name__}") from exc
        if rdtype == "TXT":
            return [b"".join(rr.strings).decode("utf-8", "replace") for rr in answer]
        if rdtype == "MX":
            return [str(rr.exchange).rstrip(".").lower() for rr in answer]
        return [rr.to_text().rstrip(".").lower() for rr in answer]

    return resolve
