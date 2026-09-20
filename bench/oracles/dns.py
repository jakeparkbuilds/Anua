"""DNS oracles via dnspython. Each returns a plain JSON-able value or raises."""
from __future__ import annotations
import dns.resolver
import dns.dnssec
import dns.name
import dns.rdatatype
import dns.exception


from .errors import OracleUnavailable  # re-exported: historically defined here

_resolver = dns.resolver.Resolver()
_resolver.lifetime = 8.0


def _resolve(name: str, rdtype: str):
    """Resolve with TCP fallback (large TXT answers truncate over UDP).

    NXDOMAIN and NoAnswer propagate — they are real answers about the domain, and each
    caller decides what they mean. Every other DNS failure (timeout, no reachable
    nameserver, malformed response) becomes OracleUnavailable: we cannot compute truth,
    so the assertion must be skipped rather than held against the agent.
    """
    try:
        return _resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        raise
    except dns.resolver.LifetimeTimeout:
        try:
            return _resolver.resolve(name, rdtype, tcp=True)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            raise
        except dns.exception.DNSException as e:
            raise OracleUnavailable(f"DNS {rdtype} {name}: {type(e).__name__}") from e
    except dns.exception.DNSException as e:
        raise OracleUnavailable(f"DNS {rdtype} {name}: {type(e).__name__}") from e


_PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")


def _a_via(domain: str, nameserver: str | None) -> list[str]:
    r = _resolver if nameserver is None else dns.resolver.Resolver(configure=False)
    if nameserver is not None:
        r.nameservers = [nameserver]
        r.lifetime = 8.0
    try:
        return sorted(str(x) for x in r.resolve(domain, "A"))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as e:
        raise OracleUnavailable(f"DNS A {domain} via {nameserver or 'system'}: {type(e).__name__}") from e


def a_record(domain: str) -> list[str]:
    """A records — but only when there IS a single truth. google.com and github.com
    answer every resolver differently (geo / round-robin), so an agent using another
    resolver was failed with zero overlap for being right. If our resolver and two public
    ones disagree, there is no ground truth to grade against: OracleUnavailable."""
    ours = _a_via(domain, None)
    for ns in _PUBLIC_RESOLVERS:
        other = _a_via(domain, ns)
        if other != ours:
            raise OracleUnavailable(f"A records for {domain} vary by resolver ({len(ours)} via system vs "
                                    f"{len(other)} via {ns}): geo/round-robin DNS has no single truth to grade")
    return ours


def _stable_records(domain: str, rdtype: str) -> list[str]:
    """Records of one type, graded only when our resolver and two public ones agree
    (same rule as a_record)."""
    def via(ns):
        r = _resolver if ns is None else dns.resolver.Resolver(configure=False)
        if ns is not None:
            r.nameservers = [ns]; r.lifetime = 8.0
        try:
            return sorted(str(x).rstrip(".").lower() for x in r.resolve(domain, rdtype))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as e:
            raise OracleUnavailable(f"DNS {rdtype} {domain} via {ns or 'system'}: {type(e).__name__}") from e
    ours = via(None)
    for ns in _PUBLIC_RESOLVERS:
        if via(ns) != ours:
            raise OracleUnavailable(f"{rdtype} records for {domain} vary by resolver: no single truth to grade")
    return ours


def aaaa_record(domain: str) -> list[str]:
    return _stable_records(domain, "AAAA")


def ns(domain: str) -> list[str]:
    """Live NS records (lowercase, no trailing dot). What an agent that 'runs NS checks'
    actually reports — RDAP's nameserver list is a registry view and can differ."""
    return _stable_records(domain, "NS")


def txt(domain: str) -> list[str]:
    """TXT record strings, each joined and stripped of quotes."""
    try:
        return sorted(b"".join(r.strings).decode(errors="ignore") for r in _resolve(domain, "TXT"))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []


def resolves(domain: str) -> bool:
    try:
        _resolve(domain, "A")
        return True
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        # exists but no A; try AAAA
        try:
            _resolve(domain, "AAAA")
            return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False


def mx(domain: str) -> list[str]:
    try:
        return sorted(str(r.exchange).rstrip(".").lower() for r in _resolve(domain, "MX")
                      if str(r.exchange) != ".")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []


def dnssec(domain: str) -> bool:
    """DNSSEC 'enabled' := zone has a DS record at the parent (or DNSKEY present). Cheap proxy."""
    for rdtype in ("DS", "DNSKEY"):
        try:
            _resolve(domain, rdtype)
            return True
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
    return False


def txt_matching(name: str, prefix: str) -> bool:
    try:
        for r in _resolve(name, "TXT"):
            txt = b"".join(r.strings).decode(errors="ignore").lower()
            if txt.startswith(prefix.lower()):
                return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    return False


def spf(domain: str) -> bool:
    return txt_matching(domain, "v=spf1")


def dmarc(domain: str) -> bool:
    return txt_matching(f"_dmarc.{domain}", "v=DMARC1")
