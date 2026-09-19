"""ORACLES: claim -> function(domain) -> ground truth.
Adding a claim here + a CLAIM_PATHS entry in agent/adapter.py is all it takes to test
a new capability. Results are cached per (claim, domain) for the run."""
from __future__ import annotations
from functools import lru_cache
from typing import Any, Callable
from . import dns as _dns, tls as _tls, http as _http
from .dns import OracleUnavailable  # re-export

ORACLES: dict[str, Callable[[str], Any]] = {
    "dns.a_record":       _dns.a_record,
    "dns.resolves":       _dns.resolves,
    "dns.mx":             _dns.mx,
    "dns.dnssec":         _dns.dnssec,
    "tls.chain_valid":    _tls.chain_valid,
    "tls.expired":        _tls.expired,
    "tls.hostname_match": _tls.hostname_match,
    "tls.not_after":      _tls.not_after,
    "tls.issuer":         _tls.issuer,
    "http.status":        _http.status,
    "http.https_ok":      _http.https_ok,
    "email.spf":          _dns.spf,
    "email.dmarc":        _dns.dmarc,
}


@lru_cache(maxsize=512)
def compute(claim: str, domain: str) -> Any:
    if claim not in ORACLES:
        raise KeyError(f"no oracle for claim {claim!r}")
    return ORACLES[claim](domain)


def has_oracle(claim: str) -> bool:
    return claim in ORACLES
