"""ADAPTER — the one file you will definitely edit after seeing the live agent.

Two responsibilities:
  build_prompt(assertion)  -> the text we send to the agent for this assertion
  extract(raw, claim)      -> pull the value for `claim` out of the agent's response

Extraction strategy, in order:
  1. If the response is JSON, look up the claim in CLAIM_PATHS (dotted paths).
  2. Else regex heuristics per claim family.
  3. Else None (assertion errors out with a clear message — better than a false pass).

HUMAN: after `python -m bench probe-agent --live`, paste the real response shape into
CLAIM_PATHS. That single edit fixes most extraction problems.
"""
from __future__ import annotations
import json
import re
from typing import Any, Optional
from ..models import Assertion

# HUMAN: dotted JSON paths into the agent's structured response, if it returns one.
CLAIM_PATHS: dict[str, list[str]] = {
    "dns.a_record":      ["dns.a", "dns.A", "records.A", "a_records"],
    "dns.mx":            ["dns.mx", "dns.MX", "records.MX", "mx_records"],
    "dns.dnssec":        ["dns.dnssec", "dnssec.enabled", "dns.dnssec_enabled"],
    "dns.resolves":      ["dns.resolves", "dns.ok"],
    "tls.chain_valid":   ["tls.valid", "tls.chain_valid", "ssl.valid", "tls.trusted"],
    "tls.expired":       ["tls.expired", "ssl.expired"],
    "tls.hostname_match":["tls.hostname_match", "tls.hostnameMatch", "ssl.hostname_valid"],
    "tls.not_after":     ["tls.not_after", "tls.notAfter", "ssl.expires", "tls.expiry"],
    "tls.issuer":        ["tls.issuer", "ssl.issuer", "tls.issuer_cn"],
    "http.status":       ["http.status", "http.status_code", "http.statusCode"],
    "http.https_ok":     ["http.https", "http.https_ok"],
    "email.spf":         ["email.spf", "email.spf_present", "spf.present"],
    "email.dmarc":       ["email.dmarc", "email.dmarc_present", "dmarc.present"],
}

_PROMPTS = {
    "default": "Diagnose the domain {domain}. Report DNS (A and MX records, DNSSEC), "
               "TLS/SSL (is the certificate chain valid and trusted, is it expired, does the "
               "hostname match, expiry date, issuer), HTTP status over HTTPS, and email "
               "configuration (SPF and DMARC presence). Respond in JSON if you can.",
}


def build_prompt(a: Assertion) -> str:
    # HUMAN: if the agent wants just a bare domain (common for MCP tools), return a.input["domain"]
    return _PROMPTS["default"].format(**a.input)


def _walk(d: Any, path: str) -> Optional[Any]:
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _try_json(raw: str) -> Optional[dict]:
    raw = raw.strip()
    # tolerate ```json fences and leading prose
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _regex_extract(raw: str, claim: str) -> Optional[Any]:
    t = raw.lower()
    def yes(*pats):  # any pattern present
        return any(re.search(p, t) for p in pats)
    def no(*pats):
        return any(re.search(p, t) for p in pats)

    if claim == "tls.chain_valid":
        if no(r"not (trusted|valid)", r"untrusted", r"invalid (cert|chain)", r"self[- ]signed", r"chain (is )?broken"):
            return False
        if yes(r"(cert(ificate)?|chain|tls|ssl)[^.]{0,40}\b(valid|trusted|ok)\b"):
            return True
    if claim == "tls.expired":
        if yes(r"\bexpired\b") and not yes(r"not expired"):
            return True
        if yes(r"not expired", r"valid until", r"expires (on|in)"):
            return False
    if claim == "tls.hostname_match":
        if yes(r"hostname (mismatch|does not match|doesn'?t match)", r"wrong host"):
            return False
        if yes(r"hostname (match|matches|ok)"):
            return True
    if claim == "dns.dnssec":
        if yes(r"dnssec[^.]{0,30}\b(not|no|disabled|absent|missing)\b", r"no dnssec"):
            return False
        if yes(r"dnssec[^.]{0,30}\b(enabled|present|signed|yes|ok)\b"):
            return True
    if claim == "dns.resolves":
        if yes(r"nxdomain", r"does not resolve", r"no (a )?records? found", r"not found"):
            return False
        if yes(r"resolves", r"a record"):
            return True
    if claim == "email.spf":
        if yes(r"spf[^.]{0,30}\b(missing|absent|not found|none|no)\b", r"no spf"):
            return False
        if yes(r"spf[^.]{0,30}\b(present|found|configured|ok|valid)\b", r"v=spf1"):
            return True
    if claim == "email.dmarc":
        if yes(r"dmarc[^.]{0,30}\b(missing|absent|not found|none|no)\b", r"no dmarc"):
            return False
        if yes(r"dmarc[^.]{0,30}\b(present|found|configured|ok|valid)\b", r"v=dmarc1"):
            return True
    if claim == "http.status":
        m = re.search(r"\b(status(?: code)?|http)\D{0,10}(\d{3})\b", t)
        if m:
            return int(m.group(2))
    if claim == "dns.a_record":
        ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", raw)
        return sorted(set(ips)) or None
    if claim == "dns.mx":
        mx = re.findall(r"\b([a-z0-9.-]+\.(?:com|net|org|io|ai|co|dev|google))\.?\b(?=[^\n]*mx|)", t)
        return sorted(set(mx)) or None
    return None


def extract(raw: str, claim: str) -> Optional[Any]:
    data = _try_json(raw)
    if data is not None:
        for p in CLAIM_PATHS.get(claim, []):
            v = _walk(data, p)
            if v is not None:
                return v
    return _regex_extract(raw, claim)
