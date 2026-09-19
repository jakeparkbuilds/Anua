"""The generator, offline. The LLM is faked; everything deterministic is exercised for real:
routing must REQUIRE the oracle key to exist, validation must reject bad proposals, and
coverage math must count what fell through."""
from __future__ import annotations
import json
import pytest

from bench.generator.graph import build_graph, validate_tests, report_coverage, generate
from bench.models import AgentCards, Verifiability


class FakeLLM:
    """Dispatches on which prompt is being asked, so one fake serves all three nodes."""
    def __init__(self, parse=None, route=None, gen=None):
        self.parse, self.route, self.gen = parse, route, gen
        self.calls: list[str] = []

    def complete(self, system: str, human: str) -> str:
        if system.startswith("You extract capability claims"):
            self.calls.append("parse"); return json.dumps(self.parse or [])
        if system.startswith("You match an AI agent's claims"):
            self.calls.append("route"); return json.dumps(self.route or [])
        if system.startswith("You design benchmark tests"):
            self.calls.append("gen"); return json.dumps(self.gen or [])
        raise AssertionError("unexpected prompt")


CARD = {"name": "SEO Analyzer", "description": "Analyzes a page for search discovery.",
        "url": "https://seo.example", "version": "1.0.0",
        "skills": [{"id": "analyze_seo", "description": "Reports title, meta description, canonical and JSON-LD; rewrites your bio.",
                    "examples": ["Analyze my portfolio"]}]}


def test_parse_and_route_classify_three_ways():
    llm = FakeLLM(
        parse=[{"claim_text": "Reports whether the page has a meta description", "source": "skill"},
               {"claim_text": "Rewrites your bio for AI search", "source": "skill"},
               {"claim_text": "Exposes an A2A JSON-RPC endpoint", "source": "trust_card"},
               {"claim_text": "Scores vibes 1-10", "source": "description"}],
        route=[{"claim_text": "Reports whether the page has a meta description", "oracle_keys": ["web.meta_description_present"], "kind": "oracle"},
               {"claim_text": "Rewrites your bio for AI search", "oracle_keys": [], "kind": "none", "reason": "generative, no ground truth"},
               {"claim_text": "Exposes an A2A JSON-RPC endpoint", "oracle_keys": [], "kind": "schema"},
               # the model hallucinates a key that does not exist -> must fall through, never VERIFIABLE
               {"claim_text": "Scores vibes 1-10", "oracle_keys": ["vibes.score"], "kind": "oracle"}],
        gen=[{"id": "meta-missing-example", "claim": "web.meta_description_present", "target": "https://example.com/",
              "comparator": "bool", "severity": "HIGH", "rationale": "example.com has no meta description",
              "prompt": "Analyze https://example.com/ and state whether it has a meta description. JSON please."}])
    tests, cov, notes = generate(AgentCards(agent_card=CARD), [], "fake-model", 30, llm=llm)
    assert llm.calls == ["parse", "route", "gen"]
    by = {c.claim_text: c for c in cov.claims}
    assert by["Reports whether the page has a meta description"].verifiability == Verifiability.VERIFIABLE
    assert by["Reports whether the page has a meta description"].oracle_key == "web.meta_description_present"
    assert by["Rewrites your bio for AI search"].verifiability == Verifiability.UNVERIFIABLE
    assert "generative" in by["Rewrites your bio for AI search"].reason
    assert by["Exposes an A2A JSON-RPC endpoint"].verifiability == Verifiability.SCHEMA_ONLY
    assert by["Scores vibes 1-10"].verifiability == Verifiability.UNVERIFIABLE
    assert "vibes.score" in by["Scores vibes 1-10"].reason
    assert (cov.claims_found, cov.verifiable, cov.schema_only, cov.unverifiable) == (4, 1, 1, 2)
    assert cov.coverage_ratio == 0.25
    assert cov.tests_generated == 1
    assert tests[0].generated and tests[0].input == {"url": "https://example.com/"} and tests[0].prompt.startswith("Analyze")


def test_schema_words_route_without_llm_help():
    llm = FakeLLM(parse=[{"claim_text": "Supports MCP streamable-http transport", "source": "description"}], route=[])
    _, cov, _ = generate(AgentCards(agent_card=CARD), [], "fake", 30, llm=llm)
    assert cov.claims[0].verifiability == Verifiability.SCHEMA_ONLY


def test_llm_garbage_never_raises():
    class Garbage:
        def complete(self, s, h): return "I'd rather not."
    tests, cov, notes = generate(AgentCards(agent_card=CARD), [], "fake", 30, llm=Garbage())
    assert tests == [] and cov.claims_found == 0
    assert any("parse_claims failed" in n for n in notes)


def test_validate_rejects_bad_proposals_and_coerces():
    st = {"existing_ids": ["dup"], "max_n": 5, "proposals": [
        {"id": "ok-1", "claim": "tls.chain_valid", "target": "revoked.badssl.com", "comparator": "bool", "severity": "HIGH",
         "prompt": "Diagnose revoked.badssl.com"},
        {"id": "dup", "claim": "tls.chain_valid", "target": "a.com"},                    # duplicate id
        {"id": "bad-claim", "claim": "vibes.score", "target": "a.com"},                  # no oracle
        {"id": "bad-domain", "claim": "dns.resolves", "target": "not a domain"},         # invalid
        {"id": "bad-url", "claim": "web.h1_count", "target": "ftp://x"},                 # invalid for url oracle
        {"id": "weird-sev", "claim": "web.h1_count", "target": "example.com", "severity": "CRITICAL", "comparator": "bool"},
        {"id": "bad-cmp", "claim": "dns.dnssec", "target": "b.org", "comparator": "fuzzy"},
        "not even a dict",
    ]}
    out = validate_tests(st)["assertions"]
    assert [a["id"] for a in out] == ["ok-1", "weird-sev", "bad-cmp"]
    assert all(a["generated"] for a in out)
    assert out[1]["severity"] == "MEDIUM"                    # unknown severity coerced
    assert out[1]["input"] == {"url": "https://example.com/"}  # domain -> url for a url oracle
    assert out[1]["comparator"] == "eq"                       # bool is wrong for an int oracle
    assert "example.com" in out[1]["prompt"]                  # prompt synthesised, names the target
    assert out[2]["comparator"] == "bool"                     # unsupported comparator -> oracle default
    notes = validate_tests(st)["notes"]
    assert any("rejected" in n for n in notes)


def test_validate_caps_and_shares_prompt_per_target():
    props = [{"id": f"t{i}", "claim": "web.h1_count" if i % 2 else "web.title_present", "target": "https://example.com/",
              "prompt": f"prompt {i} for https://example.com/"} for i in range(6)]
    out = validate_tests({"existing_ids": [], "max_n": 4, "proposals": props})["assertions"]
    assert len(out) == 4
    assert len({a["prompt"] for a in out}) == 1               # one agent call for all tests on a target


def test_coverage_math_with_no_claims():
    cov = report_coverage({"claims": [], "assertions": [], "notes": []})["coverage"]
    assert cov["claims_found"] == 0 and cov["coverage_ratio"] == 0.0


def test_generate_without_key_is_loud(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("bench.llm.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        generate(AgentCards(agent_card=CARD), [], "fake", 30)
