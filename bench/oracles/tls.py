"""TLS oracles: a real handshake against system roots. This is how badssl negatives
become objective: an agent that says expired.badssl.com is valid is provably wrong."""
from __future__ import annotations
import socket
import ssl
from datetime import datetime, timezone
from cryptography import x509

from .errors import OracleUnavailable


def _peer_der(domain: str, verify: bool, timeout: int = 10) -> bytes:
    """Handshake and return the peer cert DER.

    Raises ssl.SSLCertVerificationError when the cert itself is bad (a verdict), and
    OracleUnavailable for anything else — DNS failure, refused connection, timeout,
    protocol-level error. Those say nothing about the target's cert.
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((domain, 443), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=domain) as ss:
                return ss.getpeercert(binary_form=True)
    except ssl.SSLCertVerificationError:
        raise
    except (ssl.SSLError, OSError) as e:
        raise OracleUnavailable(f"TLS {domain}: {type(e).__name__}: {e}") from e


def chain_valid(domain: str) -> bool:
    """True iff a default-context handshake (hostname + chain + expiry) succeeds."""
    try:
        _peer_der(domain, verify=True)
        return True
    except ssl.SSLCertVerificationError:
        return False


def _cert(domain: str) -> x509.Certificate:
    """The peer cert, fetched without verification. Raises OracleUnavailable if we
    cannot get it at all (we then have no basis for a verdict about expiry/issuer)."""
    return x509.load_der_x509_certificate(_peer_der(domain, verify=False))


def expired(domain: str) -> bool:
    return _cert(domain).not_valid_after_utc < datetime.now(timezone.utc)


def not_after(domain: str) -> str:
    return _cert(domain).not_valid_after_utc.date().isoformat()


def issuer(domain: str) -> str:
    c = _cert(domain)
    try:
        return c.issuer.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    except IndexError:
        return c.issuer.rfc4514_string()


def hostname_match(domain: str) -> bool:
    """Chain may be fine but hostname wrong (wrong.host.badssl.com)."""
    try:
        _peer_der(domain, verify=True)
        return True
    except ssl.SSLCertVerificationError as e:
        msg = str(e).lower()
        if "hostname" in msg or "doesn't match" in msg or "does not match" in msg:
            return False
        # Failed for another reason (expiry, untrusted root). The hostname question is
        # still answerable from the SANs; _cert raises OracleUnavailable if it is not.
        c = _cert(domain)
        try:
            sans = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            sans = []
        return any(_wild(s, domain) for s in sans)


def _wild(pattern: str, host: str) -> bool:
    if pattern.startswith("*."):
        return host.split(".", 1)[1] == pattern[2:] and host.count(".") >= pattern.count(".")
    return pattern.lower() == host.lower()
