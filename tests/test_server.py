"""The server, offline. The pipeline is faked (it emits the same events the real one
does); everything else is real: the cache, in-flight joins, SSE framing, the A2A
envelope, and our own cards — which must survive our own drift check."""
from __future__ import annotations
import json
import threading
import time
import pytest
from fastapi.testclient import TestClient

from bench.models import Report, AgentIdentity, AgentCards, QualityScores, CoverageReport, Assertion, Kind, Severity
from bench.assertions.drift import divergences
from bench.server import app as app_mod, runs, cards
from bench.server.runs import RunStore, parse_agent, cached_steps


def _report(host: str, suite: str, score=80) -> Report:
    a = Assertion(id="dnssec-positive-cloudflare", kind=Kind.ORACLE, claim="dns.dnssec", oracle="dns.dnssec",
                  input={"domain": "cloudflare.com"}, severity=Severity.HIGH, generated=True,
                  expected=True, actual=None, passed=False, evidence="[dns.dnssec] extraction failed")
    return Report(agent=f"ans://v1.0.6.{host}", target_host=host, mode="mock",
                  identity=AgentIdentity(host=host, ans_name=f"ans://v1.0.6.{host}", ans_id="e1b4", registry_found=True,
                                         tl_entry_found=True, tl_server_fingerprint="ab" * 32, live_server_fingerprint="ab" * 32,
                                         fingerprint_match=True, verified=True, status="VERIFIED"),
                  cards=AgentCards(declared_skills=["diagnose"]), assertions=[a], quality=QualityScores(),
                  coverage=CoverageReport(claims_found=23, verifiable=17, unverifiable=4, schema_only=2, self_claims=8,
                                          capability_claims=15, capability_tests=30, selfclaim_tests=3, coverage_ratio=.74),
                  suite=suite, behavior_score=score, score_status="OK", score_basis="22/31 capability",
                  failures=[{"id": a.id, "kind": "oracle", "claim": a.claim, "input": a.input, "expected": True,
                             "actual": None, "severity": "HIGH", "oracle": a.oracle, "evidence": a.evidence,
                             "generated": True, "error": None}],
                  explanation="22/31 capability assertions passed")


class FakePipeline:
    def __init__(self, delay=0.05):
        self.calls, self.delay = [], delay

    def run(self, cfg, log=print, suite=None, on_stage=None, on_event=None, **kw):
        self.calls.append((cfg.target.host, suite))
        host = cfg.target.host
        on_stage("discover", "…"); on_event("discover", {"registry_found": True, "ans_name": f"ans://v1.0.6.{host}",
                                                        "skills": ["diagnose"], "card_name": "Doctor"})
        on_stage("identity", "…"); on_event("identity", _report(host, suite).identity.model_dump())
        on_stage("generate", "…"); on_event("claims", {"claims_found": 23})
        on_event("routed", {"claims_found": 23, "self": 8, "capability": 15, "verifiable": 17, "unverifiable": 4})
        on_event("tests", {"written": 12, "batch": 1, "batches": 2}); on_event("tests", {"written": 30, "batch": 2, "batches": 2})
        on_event("generated", {"capability_tests": 30, "selfclaim_tests": 3, "claims_found": 23, "verifiable": 17,
                               "unverifiable": 4, "schema_only": 2})
        on_stage("run", "33 assertions"); on_event("run", {"total": 33})
        time.sleep(self.delay)
        on_event("result", {"done": 33, "total": 33, "id": "x", "kind": "oracle", "passed": True, "severity": "HIGH"})
        on_event("report", {"behavior_score": 80, "score_status": "OK", "path": "out/x.json"})
        return _report(host, suite)


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake = FakePipeline()
    monkeypatch.setattr(runs, "pipeline", fake)
    store = RunStore(config_path="config.yaml", live=False, workers=3)
    store.out_dir = tmp_path
    monkeypatch.setattr(app_mod, "store", store)
    c = TestClient(app_mod.app)
    c.fake, c.store = fake, store
    return c


# --- our own cards ------------------------------------------------------------
def test_own_cards_are_specific_and_drift_free():
    ac, tc = cards.agent_card(), cards.trust_card()
    assert ac["name"] == "Anua Benchmarker" and ac["skills"][0]["id"] == "benchmark_agent"
    assert ac["url"].endswith("/a2a") and ac["protocolVersion"] == "1.0" and ac["version"] == "1.0.0"
    assert tc["ansName"].startswith("ans://v1.0.0.") and tc["endpoints"][0]["agentUrl"] == ac["url"]
    assert divergences(ac, tc, {}) == []                       # we must pass our own drift check
    # self-claims our own pipeline can check directly, stated with their URLs
    for u in (ac["url"], cards.CARD_URL, cards.TRUST_URL, cards.HEALTH_URL):
        assert u in ac["description"]
    assert tc["keys"] == []                                    # no certificate yet: say so, do not fake one


def test_well_known_endpoints_and_health(client):
    assert client.get("/.well-known/agent-card.json").json()["skills"][0]["id"] == "benchmark_agent"
    assert client.get("/.well-known/ans/trust-card.json").json()["agentDisplayName"] == "Anua Benchmarker"
    h = client.get("/health").json()
    assert h["ok"] and h["oracles"] >= 40 and h["cached_reports"] == 0
    assert client.get("/a2a").status_code == 405               # something IS serving: web.endpoint_live -> True


# --- caching is demo insurance ---------------------------------------------------
def test_second_call_is_served_from_cache_with_original_timestamp(client):
    r1 = client.post("/api/benchmark", json={"agent": "dnsdoc.webmesh.ai"}).json()
    r2 = client.post("/api/benchmark", json={"agent": "ans://v1.0.6.dnsdoc.webmesh.ai"}).json()
    assert r1["cached"] is False and r2["cached"] is True
    assert r1["run_at"] == r2["run_at"] and r2["behavior_score"] == 80
    assert client.fake.calls == [("dnsdoc.webmesh.ai", "generated")]
    assert client.get("/api/benchmark/dnsdoc.webmesh.ai").json()["cached"] is True
    r3 = client.post("/api/benchmark", json={"agent": "dnsdoc.webmesh.ai", "force": True}).json()
    assert r3["cached"] is False and len(client.fake.calls) == 2
    assert client.get("/api/benchmark/dnsdoc.webmesh.ai?suite=regression").json()["cached"] is False
    assert client.fake.calls[-1] == ("dnsdoc.webmesh.ai", "regression")


def test_concurrent_requests_join_the_in_flight_run(client):
    client.fake.delay = 0.3
    out = []
    def go(): out.append(client.store.wait_report("impact.webmesh.ai", "generated", False))
    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(out) == 4 and all(r is out[0][0] for r, _ in out)   # one Report object, four callers
    assert client.fake.calls == [("impact.webmesh.ai", "generated")]


def test_cache_warms_from_disk(client, tmp_path):
    (tmp_path / "seo_webmesh_ai_generated_x.json").write_text(_report("seo.webmesh.ai", "generated", 55).model_dump_json())
    (tmp_path / "latest.json").write_text("{}")
    (tmp_path / "junk.json").write_text("not a report")
    assert client.store.warm() == 1
    r = client.get("/api/benchmark/seo.webmesh.ai").json()
    assert r["cached"] is True and r["behavior_score"] == 55 and client.fake.calls == []


def test_bad_input_is_400_and_failed_run_is_502(client, monkeypatch):
    assert client.post("/api/benchmark", json={"agent": "not a host"}).status_code == 400
    assert client.get("/api/benchmark/dnsdoc.webmesh.ai?suite=nope").status_code == 400
    def boom(cfg, **kw): raise RuntimeError("ANTHROPIC_API_KEY is not set")
    monkeypatch.setattr(runs.pipeline, "run", boom)
    r = client.get("/api/benchmark/nokey.example")
    assert r.status_code == 502 and "ANTHROPIC_API_KEY" in r.json()["detail"]


# --- streaming ---------------------------------------------------------------------
def _events(client, url):
    evs, kind = [], None
    with client.stream("GET", url) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: "): kind = line[7:]
            elif line.startswith("data: "): evs.append((kind, json.loads(line[6:])))
    return evs


def test_stream_ticks_six_steps_then_done_and_replays_from_cache(client):
    evs = _events(client, "/api/stream/dnsdoc.webmesh.ai")
    steps = [(d["step"], d["status"], d["detail"]) for k, d in evs if k == "step"]
    assert [s for s in runs.STEPS if any(st == s and status == "done" for st, status, _ in steps)] == list(runs.STEPS)
    assert ("card", "done", "23 claims: 8 self / 15 capability, 4 unverifiable") in steps
    assert ("tests", "running", "12 written (batch 1/2)") in steps
    assert evs[-1][0] == "done" and evs[-1][1]["cached"] is False and evs[-1][1]["behavior_score"] == 80
    # cached replay: same six steps, all done, then the report with cached: true — instantly
    evs2 = _events(client, "/api/stream/dnsdoc.webmesh.ai")
    assert [d["step"] for k, d in evs2 if k == "step"] == list(runs.STEPS)
    assert all(d["status"] == "done" for k, d in evs2 if k == "step")
    assert evs2[-1][0] == "done" and evs2[-1][1]["cached"] is True
    assert len(client.fake.calls) == 1


def test_stream_reports_a_failed_run_as_an_error_event(client, monkeypatch):
    def boom(cfg, on_stage=None, **kw):
        on_stage("discover", "…"); raise RuntimeError("404 Not Found for agent-card.json")
    monkeypatch.setattr(runs.pipeline, "run", boom)
    evs = _events(client, "/api/stream/nocard.example")
    assert evs[-1][0] == "error" and "agent-card.json" in evs[-1][1]["message"]


def test_cached_steps_show_identity_states():
    r = _report("x.example", "generated")
    r.identity.fingerprint_match, r.identity.tl_server_fingerprint, r.identity.status, r.identity.verified = None, None, "PENDING", False
    fp = [s for s in cached_steps(r) if s["step"] == "fingerprint"][0]
    assert fp["status"] == "warn" and "PENDING" in fp["detail"]


# --- A2A: we are a participant ----------------------------------------------------
def _envelope(text, method="message/send"):
    return {"jsonrpc": "2.0", "id": "1", "method": method,
            "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": text}]}}}


def test_a2a_answers_with_a_readable_summary(client):
    r = client.post("/a2a", json=_envelope("Benchmark ans://v1.0.6.dnsdoc.webmesh.ai please")).json()
    assert r["id"] == "1" and "error" not in r
    text = r["result"]["parts"][0]["text"]
    assert r["result"]["kind"] == "message" and r["result"]["role"] == "agent"
    assert "IDENTITY  VERIFIED" in text and "BEHAVIOR  80/100" in text and "COVERAGE  17/23" in text
    assert "HIGH   dns.dnssec @ cloudflare.com" in text and "/api/benchmark/dnsdoc.webmesh.ai" in text
    assert r["result"]["metadata"]["cached"] is False
    # our own transport can read our own answer
    from bench.agent.transport import _extract_text
    assert _extract_text(r["result"]).startswith("Anua Benchmarker — dnsdoc.webmesh.ai")
    assert client.post("/a2a", json=_envelope("Benchmark dnsdoc.webmesh.ai")).json()["result"]["metadata"]["cached"] is True


def test_a2a_errors_follow_json_rpc(client):
    assert client.post("/a2a", json=_envelope("hello there")).json()["error"]["code"] == -32602
    assert client.post("/a2a", json=_envelope("x.example", method="tasks/get")).json()["error"]["code"] == -32601
    assert client.post("/a2a", json={"jsonrpc": "1.0", "id": 2}).json()["error"]["code"] == -32600
    assert client.post("/a2a", content=b"{nope", headers={"content-type": "application/json"}).json()["error"]["code"] == -32700


def test_parse_agent():
    assert parse_agent("ans://v1.0.6.dnsdoc.webmesh.ai") == "dnsdoc.webmesh.ai"
    assert parse_agent("How does https://impact.webmesh.ai/a2a score?") == "impact.webmesh.ai"
    assert parse_agent("benchmark seo.webmesh.ai.") == "seo.webmesh.ai"
    assert parse_agent("e.g. nothing") is None and parse_agent("") is None
