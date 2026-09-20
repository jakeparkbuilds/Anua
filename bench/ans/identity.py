"""Identity verification (MVP scope = option (a) from planning):

  1. Find the agent in the public registry (discover, not hardcode)
  2. Fetch its Transparency Log entry
  3. Compare the TL-sealed server-cert fingerprint to a LIVE TLS handshake
     -> drift check, same idea as agent-prober's certfingerprint.server signal
  4. TODO(b): fetch the identity certificate and validate the ans:// URI SAN
     against a GoDaddy private-CA root. Needs their root cert — stubbed.

verified == registry_found AND tl_entry_found AND fingerprint_match.
"""
from __future__ import annotations
import hashlib
import socket
import ssl
from typing import Optional
from cryptography import x509

from ..models import AgentIdentity
from .registry import Registry


_LAST_CERT: dict[str, x509.Certificate] = {}


def live_server_fingerprint(host: str, port: int = 443, timeout: int = 10) -> tuple[str, Optional[str]]:
    """Returns (sha256 hex fingerprint, ans:// URI SAN if any) from a live handshake.
    The handshake VERIFIES (default context): a fingerprint we return belongs to a cert
    that is publicly trusted and valid for `host` right now."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    fp = hashlib.sha256(der).hexdigest()
    uri_san = None
    try:
        cert = x509.load_der_x509_certificate(der)
        _LAST_CERT[host] = cert
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        for u in san.value.get_values_for_type(x509.UniformResourceIdentifier):
            if u.startswith("ans://"):
                uri_san = u
    except Exception:
        pass
    return fp, uri_san


def tlsa_fingerprints(host: str) -> Optional[list[str]]:
    """DANE-EE (3 0 1) fingerprints published at _443._tcp.<host>, or None when no TLSA
    record exists / DNS failed. ANS provisions this record with the sealed fingerprint
    and it is the operator-controlled binding an honest rotation updates."""
    try:
        import dns.resolver
        out = []
        for r in dns.resolver.resolve(f"_443._tcp.{host}", "TLSA", lifetime=8):
            if r.usage == 3 and r.selector == 0 and r.mtype == 1:
                out.append(r.cert.hex().lower())
        return out
    except Exception:
        return None


def _tl_is_about(entry: dict, host: str) -> bool:
    """A TL entry reached by an ansId HINT must actually describe this host. The yaml's
    dnsdoc ansId once made example.com 'find' dnsdoc's entry and fail its fingerprint."""
    name = str(Registry.tl_ans_name(entry) or "")
    ev = ((entry.get("payload") or {}).get("producer") or {}).get("event") or {}
    agent_host = str((ev.get("agent") or {}).get("agentHost") or (ev.get("agent") or {}).get("host") or "")
    h = host.lower()
    return name.lower().endswith("." + h) or name.lower() == h or agent_host.lower() == h


def _classify_drift(ident: AgentIdentity, host: str, live_fp: str, tl: Optional[dict]) -> None:
    """The live cert is not the sealed one. Two very different situations:

      ROTATED   the operator renewed (Let's Encrypt does it every 60-90 days): the live
                cert verified against public roots for this host, was issued AFTER the
                seal, and — where the operator publishes DANE — the TLSA record names
                it. The ANS attestation is stale. Not impersonation, not verification.
      MISMATCH  anything else: an older cert than the seal, a TLSA record that names a
                different cert, or no way to tell. This is the alarm.

    dnsdoc renewed at 23:43 UTC on demo eve and this check called it MISMATCH."""
    from datetime import datetime, timezone
    tlsa = tlsa_fingerprints(host)
    ident.tlsa_fingerprints = tlsa
    if tlsa and live_fp in tlsa:
        ident.fingerprint_match = True          # the DNS binding names the live cert: rotated properly
        ident.rotated = True
        ident.notes.append("live cert differs from the TL seal but matches the published TLSA record: "
                           "certificate rotated and the DANE binding was updated")
        return
    if tlsa:
        ident.notes.append(f"SERVER CERT MISMATCH: live cert is neither the TL-sealed one nor in the "
                           f"published TLSA record ({len(tlsa)} entr{'y' if len(tlsa) == 1 else 'ies'})")
        return
    cert = _LAST_CERT.get(host)
    sealed_at = None
    try:
        ev = ((tl or {}).get("payload") or {}).get("producer", {}).get("event", {})
        ts = ev.get("issuedAt") or ev.get("timestamp")
        sealed_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
    except Exception:
        sealed_at = None
    if cert is not None and sealed_at is not None and cert.not_valid_before_utc > sealed_at:
        ident.rotated = True
        ident.notes.append(f"certificate ROTATED: live cert issued {cert.not_valid_before_utc.date()} by "
                           f"{cert.issuer.rfc4514_string()[:40]}, after the ANS seal ({sealed_at.date()}); it "
                           f"verifies for {host} against public roots, but the ANS attestation is stale and "
                           f"no TLSA record binds it — not impersonation evidence, not verified either")
        return
    ident.notes.append("SERVER CERT MISMATCH: live cert is not the TL-sealed one and does not look like a "
                       "later renewal" + (" (it predates the seal)" if cert is not None and sealed_at else ""))


def verify(host: str, registry: Registry, ans_id_hint: str = "", live: bool = False,
           entry_summary: Optional[dict] = None, trust_card: Optional[dict] = None) -> AgentIdentity:
    """Four outcomes, and the UI must tell them apart:
      VERIFIED   registry hit + TL entry + live fingerprint == sealed fingerprint
      PENDING    TL entry exists but no sealed server cert yet (registration in flight)
      MISMATCH   live fingerprint != sealed fingerprint  (drift — the headline alarm)
      NOT_FOUND  no registry hit and no TL entry
    Anything else is UNVERIFIED (e.g. handshake failed)."""
    ident = AgentIdentity(host=host)
    trust_card = trust_card or {}

    # 1. discover
    if entry_summary is None:
        entry_summary = registry.find_by_host(host)
    if entry_summary:
        ident.registry_found = True
        ident.ans_id = (entry_summary.get("agentId") or entry_summary.get("ansId")
                        or entry_summary.get("id") or ans_id_hint or None)
        ident.ans_name = entry_summary.get("ansName") or entry_summary.get("name")
    else:
        # The agent's own trust card / agent card carry the ansId — use them so a
        # search miss (fuzzy ranking, or PENDING_VALIDATION) still reaches the TL.
        ident.ans_id = (ans_id_hint or trust_card.get("agentId") or trust_card.get("ansId") or None)
        ident.ans_name = trust_card.get("ansName")
        ident.notes.append(("not found via registry search" if not getattr(registry, "last_error", None)
                            else f"registry search unavailable ({registry.last_error}); not a verdict on registration")
                           + ("; using ansId from the agent's own cards" if ident.ans_id else ""))

    # 2. transparency log
    tl = None
    if ident.ans_id:
        tl = registry.tl_entry(ident.ans_id)
        if tl and not _tl_is_about(tl, host):
            ident.notes.append(f"transparency-log entry {ident.ans_id} is not about {host} "
                               f"({Registry.tl_ans_name(tl) or 'unknown name'}); ignored")
            ident.ans_id, ident.ans_name, tl = None, None, None
        if tl:
            ident.tl_entry_found = True
            ident.ans_name = ident.ans_name or Registry.tl_ans_name(tl)
            ident.tl_server_fingerprint = Registry.tl_server_fingerprint(tl)
            if not ident.tl_server_fingerprint:
                ident.notes.append("TL entry present, certificate not yet sealed (registration pending)")
        else:
            ident.notes.append("no transparency log entry for ans_id")

    # 3. live handshake vs sealed baseline
    if live:
        try:
            fp, uri = live_server_fingerprint(host)
            ident.live_server_fingerprint = fp
            ident.identity_cert_uri_san = uri
            if ident.tl_server_fingerprint:
                ident.fingerprint_match = (fp == ident.tl_server_fingerprint)
                if not ident.fingerprint_match:
                    _classify_drift(ident, host, fp, tl)
        except Exception as e:
            ident.notes.append(f"live TLS handshake failed: {e}")
    else:
        # mock: pretend live == sealed so the offline pipeline is green
        ident.live_server_fingerprint = ident.tl_server_fingerprint
        ident.fingerprint_match = bool(ident.tl_server_fingerprint)
        ident.notes.append("MOCK MODE: fingerprint match simulated")

    ident.verified = bool(ident.registry_found and ident.tl_entry_found and ident.fingerprint_match)
    if ident.verified:
        ident.status = "VERIFIED"
    elif ident.fingerprint_match is False:
        ident.status = "ROTATED" if ident.rotated else "MISMATCH"
    elif ident.tl_entry_found and not ident.tl_server_fingerprint:
        ident.status = "PENDING"
    elif not ident.registry_found and not ident.tl_entry_found:
        ident.status = "NOT_FOUND"
    else:
        ident.status = "UNVERIFIED"
    return ident
