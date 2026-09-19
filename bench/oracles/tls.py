"""TLS oracles: a real handshake against system roots. This is how badssl negatives
become objective: an agent that says expired.badssl.com is valid is provably wrong."""
from __future__ import annotations
import socket
import ssl
from datetime import datetime, timezone
from typing import Optional
from cryptography import x509


def _peer_der(domain: str, verify: bool, timeout: int = 10) -> bytes:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((domain, 443), timeout=timeout) as s:
        with ctx.wrap_socket(s, server_hostname=domain) as ss:
            return ss.getpeercert(binary_form=True)


def chain_valid(domain: str) -> bool:
    """True iff a default-context handshake (hostname + chain + expiry) succeeds."""
    try:
        _peer_der(domain, verify=True)
        return True
    except ssl.SSLCertVerificationError:
        return False
    except (ssl.SSLError, OSError):
        return False


def _cert(domain: str) -> Optional[x509.Certificate]:
    try:
        return x509.load_der_x509_certificate(_peer_der(domain, verify=False))
    except (ssl.SSLError, OSError):
        return None


def expired(domain: str) -> Optional[bool]:
    c = _cert(domain)
    if c is None:
        return None
    return c.not_valid_after_utc < datetime.now(timezone.utc)


def not_after(domain: str) -> Optional[str]:
    c = _cert(domain)
    return c.not_valid_after_utc.date().isoformat() if c else None


def issuer(domain: str) -> Optional[str]:
    c = _cert(domain)
    if c is None:
        return None
    try:
        return c.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    except IndexError:
        return c.issuer.rfc4514_string()


def hostname_match(domain: str) -> Optional[bool]:
    """Chain may be fine but hostname wrong (wrong.host.badssl.com)."""
    try:
        _peer_der(domain, verify=True)
        return True
    except ssl.SSLCertVerificationError as e:
        msg = str(e).lower()
        if "hostname" in msg or "doesn't match" in msg or "does not match" in msg:
            return False
        # failed for another reason — hostname unknown; check SANs manually
        c = _cert(domain)
        if c is None:
            return None
        try:
            sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            sans = []
        return any(_wild(s, domain) for s in sans)
    except (ssl.SSLError, OSError):
        return None


def _wild(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):
        return host.split(".", 1)[1] == pattern[2:] and host.count(".") >= pattern.count(".")
    return pattern.lower() == host.lower()
