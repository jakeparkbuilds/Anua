"""Network pack: the original 13 DNS / TLS / HTTP / email oracles. Behaviour unchanged —
the callables are the same functions in oracles/dns.py, tls.py, http.py."""
from __future__ import annotations
from .. import dns as _dns, tls as _tls, http as _http
from . import spec

_BADSSL = ("expired.badssl.com", "self-signed.badssl.com", "wrong.host.badssl.com",
           "untrusted-root.badssl.com", "revoked.badssl.com")
_NX = ("this-domain-does-not-exist-zz.invalid",)

P = "network"
SPECS = {s.key: s for s in [
    spec("dns.a_record", _dns.a_record,
         "Sorted list of IPv4 A records for the domain (empty list if none / NXDOMAIN)",
         comparator="set_overlap", returns="list", negatives=_NX, pack=P),
    spec("dns.aaaa_record", _dns.aaaa_record,
         "Sorted list of IPv6 AAAA records for the domain (empty list if none / NXDOMAIN)",
         comparator="set_overlap", returns="list", negatives=_NX, pack=P),
    spec("dns.ns", _dns.ns,
         "Sorted list of authoritative NS hostnames from live DNS, lowercase, no trailing dot",
         comparator="set_overlap", returns="list", negatives=_NX, pack=P),
    spec("dns.txt", _dns.txt,
         "List of TXT record strings published at the domain (empty if none)",
         comparator="set_overlap", returns="list", pack=P),
    spec("dns.resolves", _dns.resolves,
         "True iff the domain has an A or AAAA record (False on NXDOMAIN)",
         negatives=_NX, pack=P),
    spec("dns.mx", _dns.mx,
         "Sorted list of MX exchange hostnames, lowercase, no trailing dot (empty if none)",
         comparator="set_overlap", returns="list", pack=P),
    spec("dns.dnssec", _dns.dnssec,
         "True iff the zone is DNSSEC-signed (DS at parent or DNSKEY present)",
         negatives=("badssl.com",), pack=P),
    spec("tls.chain_valid", _tls.chain_valid,
         "True iff a default-context TLS handshake on :443 succeeds (chain trusted, not expired, hostname matches)",
         negatives=_BADSSL, pack=P),
    spec("tls.expired", _tls.expired,
         "True iff the leaf certificate's notAfter is in the past",
         negatives=("expired.badssl.com",), pack=P),
    spec("tls.hostname_match", _tls.hostname_match,
         "True iff the certificate's SANs cover the hostname",
         negatives=("wrong.host.badssl.com",), pack=P),
    spec("tls.not_after", _tls.not_after,
         "Leaf certificate expiry date as ISO date YYYY-MM-DD",
         comparator="date_close", returns="date", pack=P),
    spec("tls.issuer", _tls.issuer,
         "Issuer common name of the leaf certificate (e.g. 'R11', 'WE1')",
         comparator="contains", returns="str", pack=P),
    spec("http.status", _http.status,
         "HTTP status code of https://domain/ — the first response's code, or the final code after redirects; "
         "either counts",
         comparator="one_of", returns="list", pack=P),
    spec("http.https_ok", _http.https_ok,
         "True iff https://domain/ is reachable with certificate verification (False on cert failure)",
         negatives=_BADSSL, pack=P),
    spec("email.spf", _dns.spf,
         "True iff the domain publishes a TXT record starting with v=spf1",
         negatives=("expired.badssl.com",), pack=P),
    spec("email.dmarc", _dns.dmarc,
         "True iff _dmarc.<domain> publishes a TXT record starting with v=DMARC1",
         negatives=("expired.badssl.com",), pack=P),
]}
