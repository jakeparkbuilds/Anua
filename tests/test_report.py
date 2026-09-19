from bench.models import Assertion, Kind, Severity, AgentIdentity, AgentCards
from bench.report.builder import build

def _a(id, kind, passed, sev=Severity.MEDIUM, score=None, actual=None):
    a = Assertion(id=id, kind=kind, claim="c", input={}, severity=sev)
    a.passed = passed
    a.score = score if score is not None else (100.0 if passed else 0.0)
    a.actual = actual
    return a

def test_behavior_score_and_failure_ordering():
    ident = AgentIdentity(host="h", verified=True, ans_name="ans://v1.0.0.h")
    cards = AgentCards()
    asserts = [
        _a("o1", Kind.ORACLE, True), _a("o2", Kind.ORACLE, False, Severity.HIGH),
        _a("o3", Kind.ORACLE, False, Severity.LOW), _a("o4", Kind.ORACLE, None),  # ungraded
        _a("s1", Kind.SCHEMA, True),
        _a("q1", Kind.QUALITY, None, Severity.INFO, score=50.0,
           actual={"readability": 50, "grounding": .5, "actionability": .5, "confidence": "low", "method": "classical"}),
    ]
    r = build("h", "h", "mock", ident, cards, asserts, {"oracle": .7, "schema": .2, "quality": .1}, [])
    # oracle rate 1/3 (o4 ungraded), schema 1.0, quality .5 -> .7*.333+.2*1+.1*.5 = .483
    assert r.behavior_score == 48
    assert [f["id"] for f in r.failures] == ["o2", "o3"]   # HIGH first
    assert r.summary["high_severity_failures"] == 1
    assert "VERIFIED" in r.explanation

def test_ungraded_oracle_does_not_penalize():
    ident = AgentIdentity(host="h"); cards = AgentCards()
    asserts = [_a("o1", Kind.ORACLE, True), _a("o2", Kind.ORACLE, None)]
    r = build("h", "h", "mock", ident, cards, asserts, {"oracle": 1, "schema": 0, "quality": 0}, [])
    assert r.summary["oracle_pass_rate"] == 1.0
