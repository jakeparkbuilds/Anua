import json
from bench.agent.adapter import extract, build_prompt
from bench.models import Assertion, Kind

def test_json_path_extraction():
    raw = json.dumps({"tls": {"valid": False, "expired": True}, "dns": {"dnssec": True}})
    assert extract(raw, "tls.chain_valid") is False
    assert extract(raw, "tls.expired") is True
    assert extract(raw, "dns.dnssec") is True

def test_json_in_prose_with_fences():
    raw = "Here you go:\n```json\n{\"http\": {\"status\": 404}}\n```"
    assert extract(raw, "http.status") == 404

def test_regex_negative_tls():
    assert extract("The certificate chain is not trusted (self-signed).", "tls.chain_valid") is False
    assert extract("Certificate expired on 2015-04-12.", "tls.expired") is True
    assert extract("Hostname mismatch: cert is for *.badssl.com", "tls.hostname_match") is False

def test_regex_positive_and_ip():
    assert extract("DNSSEC is enabled and the chain validates.", "dns.dnssec") is True
    assert extract("A records: 104.16.132.229, 104.16.133.229", "dns.a_record") == ["104.16.132.229", "104.16.133.229"]

def test_unknown_returns_none():
    assert extract("nothing relevant here", "tls.issuer") is None

def test_prompt_contains_domain():
    a = Assertion(id="x", kind=Kind.ORACLE, claim="tls.chain_valid", input={"domain": "example.com"})
    assert "example.com" in build_prompt(a)
