"""The performance pass: concurrent prefetch must not change a verdict or an order, the
deadline must skip (ours) rather than fail (theirs), /api/examples must never run
anything, and an unknown host gets the smaller suite."""
import threading
import time
import pytest
from fastapi.testclient import TestClient

from bench.assertions.runner import run_all, Runner
from bench.agent.transport import AgentUnavailable
from bench.models import Assertion, Kind, AgentCards, Report, AgentIdentity, QualityScores
from bench.server import app as app_mod, runs


class Slow:
    """An agent that answers 'True' for everything, slowly, and counts concurrency."""
    def __init__(self, delay=0.15):
        self.delay, self.calls, self.active, self.peak = delay, [], 0, 0
        self.lock = threading.Lock()

    def send(self, prompt):
        with self.lock:
            self.active += 1; self.peak = max(self.peak, self.active); self.calls.append(prompt)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        return '{"dns": {"dnssec": true}}', 1


def _oracle(i, domain="example.com"):
    return Assertion(id=f"o{i}", kind=Kind.ORACLE, claim="dns.dnssec", oracle="dns.dnssec",
                     input={"domain": f"{i}.{domain}"}, expected_override=True)


def test_prefetch_runs_agent_calls_concurrently_and_grades_in_order(monkeypatch):
    from bench.assertions import runner as rmod
    monkeypatch.setattr(rmod, "compute", lambda oracle, target: True)
    A = [_oracle(i) for i in range(8)]
    t = Slow()
    t0 = time.monotonic()
    out = run_all(A, t, AgentCards(), workers=5, timeout_s=None)
    elapsed = time.monotonic() - t0
    assert [a.id for a in out] == [a.id for a in A]                 # order unchanged
    assert all(a.passed is True for a in out)
    assert t.peak > 1 and t.peak <= 5                               # bounded concurrency
    assert elapsed < 8 * 0.15                                       # faster than sequential
    assert len(t.calls) == 8                                        # each prompt asked once


def test_sequential_and_concurrent_runs_give_identical_verdicts(monkeypatch):
    from bench.assertions import runner as rmod
    monkeypatch.setattr(rmod, "compute", lambda oracle, target: True)
    A = lambda: [_oracle(i) for i in range(6)]
    seq = run_all(A(), Slow(0.01), AgentCards(), workers=1, timeout_s=None)
    con = run_all(A(), Slow(0.01), AgentCards(), workers=5, timeout_s=None)
    assert [(a.id, a.passed, a.evidence) for a in seq] == [(a.id, a.passed, a.evidence) for a in con]


def test_deadline_skips_unattempted_tests_instead_of_failing_them(monkeypatch):
    from bench.assertions import runner as rmod
    monkeypatch.setattr(rmod, "compute", lambda oracle, target: True)
    A = [_oracle(i) for i in range(6)] + [Assertion(id="s", kind=Kind.SCHEMA, claim="card.protocol_declared",
                                                    input={"protocol": "a2a"})]
    out = run_all(A, Slow(0.3), AgentCards(declared_protocols=["a2a"]), workers=1, timeout_s=0.5)
    skipped = [a for a in out if a.passed is None and "deadline" in (a.error or "")]
    graded = [a for a in out if a.passed is not None]
    assert skipped and graded                                       # partial result, not nothing
    assert all(a.evidence.startswith("SKIPPED —") for a in skipped) # ours, never a fail
    assert out[-1].id == "s" and out[-1].passed is True             # non-agent checks still run


def test_a_paywall_seen_by_one_worker_blocks_the_rest(monkeypatch):
    from bench.assertions import runner as rmod
    monkeypatch.setattr(rmod, "compute", lambda oracle, target: True)
    class Wall:
        def send(self, prompt):
            raise AgentUnavailable("HTTP 402 payment required", permanent=True)
    out = run_all([_oracle(i) for i in range(5)], Wall(), AgentCards(), workers=5, timeout_s=None)
    assert all(a.passed is None and "402" in a.error for a in out)


# ---- /api/examples -----------------------------------------------------------------
def _rep(host, score, status="OK"):
    return Report(agent=host, target_host=host, mode="live", identity=AgentIdentity(host=host, status="VERIFIED"),
                  cards=AgentCards(), assertions=[], quality=QualityScores(), suite="generated",
                  behavior_score=score, score_status=status, summary={"score_confidence": "high" if score else "none"})


def test_examples_reads_the_cache_and_never_runs(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runs.pipeline, "run", lambda *a, **k: calls.append(1))
    monkeypatch.setenv("BENCH_PREWARM", "")                          # no background runs in a test
    store = runs.RunStore("config.yaml", live=True, workers=1)
    store.out_dir = tmp_path                                          # nothing on disk to warm from
    store._cache[("b.example", "generated")] = _rep("b.example", 40)
    store._cache[("a.example", "generated")] = _rep("a.example", 90)
    store._cache[("c.example", "generated")] = _rep("c.example", None, "INSUFFICIENT_COVERAGE")
    store._cache[("a.example", "regression")] = _rep("a.example", 10)
    monkeypatch.setattr(app_mod, "store", store)
    with TestClient(app_mod.app) as client:
        t0 = time.monotonic()
        rows = client.get("/api/examples").json()
        assert time.monotonic() - t0 < 0.1
    assert [r["host"] for r in rows] == ["a.example", "b.example", "c.example"]   # best first, no score last
    assert rows[0]["score"] == 90 and rows[2]["score"] is None and rows[2]["status"] == "INSUFFICIENT_COVERAGE"
    assert calls == []


def test_unknown_host_gets_the_smaller_generated_suite(tmp_path):
    store = runs.RunStore("config.yaml", live=True, workers=1)
    store.out_dir = tmp_path
    assert store._config_for("never-seen.example", "generated").generator.max_generated == runs.UNKNOWN_HOST_MAX_TESTS
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "seen_example_abc.json").write_text("{}")
    assert store._config_for("seen.example", "generated").generator.max_generated == 30


def test_prewarm_swallows_errors(monkeypatch):
    def boom(cfg, **k): raise RuntimeError("no network")
    monkeypatch.setattr(runs.pipeline, "run", boom)
    store = runs.RunStore("config.yaml", live=True, workers=1)
    store.prewarm(("x.example",))                                   # must not raise


# ---- bare names -----------------------------------------------------------------------
def test_bare_name_resolves_from_cache_then_registry(monkeypatch):
    store = runs.RunStore("config.yaml", live=True, workers=1)
    store._cache[("dnsdoc.webmesh.ai", "generated")] = _rep("dnsdoc.webmesh.ai", 80)
    assert runs.resolve_name("dnsdoc", store) == "dnsdoc.webmesh.ai"          # no network needed
    hits = [{"agentHost": "seo.webmesh.ai", "displayName": "SEO", "lifecycle": {"status": "ACTIVE"}},
            {"agentHost": "seo.other.example", "lifecycle": {"status": "REVOKED"}},
            {"agentHost": "unrelated.example", "displayName": "x"}]
    monkeypatch.setattr(runs.Registry, "search", lambda self, q, limit=10: hits)
    assert runs.resolve_name("seo", store) == "seo.webmesh.ai"                 # ACTIVE first-label match wins
    assert runs.resolve_name("nobody", store) is None
    assert runs.resolve_name("not a name!", store) is None
