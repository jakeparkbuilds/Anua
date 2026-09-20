"""SELF claims vs CAPABILITY claims.

"Exposes an A2A endpoint at https://impact.webmesh.ai" is a claim about the agent's own
infrastructure. We check it ourselves. Asking the agent about it was scoring a
domain-takedown risk scorer 26/100 with nine HIGH failures for not being a TLS tool.

Also covers the two bugs found in the same run: a truncated generator reply losing every
test, and a behavior score computed from zero behavioural evidence.
"""
from __future__ import annotations
import json
import pytest

from bench.generator.graph import selfclaim_assertions, validate_tests, own_hosts, _NEGATIVE_SENSE
from bench.assertions.runner import Runner
from bench.report.builder import build
from bench.models import Assertion, AgentCards, AgentIdentity, Kind, Severity, CoverageReport, CapabilityClaim
from bench.llm import parse_json, salvage_list, strip_fences

HOST = "impact.webmesh.ai"
CARDS = {"agent_card": {"name": "Domain Impact Analyzer", "url": "https://impact.webmesh.ai"},
         "trust_card": {"endpoints": [{"agentUrl": "https://impact.webmesh.ai"}],
                        "transparencyLog": "https://transparency.ans.godaddy.com/v1/agents/f406507d"},
         "registry_entry": {}}
WEIGHTS = {"oracle": .7, "selfclaim": .25, "schema": .2, "quality": .1}


class _NeverCalled:
    def send(self, prompt):  # pragma: no cover
        raise AssertionError("a self-claim must never call the agent")


# --- what counts as the agent's own infrastructure --------------------------
def test_own_hosts_includes_parent_zone_and_published_urls():
    mine = own_hosts(CARDS, HOST)
    assert HOST in mine
    assert "webmesh.ai" in mine                          # DNSSEC lives on the zone apex
    assert "transparency.ans.godaddy.com" in mine        # a URL the agent itself publishes
    assert "expired.badssl.com" not in mine


# --- self-claims are checked directly ---------------------------------------
def test_selfclaim_targets_the_agent_and_never_the_agent_s_opinion(monkeypatch):
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": "Exposes an A2A endpoint at https://impact.webmesh.ai",
                          "oracle_keys": ["web.fetchable"], "target": "https://impact.webmesh.ai"}]}
    tests, _ = selfclaim_assertions(st)
    assert len(tests) == 1
    a = tests[0]
    assert a.kind is Kind.SELFCLAIM
    assert a.input == {"url": "https://impact.webmesh.ai"}
    assert a.expected_override is True          # the card claims it IS reachable

    # the oracle says otherwise -> the agent's own card is wrong, and we say so
    monkeypatch.setattr("bench.assertions.runner.compute", lambda claim, target: False)
    got = Runner(_NeverCalled(), AgentCards()).run(a)
    assert got.passed is False
    assert got.expected is True and got.actual is False
    assert "agent not called" in got.evidence


def test_negative_sense_oracle_is_not_expected_true():
    """No agent claims its own certificate is expired."""
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": "Serves a valid certificate", "oracle_keys": ["tls.expired"],
                          "target": HOST}]}
    tests, _ = selfclaim_assertions(st)
    assert "tls.expired" in _NEGATIVE_SENSE
    assert tests[0].expected_override is False


def test_selfclaim_cannot_be_pointed_at_a_third_party():
    """The guard that stops a 'negative control' against badssl.com becoming a self-check."""
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": "Serves a valid certificate", "oracle_keys": ["tls.chain_valid"],
                          "target": "expired.badssl.com"}]}
    tests, notes = selfclaim_assertions(st)
    # falls back to the agent's own host rather than probing someone else's
    assert [a.input for a in tests] == [{"domain": HOST}]


def test_selfclaim_skips_oracles_with_no_claimed_value():
    """http.status returns an int. "Exposes an endpoint" does not say WHICH int, and a
    POST-only A2A endpoint answering 405 to our GET is not a lie."""
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": "Exposes an endpoint", "oracle_keys": ["http.status"], "target": HOST}]}
    tests, notes = selfclaim_assertions(st)
    assert tests == []
    assert any("no claimed value" in n for n in notes)


# --- capability tests may not be aimed at the agent itself -------------------
def test_capability_test_aimed_at_own_host_is_rejected():
    st = {"host": HOST, "cards": CARDS, "existing_ids": [], "max_n": 30, "selfclaims": [], "notes": [],
          "proposals": [
              {"id": "ask-about-self", "claim": "tls.chain_valid", "target": HOST, "comparator": "bool",
               "severity": "HIGH", "prompt": "Is impact.webmesh.ai's cert valid?"},
              {"id": "ask-about-badssl", "claim": "tls.chain_valid", "target": "expired.badssl.com",
               "comparator": "bool", "severity": "HIGH", "prompt": "Is expired.badssl.com's cert valid?"}]}
    out = validate_tests(st)
    ids = [a["id"] for a in out["assertions"]]
    assert ids == ["ask-about-badssl"]
    assert any("own host" in n for n in out["notes"])


# --- a truncated reply must not lose the tests that did arrive ---------------
def test_truncated_generator_reply_is_salvaged():
    truncated = ('```json\n[\n  {"id": "a", "claim": "dns.a_record", "target": "github.com"},\n'
                 '  {"id": "b", "claim": "tls.expired", "target": "expired.badssl.com"},\n'
                 '  {"id": "c", "claim": "web.ti')
    got = parse_json(truncated, "list")
    assert [g["id"] for g in got] == ["a", "b"]


def test_unterminated_fence_is_stripped():
    assert strip_fences('```json\n{"a": 1}').startswith("{")
    assert salvage_list("no array here") == []


def test_clean_json_still_parses_exactly():
    assert parse_json('```json\n[{"a": 1}]\n```', "list") == [{"a": 1}]


# --- a score needs behavioural evidence --------------------------------------
def _report(assertions, coverage=None):
    return build("a", "h", "live", AgentIdentity(host="h", verified=True), AgentCards(),
                 assertions, WEIGHTS, [], coverage=coverage, suite="generated")


def test_self_claims_and_schema_alone_still_score_but_say_so():
    """Six checks of the agent's own DNS/TLS plus one schema check are measurements. They
    score — renormalised over the pools present — and the confidence says the number rests
    on self-claims, not on capability tests."""
    cov = CoverageReport(claims_found=22, verifiable=6, schema_only=4, unverifiable=12,
                         capability_claims=16, self_claims=6, capability_tests=0, selfclaim_tests=6)
    passing_self = [Assertion(id=f"s{i}", kind=Kind.SELFCLAIM, claim="web.fetchable",
                              input={"url": "https://x.com"}, passed=True, score=100.0) for i in range(6)]
    schema = [Assertion(id="sc", kind=Kind.SCHEMA, claim="card.skills_nonempty", input={}, passed=True)]
    r = _report(passing_self + schema, cov)
    assert r.score_status == "OK" and r.behavior_score == 100      # (.25×1 + .2×1) / .45
    assert r.summary["score_confidence"] == "medium" and r.summary["graded_total"] == 7
    assert "no graded capability test" in r.score_basis and "unverifiable" in r.score_basis
    assert "score rests on self-claims and schema" in r.explanation


def test_two_graded_assertions_are_not_a_score():
    a = [Assertion(id="o", kind=Kind.ORACLE, claim="tls.chain_valid", input={"domain": "d"}, passed=True),
         Assertion(id="s", kind=Kind.SELFCLAIM, claim="web.fetchable", input={"url": "u"}, passed=True)]
    r = _report(a)
    assert r.score_status == "INSUFFICIENT_COVERAGE" and r.behavior_score is None
    assert "only 2 assertion(s) graded" in r.score_basis and r.summary["score_confidence"] == "none"


def test_three_graded_assertions_score_with_low_confidence():
    a = [Assertion(id="o", kind=Kind.ORACLE, claim="tls.chain_valid", input={"domain": "d"}, passed=True),
         Assertion(id="s", kind=Kind.SELFCLAIM, claim="web.fetchable", input={"url": "u"}, passed=True),
         Assertion(id="c", kind=Kind.SCHEMA, claim="card.skills_nonempty", input={}, passed=False)]
    r = _report(a)
    assert r.score_status == "OK" and r.summary["score_confidence"] == "low"
    assert r.behavior_score == round(100 * (.7 * 1 + .25 * 1 + .2 * 0) / (.7 + .25 + .2))
    assert "1/1 capability" in r.score_basis and "1/1 self-claim" in r.score_basis and "0/1 schema" in r.score_basis


def test_ungraded_capability_assertions_do_not_count_as_evidence():
    """A paywalled agent leaves everything passed=None. That is not a score either."""
    a = [Assertion(id="o", kind=Kind.ORACLE, claim="tls.chain_valid", input={"domain": "d"},
                   passed=None, error="agent unavailable: HTTP 402 payment required")]
    r = _report(a)
    assert r.score_status == "INSUFFICIENT_COVERAGE"
    assert "refused to serve us" in r.score_basis and "HTTP 402" in r.score_basis


def test_endpoint_claims_use_endpoint_live_not_fetchable():
    """dnsdoc's MCP endpoint answers an HTML GET with 406. It is serving; it is not a
    document. Grading "exposes an MCP endpoint" with `fetchable` called that a lie."""
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": "Exposes an MCP server at https://impact.webmesh.ai/mcp",
                          "oracle_keys": ["web.fetchable"], "target": "https://impact.webmesh.ai/mcp"}]}
    tests, _ = selfclaim_assertions(st)
    assert tests[0].claim == "web.endpoint_live"
    assert tests[0].input == {"url": "https://impact.webmesh.ai/mcp"}


def test_two_urls_on_one_host_get_distinct_ids():
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": "a", "oracle_keys": ["web.fetchable"], "target": "https://impact.webmesh.ai/mcp"},
                         {"claim_text": "b", "oracle_keys": ["web.fetchable"],
                          "target": "https://impact.webmesh.ai/.well-known/ans/trust-card.json"}]}
    tests, _ = selfclaim_assertions(st)
    assert len({a.id for a in tests}) == 2, [a.id for a in tests]


def test_bare_third_party_origin_is_not_a_self_check():
    """"Logs identity to a transparency log" became https://transparency.ans.godaddy.com,
    whose root 404s. That is the model paraphrasing, not the agent lying."""
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": "Logs identity to a transparency log",
                          "oracle_keys": ["web.fetchable"], "target": "https://transparency.ans.godaddy.com"}]}
    tests, notes = selfclaim_assertions(st)
    assert tests == []
    assert any("third-party origin" in n for n in notes)


def test_third_party_url_with_a_path_is_kept():
    """The agent's actual transparency entry IS its own claim to check."""
    url = "https://transparency.ans.godaddy.com/v1/agents/f406507d"
    st = {"host": HOST, "cards": CARDS, "existing_ids": [],
          "selfclaims": [{"claim_text": f"Publishes a transparency log entry at {url}",
                          "oracle_keys": ["web.fetchable"], "target": url}]}
    tests, _ = selfclaim_assertions(st)
    assert [a.input for a in tests] == [{"url": url}]
