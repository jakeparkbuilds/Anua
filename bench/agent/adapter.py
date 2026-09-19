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
    "url":     "Analyze the page at {url}. Report everything you observe about it, with concrete "
               "values (title, meta description, headings, canonical, structured data, robots, "
               "sitemap, status, redirects). Respond in JSON if you can.",
}


def target_of(a: Assertion) -> str:
    """The thing the oracle is asked about: a URL if the test has one, else the domain."""
    return a.input.get("url") or a.input.get("domain") or ""


def build_prompt(a: Assertion) -> str:
    """Generated tests carry their own prompt (written from the agent's own examples).
    Hand-written ones use the legacy templates."""
    if a.prompt:
        return a.prompt
    if "url" in a.input:
        return _PROMPTS["url"].format(**a.input)
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


_TLS_OK_KEYS = ("not_after", "notAfter", "issuer", "subject", "san", "sans",
                "days_remaining", "expires", "expiry", "version", "cipher")
_STATUS_KEYS = ("status", "status_code", "https_status", "statusCode", "code")

def _bool(v):
    return v if isinstance(v, bool) else None

def _tls_ok(tls):
    if not tls or tls.get("error"):
        return False
    return tls.get("reachable") is True or any(k in tls for k in _TLS_OK_KEYS)

def _from_evidence(ev, claim):
    dns = ev.get("dns") if isinstance(ev.get("dns"), dict) else {}
    tls = ev.get("tls") if isinstance(ev.get("tls"), dict) else {}
    http = ev.get("http") if isinstance(ev.get("http"), dict) else {}
    tls_err = str(tls.get("error") or "").lower()

    if claim == "dns.a_record":
        return sorted(dns["A"]) if "A" in dns else None
    if claim == "dns.mx":
        if "MX" not in dns: return None
        return sorted({h for h in (str(x).split()[-1].rstrip(".").lower() for x in dns["MX"]) if h})
    if claim == "dns.resolves":
        if not dns: return None
        return any(dns.get(k) for k in ("A", "AAAA", "CNAME", "MX", "NS", "TXT"))
    if claim == "dns.dnssec":
        for src in (dns, ev):
            for k in ("dnssec", "DNSSEC"):
                if _bool(src.get(k)) is not None: return src[k]
        return None
    if claim == "email.spf":
        if not dns: return None
        return bool(dns.get("SPF")) or any("v=spf1" in str(t).lower() for t in dns.get("TXT", []))
    if claim == "email.dmarc":
        if not dns: return None
        return bool(dns.get("DMARC"))
    if claim == "tls.chain_valid":
        if _bool(tls.get("valid")) is not None: return tls["valid"]
        if tls_err and any(w in tls_err for w in ("certificate", "verify", "ssl")): return False
        return True if _tls_ok(tls) else None
    if claim == "tls.expired":
        if _bool(tls.get("expired")) is not None: return tls["expired"]
        if "expired" in tls_err: return True
        return False if _tls_ok(tls) else None
    if claim == "tls.hostname_match":
        if _bool(tls.get("hostname_matches_san")) is not None: return tls["hostname_matches_san"]
        if _bool(tls.get("hostname_match")) is not None: return tls["hostname_match"]
        if any(w in tls_err for w in ("hostname mismatch", "not valid for", "doesn't match")): return False
        return True if _tls_ok(tls) else None
    if claim in ("http.https_ok", "http.status"):
        status = next((http[k] for k in _STATUS_KEYS if isinstance(http.get(k), int)), None)
        if claim == "http.status": return status
        if _bool(http.get("https_ok")) is not None: return http["https_ok"]
        if http.get("https_error"): return False
        return True if status is not None else None
    return None

def extract(raw, claim):
    data = _try_json(raw)
    if isinstance(data, dict):
        ev = data.get("evidence")
        if isinstance(ev, dict):
            return _from_evidence(ev, claim)
        for p in CLAIM_PATHS.get(claim, []):
            v = _walk(data, p)
            if v is not None:
                return v
    return _regex_extract(raw, claim)

def https_timed_out(raw):
    data = _try_json(raw)
    if not isinstance(data, dict): return False
    ev = data.get("evidence")
    if not isinstance(ev, dict): return False
    http = ev.get("http")
    if not isinstance(http, dict): return False
    return "timed out" in str(http.get("https_error") or "").lower()


# ---------------------------------------------------------------------------
# LLM-assisted extraction: the model READS the agent's answer, it never judges it.
# Used only when the structured/regex paths above return None (i.e. for agents whose
# response shape we have never seen). Every extracted value must be backed by a verbatim
# quote that actually occurs in the response, or it is discarded — no invented values.
# ---------------------------------------------------------------------------
_EXTRACT_SYSTEM = """You are a strict parser. You are given an AI agent's raw response and a list of
claims, each with a key, a description of the value type, and the target it is about.
For each key, report the value THE AGENT STATED for that target — do NOT compute or guess
the true value, do NOT infer from silence. If the agent did not state it, return null.

Return STRICT JSON: {"<key>": {"value": <bool|int|string|list|null>, "quote": "<verbatim
substring of the response that states it, or null>"}}. Types: bool claims -> true/false;
int claims -> integer; str claims -> the string; list claims -> array of strings;
date claims -> "YYYY-MM-DD". JSON only, no prose."""


class LLMExtractor:
    def __init__(self, llm, specs: dict):
        self.llm, self.specs = llm, specs
        self._cache: dict[tuple[str, str], Any] = {}

    def prime(self, raw: str, claims: list[tuple[str, str]]) -> None:
        """One model call per distinct response: extract every (claim, target) at once."""
        todo = [(c, t) for c, t in claims if (hash(raw), f"{c}|{t}") not in self._cache]
        if not todo:
            return
        rows = []
        for c, t in todo:
            sp = self.specs.get(c)
            rows.append({"key": f"{c}|{t}", "claim": c, "target": t,
                         "type": sp.returns if sp else "str", "meaning": sp.description if sp else c})
        try:
            from ..llm import parse_json
            out = parse_json(self.llm.complete(_EXTRACT_SYSTEM,
                             f"Claims:\n{json.dumps(rows, indent=1)}\n\nAgent response:\n{raw[:12000]}"), "dict")
        except Exception:
            out = {}
        for c, t in todo:
            k = f"{c}|{t}"
            item = out.get(k) if isinstance(out, dict) else None
            val, quote = (item.get("value"), item.get("quote")) if isinstance(item, dict) else (None, None)
            if val is not None and not (isinstance(quote, str) and quote and " ".join(quote.split()).lower() in " ".join(raw.split()).lower()):
                val = None          # ungrounded: the model could not point at where the agent said it
            self._cache[(hash(raw), k)] = val

    def extract(self, raw: str, claim: str, target: str) -> Optional[Any]:
        key = (hash(raw), f"{claim}|{target}")
        if key not in self._cache:
            self.prime(raw, [(claim, target)])
        return self._cache.get(key)
