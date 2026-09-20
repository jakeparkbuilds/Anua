"""Audit round 1: every path where a verdict could be wrong.

A wrong PASS is worse than a wrong FAIL, and both are worse than an honest SKIPPED.
Each test here names the wrong verdict it prevents."""
import json
import httpx
import pytest
from bench.assertions.compare import compare, Ungradable
from bench.assertions.runner import Runner
from bench.agent import adapter, card as card_mod
from bench.agent.adapter import LLMExtractor, ExtractionUnavailable
from bench.agent.transport import A2ATransport, AgentUnavailable, _post
from bench.models import Assertion, Kind, AgentCards, AgentIdentity, Severity
from bench.oracles.registry import SPECS
from bench.report.builder import build


# ---- comparators -----------------------------------------------------------------------
def test_a_string_is_one_set_member_not_a_bag_of_characters():
    # set(map(str, "ns1.example.com")) was {'n','s','1','.',...}: zero overlap, a FAIL
    ok, ev = compare("set_overlap", ["ns1.example.com", "ns2.example.com"], "ns1.example.com")
    assert ok
    ok, _ = compare("set_eq", ["a.com", "b.com"], "a.com, b.com")
    assert ok


def test_set_overlap_needs_half_of_the_agents_list_to_be_real():
    # one right nameserver out of three listed is not a pass
    ok, ev = compare("set_overlap", ["ns1.real.net", "ns2.real.net"], ["ns1.real.net", "x.fake", "y.fake"])
    assert not ok and "≥½" in ev
    ok, _ = compare("set_overlap", ["ns1.real.net", "ns2.real.net"], ["ns1.real.net", "x.fake"])
    assert ok
    ok, _ = compare("set_overlap", ["104.16.132.229", "104.16.133.229"], ["104.16.132.229"])
    assert ok                                             # anycast: a subset is fine
    ok, _ = compare("set_overlap", [], [])
    assert ok                                             # both say none


def test_contains_with_an_empty_oracle_value_is_ungradable_not_a_free_pass():
    # '' in anything is True: an agent saying "meta description: banana" would PASS
    with pytest.raises(Ungradable):
        compare("contains", "", "banana")


def test_unknown_comparator_is_ungradable():
    with pytest.raises(Ungradable):
        compare("fuzzy", 1, 1)


# ---- runner: our failures are never the agent's ---------------------------------------
class _Say:
    def __init__(self, text): self.text = text
    def send(self, prompt): return self.text, 5


def _oracle(claim="tls.chain_valid", **kw):
    return Assertion(id="t", kind=Kind.ORACLE, claim=claim, input={"domain": "example.com"}, oracle=claim,
                     comparator="bool", **kw)


def test_runner_bug_is_skipped_not_failed(monkeypatch):
    def boom(claim, target): raise ZeroDivisionError("our bug")
    monkeypatch.setattr("bench.assertions.runner.compute", boom)
    out = Runner(_Say("{}"), AgentCards()).run(_oracle())
    assert out.passed is None and "runner error (ours)" in out.evidence


def test_reader_failure_is_skipped_not_failed(monkeypatch):
    monkeypatch.setattr("bench.assertions.runner.compute", lambda c, t: True)

    class BrokenLLM:
        def complete(self, s, h): raise RuntimeError("429 rate limited")
    out = Runner(_Say("The certificate looks fine."), AgentCards(), LLMExtractor(BrokenLLM(), SPECS)).run(_oracle())
    assert out.passed is None and "reader unavailable" in out.evidence


def test_agent_not_stating_the_value_is_a_fail_that_says_so(monkeypatch):
    monkeypatch.setattr("bench.assertions.runner.compute", lambda c, t: True)

    class NullLLM:
        def complete(self, s, h): return json.dumps({"dns.dnssec|example.com": {"value": None, "quote": None}})
    out = Runner(_Say("A records resolve. SPF present."), AgentCards(), LLMExtractor(NullLLM(), SPECS)).run(_oracle("dns.dnssec"))
    assert out.passed is False
    assert "never mentions it" in out.evidence and "dnssec" in out.evidence


def test_regex_reader_is_not_used_when_an_llm_reader_exists(monkeypatch):
    # "DNSSEC not evaluated" pattern-matched to False and FAILED a signed zone
    monkeypatch.setattr("bench.assertions.runner.compute", lambda c, t: True)

    class NullLLM:
        def complete(self, s, h): return json.dumps({"dns.dnssec|example.com": {"value": None, "quote": None}})
    raw = "DNSSEC not evaluated in this run."
    with_llm = Runner(_Say(raw), AgentCards(), LLMExtractor(NullLLM(), SPECS)).run(_oracle("dns.dnssec"))
    assert with_llm.actual is None                        # LLM said "not stated"; regex never consulted
    without = Runner(_Say(raw), AgentCards()).run(_oracle("dns.dnssec"))
    assert without.actual is False and "pattern match" in without.evidence   # last resort, and labelled


def test_pinned_expectation_is_checked_against_the_live_oracle(monkeypatch):
    # expired.badssl.com renewed its cert once. A pinned "False" would then fail an
    # agent for being right.
    monkeypatch.setattr("bench.assertions.runner.compute", lambda c, t: True)
    out = Runner(_Say('{"tls": {"valid": true}}'), AgentCards()).run(_oracle(expected_override=False))
    assert out.passed is None and "control target changed" in out.evidence
    monkeypatch.setattr("bench.assertions.runner.compute", lambda c, t: False)
    out = Runner(_Say('{"tls": {"valid": true}}'), AgentCards()).run(_oracle(expected_override=False))
    assert out.passed is False                            # control intact: graded normally


def test_consistency_with_an_unreadable_answer_is_skipped():
    a = Assertion(id="c", kind=Kind.CONSISTENCY, claim="tls.chain_valid", input={"domain": "example.com"}, comparator="eq")
    out = Runner(_Say("no structured answer"), AgentCards()).run(a)
    assert out.passed is None and "consistency" in out.evidence


def test_skills_check_is_skipped_when_the_card_could_not_be_read():
    a = Assertion(id="s", kind=Kind.SCHEMA, claim="card.skills_nonempty", input={}, comparator="bool", expected_override=True)
    out = Runner(_Say(""), AgentCards(card_error="agent card unreachable: ConnectError")).run(a)
    assert out.passed is None and "unknown, not absent" in out.evidence


def test_transient_agent_failure_does_not_block_the_rest_of_the_run(monkeypatch):
    monkeypatch.setattr("bench.assertions.runner.compute", lambda c, t: True)
    n = {"calls": 0}

    class Flaky:
        def send(self, prompt):
            n["calls"] += 1
            if n["calls"] == 1:
                raise AgentUnavailable("HTTP 502 Bad Gateway", permanent=False)
            return '{"tls": {"valid": true}}', 5
    r = Runner(Flaky(), AgentCards())
    first = r.run(_oracle())
    a2 = _oracle(); a2.input = {"domain": "other.com"}
    second = r.run(a2)
    assert first.passed is None and "NO VERDICT" in first.evidence
    assert second.passed is True                           # we kept calling
    assert n["calls"] == 2


# ---- transport: 5xx and timeouts are unavailability, with one retry --------------------
class _Client:
    def __init__(self, responses): self.responses = list(responses); self.timeout = httpx.Timeout(30)
    def post(self, url, **kw):
        r = self.responses.pop(0)
        if isinstance(r, Exception): raise r
        return r


def _resp(code): return httpx.Response(code, request=httpx.Request("POST", "https://a/x"), json={"result": {"parts": [{"text": "ok"}]}})


def test_502_then_success_is_a_success(monkeypatch):
    monkeypatch.setattr("bench.agent.transport.time.sleep", lambda s: None)
    r = _post(_Client([_resp(502), _resp(200)]), "https://a/x", json={})
    assert r.status_code == 200


def test_502_twice_is_agent_unavailable_not_a_wrong_answer(monkeypatch):
    monkeypatch.setattr("bench.agent.transport.time.sleep", lambda s: None)
    with pytest.raises(AgentUnavailable, match="HTTP 50") as e:
        _post(_Client([_resp(502), _resp(503)]), "https://a/x", json={})
    assert e.value.permanent is False


def test_read_timeout_is_agent_unavailable(monkeypatch):
    monkeypatch.setattr("bench.agent.transport.time.sleep", lambda s: None)
    with pytest.raises(AgentUnavailable, match="timed out"):
        _post(_Client([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")]), "https://a/x", json={})


def test_402_is_permanent_for_the_run():
    tr = A2ATransport("https://a/x", "message/send", 5)

    class C:
        timeout = httpx.Timeout(5)
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, **kw): return httpx.Response(402, request=httpx.Request("POST", url))
    import bench.agent.transport as t
    orig = t.httpx.Client
    t.httpx.Client = C
    try:
        with pytest.raises(AgentUnavailable) as e:
            tr.send("hi")
        assert e.value.permanent is True
    finally:
        t.httpx.Client = orig


# ---- the LLM reader: quotes must carry the value; provenance never leaks ---------------
def test_quote_must_contain_the_value_it_is_cited_for():
    raw = "Nameservers: ns1.real.net and ns2.real.net. Registrar: MarkMonitor. Created 2015-04-07."

    class Liar:
        def complete(self, s, h):
            return json.dumps({
                "whois.registrar|x.com": {"value": "GoDaddy", "quote": "Registrar: MarkMonitor."},        # quote exists, value not in it
                "whois.nameservers|x.com": {"value": ["ns1.real.net", "ns9.real.net"], "quote": "Nameservers: ns1.real.net and ns2.real.net."},
                "whois.created|x.com": {"value": "2015-04-07", "quote": "Created 2015-04-07."},
            })
    ex = LLMExtractor(Liar(), SPECS)
    assert ex.extract(raw, "whois.registrar", "x.com") is None
    assert ex.extract(raw, "whois.nameservers", "x.com") is None
    assert ex.extract(raw, "whois.created", "x.com") == "2015-04-07"


def test_reader_never_sees_where_we_get_the_truth():
    # told "nameservers from RDAP", the model refused to read nameservers reported from DNS
    seen = {}

    class Spy:
        def complete(self, s, h): seen["h"] = h; return "{}"
    LLMExtractor(Spy(), SPECS).prime("x", [("whois.nameservers", "a.com"), ("whois.registrar", "a.com")])
    assert "RDAP" not in seen["h"] and "rdap" not in seen["h"]


def test_reader_exception_raises_extraction_unavailable():
    class Broken:
        def complete(self, s, h): raise RuntimeError("overloaded")
    with pytest.raises(ExtractionUnavailable):
        LLMExtractor(Broken(), SPECS).prime("x", [("dns.dnssec", "a.com")])


def test_mx_is_no_longer_every_hostname_in_the_text():
    assert adapter.extract("MX for google.com: smtp.google.com. Also see example.net.", "dns.mx") is None


def test_chain_validity_is_not_inferred_from_certificate_fields_alone():
    # cert details present but no verified-handshake flag: the agent has not said "valid"
    raw = json.dumps({"evidence": {"tls": {"issuer": "R11", "not_after": "2027-01-01"}}})
    assert adapter.extract(raw, "tls.chain_valid") is None
    raw = json.dumps({"evidence": {"tls": {"reachable": True, "issuer": "R11"}}})
    assert adapter.extract(raw, "tls.chain_valid") is True


# ---- generator guards ------------------------------------------------------------------
def test_generator_cannot_be_told_what_a_self_claim_should_measure():
    from bench.generator.graph import selfclaim_assertions
    st = {"host": "agent.example", "cards": {"agent_card": {"url": "https://agent.example/a2a"}},
          "existing_ids": [], "selfclaims": [{"claim_text": "Exposes an A2A endpoint at https://agent.example/a2a",
                                              "oracle_keys": ["web.endpoint_live"], "target": "https://agent.example/a2a",
                                              "expected": False}]}   # a card that says "expect false" gets nothing
    out, _ = selfclaim_assertions(st)
    assert out and out[0].expected_override is True


def test_capability_targets_are_bounded_to_the_allowed_host_set():
    from bench.generator.graph import validate_tests
    st = {"host": "agent.example", "cards": {}, "existing_ids": [], "max_n": 10, "selfclaims": [], "notes": [],
          "proposals": [{"id": "ok", "claim": "tls.chain_valid", "target": "expired.badssl.com", "prompt": "x expired.badssl.com"},
                        {"id": "scan", "claim": "tls.chain_valid", "target": "some-small-business.com", "prompt": "x"}]}
    out = validate_tests(st)
    assert [a["id"] for a in out["assertions"]] == ["ok"]
    assert any("outside the allowed" in n for n in out["notes"])


# ---- cards and registry: garbage in, no traceback out ----------------------------------
def test_card_that_is_html_or_a_list_or_huge_is_no_card(monkeypatch):
    class R:
        def __init__(self, content, ct="text/html"):
            self.content = content; self.headers = {"content-type": ct}
        def raise_for_status(self): pass
        def json(self):
            return json.loads(self.content)

    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return C.resp
    monkeypatch.setattr(card_mod.httpx, "Client", C)
    for body in (b"<html>not json</html>", b"[1,2,3]", b"{" + b" " * 2_100_000 + b"}"):
        C.resp = R(body)
        cards = card_mod.fetch("h.example", 5, live=True, registry_entry={})
        assert cards.agent_card == {} and cards.card_error


def test_registry_outage_is_not_not_registered(monkeypatch):
    from bench.ans.registry import Registry
    import bench.ans.registry as regmod
    def down(url, timeout, params=None): raise httpx.ConnectError("down")
    monkeypatch.setattr(regmod, "_get", down)
    reg = Registry("https://s", "https://t", 5, live=True)
    assert reg.find_by_host("x.example") is None and "registry search failed" in reg.last_error
    assert reg.tl_entry("abc") is None


def test_registry_picks_the_active_highest_version_when_a_host_has_several(monkeypatch):
    from bench.ans.registry import Registry
    import bench.ans.registry as regmod
    hits = [{"agentHost": "x.example", "agentVersion": "v1.0.9", "lifecycle": {"status": "ACTIVE"}},
            {"agentHost": "x.example", "agentVersion": "v1.0.10", "lifecycle": {"status": "ACTIVE"}},
            {"agentHost": "x.example", "agentVersion": "v2.0.0", "lifecycle": {"status": "REVOKED"}}]
    monkeypatch.setattr(regmod, "_get", lambda url, timeout, params=None: {"items": hits})
    assert Registry("https://s", "https://t", 5, live=True).find_by_host("x.example")["agentVersion"] == "v1.0.10"


# ---- score integrity ---------------------------------------------------------------------
def test_score_carries_its_evidence_count_and_confidence():
    def o(i, passed):
        return Assertion(id=f"o{i}", kind=Kind.ORACLE, claim="tls.expired", input={}, passed=passed)
    r = build("a", "h", "live", AgentIdentity(host="h"), AgentCards(), [o(1, True), o(2, True), o(3, False)],
              {"oracle": .7, "selfclaim": .25, "schema": .2, "quality": .1}, [])
    assert r.summary["evidence_n"] == 3 and r.summary["score_confidence"] == "low"
    assert "3 graded capability test(s) — low confidence" in r.score_basis
    assert r.behavior_score == round(100 * (2 / 3))   # no quality item ran: it is not in the blend


def test_ungraded_assertions_are_in_no_denominator():
    def o(i, passed):
        return Assertion(id=f"o{i}", kind=Kind.ORACLE, claim="tls.expired", input={}, passed=passed)
    graded = [o(1, True), o(2, False)]
    r1 = build("a", "h", "live", AgentIdentity(host="h"), AgentCards(), graded, {}, [])
    r2 = build("a", "h", "live", AgentIdentity(host="h"), AgentCards(), graded + [o(3, None), o(4, None), o(5, None)], {}, [])
    assert r1.behavior_score == r2.behavior_score and r2.summary["skipped"] == 3


def test_dnsdoc_evidence_expiry_and_issuer_are_read_structurally():
    raw = json.dumps({"evidence": {"tls": {"reachable": True, "issuer_cn": "WE1", "not_after": "Dec  4 23:29:33 2026 GMT"}}})
    assert adapter.extract(raw, "tls.issuer") == "WE1"
    assert compare("date_close", "2026-12-04", adapter.extract(raw, "tls.not_after"))[0]
