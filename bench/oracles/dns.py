"""DNS oracles via dnspython. Each returns a plain JSON-able value or raises."""
from __future__ import annotations
import dns.resolver
import dns.dnssec
import dns.name
import dns.rdatatype
import dns.exception


class OracleUnavailable(Exception):
    """Ground truth could not be computed (timeout/network). Not an agent failure."""


_resolver = dns.resolver.Resolver()
_resolver.lifetime = 8.0


def _resolve(name: str, rdtype: str):
    """Resolve with TCP fallback (large TXT answers truncate over UDP)."""
    try:
        return _resolver.resolve(name, rdtype)
    except dns.resolver.LifetimeTimeout:
        try:
            return _resolver.resolve(name, rdtype, tcp=True)
        except dns.exception.DNSException as e:
            raise OracleUnavailable(f"DNS {rdtype} {name}: {type(e).__name__}") from e


def a_record(domain: str) -> list[str]:
    try:
        return sorted(str(r) for r in _resolver.resolve(domain, "A"))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []


def resolves(domain: str) -> bool:
    try:
        _resolver.resolve(domain, "A")
        return True
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        # exists but no A; try AAAA
        try:
            _resolver.resolve(domain, "AAAA")
            return True
        except dns.exception.DNSException:
            return False
    except dns.exception.DNSException:
        return False


def mx(domain: str) -> list[str]:
    try:
        return sorted(str(r.exchange).rstrip(".").lower() for r in _resolver.resolve(domain, "MX")
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
    except OracleUnavailable:
        raise
    except dns.exception.DNSException as e:
        raise OracleUnavailable(f"DNS TXT {name}: {type(e).__name__}") from e
    return False


def spf(domain: str) -> bool:
    return txt_matching(domain, "v=spf1")


def dmarc(domain: str) -> bool:
    return txt_matching(f"_dmarc.{domain}", "v=DMARC1")
