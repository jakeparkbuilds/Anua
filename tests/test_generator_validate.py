from bench.generator.graph import _validate

def test_validate_filters_bad_proposals():
    st = {"existing_ids": ["dup"], "max_n": 5, "proposals": [
        {"id": "ok-1", "claim": "tls.chain_valid", "domain": "revoked.badssl.com", "comparator": "bool", "severity": "HIGH"},
        {"id": "dup", "claim": "tls.chain_valid", "domain": "a.com"},              # duplicate id
        {"id": "bad-claim", "claim": "vibes.score", "domain": "a.com"},           # no oracle
        {"id": "bad-domain", "claim": "dns.resolves", "domain": "not a domain"},  # invalid
        {"id": "weird-sev", "claim": "dns.dnssec", "domain": "b.org", "severity": "CRITICAL"},
    ]}
    out = _validate(st)["assertions"]
    ids = [a["id"] for a in out]
    assert ids == ["ok-1", "weird-sev"]
    assert all(a["generated"] for a in out)
    assert out[1]["severity"] == "MEDIUM"   # unknown severity coerced
