"""Hand-written assertions. These ALWAYS run. Generated ones are additive.

Design: negatives from badssl.com give unambiguous ground truth. Positives use
stable, well-known domains. Every oracle assertion's `expected` is computed live
at run time (so it can't rot), except where expected_override pins a known truth.
"""
from __future__ import annotations
from ..models import Assertion, Kind, Severity

H, M, L = Severity.HIGH, Severity.MEDIUM, Severity.LOW


def _o(id, claim, domain, sev=M, comparator="eq", override=None, rationale=""):
    return Assertion(id=id, kind=Kind.ORACLE, claim=claim, input={"domain": domain},
                     oracle=claim, comparator=comparator, expected_override=override,
                     severity=sev, rationale=rationale)


ORACLE_ASSERTIONS: list[Assertion] = [
    # --- TLS negatives (the headline: provable competence failures) ---------
    _o("tls-expired-chain",   "tls.chain_valid",    "expired.badssl.com",        H, "bool", False,
       "Expired cert must be reported as NOT valid"),
    _o("tls-expired-flag",    "tls.expired",        "expired.badssl.com",        H, "bool", True,
       "Must explicitly flag expiry"),
    _o("tls-selfsigned",      "tls.chain_valid",    "self-signed.badssl.com",    H, "bool", False,
       "Self-signed must be untrusted"),
    _o("tls-untrusted-root",  "tls.chain_valid",    "untrusted-root.badssl.com", H, "bool", False,
       "Unknown root must be untrusted"),
    _o("tls-wrong-host",      "tls.hostname_match", "wrong.host.badssl.com",     H, "bool", False,
       "Hostname mismatch must be detected"),
    _o("tls-wrong-host-chain","tls.chain_valid",    "wrong.host.badssl.com",     M, "bool", False,
       "Overall validity must be false on hostname mismatch"),

    # --- TLS positives --------------------------------------------------------
    _o("tls-good-cloudflare", "tls.chain_valid",    "cloudflare.com",            M, "bool",
       rationale="Well-configured site must be reported valid"),
    _o("tls-good-google",     "tls.chain_valid",    "google.com",                M, "bool"),
    _o("tls-not-expired",     "tls.expired",        "cloudflare.com",            L, "bool"),

    # --- DNS ------------------------------------------------------------------
    _o("dns-dnssec-yes",      "dns.dnssec",         "cloudflare.com",            M, "bool",
       rationale="cloudflare.com is DNSSEC-signed"),
    _o("dns-dnssec-no",       "dns.dnssec",         "badssl.com",                M, "bool"),
    _o("dns-resolves-yes",    "dns.resolves",       "example.com",               L, "bool"),
    _o("dns-nxdomain",        "dns.resolves",       "this-domain-does-not-exist-zz.invalid", H, "bool", False,
       "Must not hallucinate records for a nonexistent domain"),
    _o("dns-a-records",       "dns.a_record",       "cloudflare.com",            L, "set_overlap",
       rationale="Reported A records should overlap ours (anycast may differ)"),
    _o("dns-mx-google",       "dns.mx",             "google.com",                L, "set_overlap"),

    # --- HTTP -----------------------------------------------------------------
    _o("http-https-ok",       "http.https_ok",      "example.com",               L, "bool",
       rationale="Reachable over verified HTTPS"),
    _o("http-https-bad",      "http.https_ok",      "expired.badssl.com",        M, "bool", False,
       rationale="Verified HTTPS must fail on expired cert"),

    # --- Email ----------------------------------------------------------------
    _o("email-spf-google",    "email.spf",          "google.com",                M, "bool"),
    _o("email-dmarc-google",  "email.dmarc",        "google.com",                M, "bool"),
    _o("email-spf-none",      "email.spf",          "expired.badssl.com",        L, "bool"),
]

SCHEMA_ASSERTIONS: list[Assertion] = [
    Assertion(id="schema-endpoint-reachable", kind=Kind.SCHEMA, claim="endpoint.reachable",
              input={"domain": "example.com"}, comparator="nonempty", severity=H,
              rationale="Agent must respond at all"),
    Assertion(id="schema-declared-skills-exist", kind=Kind.SCHEMA, claim="card.skills_nonempty",
              input={}, comparator="bool", expected_override=True, severity=M,
              rationale="Agent card must declare at least one skill"),
    Assertion(id="schema-card-vs-trustcard", kind=Kind.SCHEMA, claim="drift.card_vs_trust",
              input={}, comparator="set_eq", severity=M,
              rationale="Functions in trust card should match skills in agent card (claim drift)"),
    Assertion(id="schema-protocol-a2a", kind=Kind.SCHEMA, claim="card.protocol_declared",
              input={"protocol": "a2a"}, comparator="bool", expected_override=True, severity=L),
]

CONSISTENCY_ASSERTIONS: list[Assertion] = [
    Assertion(id="consistency-repeat", kind=Kind.CONSISTENCY, claim="tls.chain_valid",
              input={"domain": "cloudflare.com"}, comparator="eq", severity=L,
              rationale="Same input twice must give same verdict"),
]

QUALITY_ASSERTIONS: list[Assertion] = [
    Assertion(id="quality-explainability", kind=Kind.QUALITY, claim="quality.explainability",
              input={"domain": "expired.badssl.com"}, comparator="nonempty", severity=Severity.INFO,
              rationale="Classical-ML scored; never affects pass/fail"),
]


def all_assertions() -> list[Assertion]:
    return [*ORACLE_ASSERTIONS, *SCHEMA_ASSERTIONS, *CONSISTENCY_ASSERTIONS, *QUALITY_ASSERTIONS]
