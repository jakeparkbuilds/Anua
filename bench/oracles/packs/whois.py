"""WHOIS pack via RDAP (RFC 9083) over plain httpx — no whois dependency.

rdap.org bootstraps to the registry's RDAP server. Lookups are on the registrable
domain (last two labels — good enough for .com/.org/.ai, wrong for co.uk-style
public suffixes, which we note rather than solve tonight).

A 404 from RDAP is a verdict (domain not registered). Anything else raises.
"""
from __future__ import annotations
from functools import lru_cache
from typing import Any, Optional
import httpx

from ..errors import OracleUnavailable
from . import spec

_TIMEOUT = 15


def registrable(target: str) -> str:
    host = target.strip().lower()
    host = host.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    labels = [l for l in host.split(".") if l]
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


@lru_cache(maxsize=256)
def _rdap(domain: str) -> Optional[dict[str, Any]]:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True,
                          headers={"Accept": "application/rdap+json, application/json"}) as c:
            r = c.get(f"https://rdap.org/domain/{domain}")
    except httpx.HTTPError as e:
        raise OracleUnavailable(f"RDAP {domain}: {type(e).__name__}: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise OracleUnavailable(f"RDAP {domain}: HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise OracleUnavailable(f"RDAP {domain}: non-JSON body") from e


def _need(target: str) -> dict[str, Any]:
    d = _rdap(registrable(target))
    if d is None:
        raise OracleUnavailable(f"RDAP: {registrable(target)} is not registered; no record to read")
    return d


def _event(d: dict, action: str) -> str:
    for ev in d.get("events", []) or []:
        if str(ev.get("eventAction", "")).lower() == action:
            return str(ev.get("eventDate", ""))[:10]
    raise OracleUnavailable(f"RDAP record has no {action} event")


def registered(target: str) -> bool:
    return _rdap(registrable(target)) is not None


def created(target: str) -> str:
    return _event(_need(target), "registration")


def expires(target: str) -> str:
    return _event(_need(target), "expiration")


def registrar(target: str) -> str:
    for ent in _need(target).get("entities", []) or []:
        if "registrar" in [str(r).lower() for r in ent.get("roles", [])]:
            vc = ent.get("vcardArray", [])
            for item in (vc[1] if len(vc) > 1 and isinstance(vc[1], list) else []):
                if isinstance(item, list) and item and item[0] == "fn" and len(item) > 3:
                    return str(item[3])
            if ent.get("handle"):
                return str(ent["handle"])
    raise OracleUnavailable("RDAP record has no registrar entity")


def nameservers(target: str) -> list[str]:
    ns = [str(n.get("ldhName", "")).rstrip(".").lower() for n in _need(target).get("nameservers", []) or []]
    return sorted(n for n in ns if n)


P = "whois"
SPECS = {s.key: s for s in [
    spec("whois.registered", registered, "True iff the registrable domain has an RDAP record (is registered)",
         negatives=("this-domain-does-not-exist-zz.invalid",), pack=P),
    spec("whois.created", created, "Domain registration date as ISO date YYYY-MM-DD",
         comparator="date_close", returns="date", pack=P),
    spec("whois.expires", expires, "Domain registration expiry date as ISO date YYYY-MM-DD",
         comparator="date_close", returns="date", pack=P),
    spec("whois.registrar", registrar, "Registrar name from the RDAP registrar entity",
         comparator="contains", returns="str", pack=P),
    spec("whois.nameservers", nameservers, "Sorted list of authoritative nameserver hostnames from RDAP",
         comparator="set_overlap", returns="list", pack=P),
]}
