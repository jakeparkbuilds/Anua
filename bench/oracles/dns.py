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


def a_record(domain: str) -> list[str]:
    try:
        return sorted(str(r) for r in _resolve(domain, "A"))
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
