"""Audit round 1, second pass: identity drift, ground truth that is not unique, comparators
that were too strict or too loose, and runs that must not die or be cached wrong."""
import json
from datetime import datetime, timezone, timedelta
import pytest
from bench.assertions.compare import compare, Ungradable
from bench.ans import identity as ident_mod
from bench.ans.registry import Registry
from bench.models import AgentIdentity, Assertion, Kind, AgentCards, Report, QualityScores
from bench.oracles.errors import OracleUnavailable
from bench.oracles import dns as dns_mod
from bench.report.builder import build


# ---- identity: a renewed certificate is not an impersonation alarm -------------------
def _tl(issued="2026-09-12T16:15:17Z", host="dnsdoc.webmesh.ai"):
    return {"payload": {"producer": {"event": {"ansName": f"ans://v1.0.6.{host}", "issuedAt": issued,
                                                "attestations": {"serverCert": {"fingerprint": "SHA256:" + "aa" * 32}}}}}}


class _Cert:
    def __init__(self, not_before):
        self.not_valid_before_utc = not_before
        self.issuer = type("I", (), {"rfc4514_string": lambda self: "CN=YE1,O=Let's Encrypt"})()


def _run_identity(monkeypatch, live_fp, cert_before, tlsa):
    monkeypatch.setattr(ident_mod, "live_server_fingerprint", lambda host, **k: (live_fp, None))
    monkeypatch.setattr(ident_mod, "tlsa_fingerprints", lambda host: tlsa)
    ident_mod._LAST_CERT["dnsdoc.webmesh.ai"] = _Cert(cert_before)
    reg = Registry("https://s", "https://t", 5, live=True)
    monkeypatch.setattr(reg, "tl_entry", lambda ans_id: _tl())
    return ident_mod.verify("dnsdoc.webmesh.ai", reg, live=True,
                            entry_summary={"agentId": "e1b", "ansName": "ans://v1.0.6.dnsdoc.webmesh.ai"})


def test_renewal_after_the_seal_is_rotated_not_mismatch(monkeypatch):
    ident = _run_identity(monkeypatch, "bb" * 32, datetime(2026, 9, 19, 23, 43, tzinfo=timezone.utc), tlsa=None)
    assert ident.status == "ROTATED" and ident.rotated and not ident.verified
    assert any("ROTATED" in n and "not impersonation" in n for n in ident.notes)


def test_a_cert_older_than_the_seal_is_a_real_mismatch(monkeypatch):
    ident = _run_identity(monkeypatch, "bb" * 32, datetime(2026, 8, 1, tzinfo=timezone.utc), tlsa=None)
    assert ident.status == "MISMATCH" and not ident.rotated


def test_a_rotation_published_in_tlsa_is_verified(monkeypatch):
    ident = _run_identity(monkeypatch, "bb" * 32, datetime(2026, 9, 19, tzinfo=timezone.utc), tlsa=["bb" * 32])
    assert ident.status == "VERIFIED" and ident.fingerprint_match and ident.rotated


def test_a_live_cert_absent_from_a_published_tlsa_is_a_mismatch(monkeypatch):
    ident = _run_identity(monkeypatch, "bb" * 32, datetime(2026, 9, 19, tzinfo=timezone.utc), tlsa=["cc" * 32])
    assert ident.status == "MISMATCH"


def test_unchanged_cert_is_still_verified(monkeypatch):
    ident = _run_identity(monkeypatch, "aa" * 32, datetime(2026, 9, 1, tzinfo=timezone.utc), tlsa=None)
    assert ident.status == "VERIFIED"


def test_tl_entry_for_another_host_is_ignored(monkeypatch):
    # the yaml's dnsdoc ansId hint made example.com "find" dnsdoc's entry and report MISMATCH
    monkeypatch.setattr(ident_mod, "live_server_fingerprint", lambda host, **k: ("bb" * 32, None))
    reg = Registry("https://s", "https://t", 5, live=True)
    monkeypatch.setattr(reg, "find_by_host", lambda host, display_name="": None)
    monkeypatch.setattr(reg, "tl_entry", lambda ans_id: _tl(host="dnsdoc.webmesh.ai"))
    ident = ident_mod.verify("example.com", reg, ans_id_hint="e1b", live=True)
    assert not ident.tl_entry_found and ident.status == "NOT_FOUND"
    assert any("not about example.com" in n for n in ident.notes)


def test_cli_host_override_drops_the_yaml_ans_id():
    from bench.__main__ import _set_host
    from bench.config import load
    cfg = load("config.yaml")
    assert cfg.target.ans_id
    _set_host(cfg, "example.com")
    assert cfg.target.host == "example.com" and cfg.target.ans_id == "" and cfg.target.endpoint == ""


# ---- no endpoint: unavailable, not a traceback ------------------------------------------
def test_no_endpoint_is_an_unavailable_agent_not_a_crash():
    from bench.agent import transport
    from bench.config import load
    cfg = load("config.yaml", live=True)
    cfg.target.endpoint = ""
    t = transport.build(cfg, AgentCards())
    from bench.agent.transport import AgentUnavailable
    with pytest.raises(AgentUnavailable) as e:
        t.send("hi")
    assert e.value.permanent and "no endpoint" in str(e.value)


# ---- ground truth that is not unique ------------------------------------------------------
def test_a_records_that_vary_by_resolver_are_not_graded(monkeypatch):
    answers = {None: ["1.1.1.1"], "1.1.1.1": ["2.2.2.2"], "8.8.8.8": ["1.1.1.1"]}
    monkeypatch.setattr(dns_mod, "_a_via", lambda domain, ns: answers[ns])
    with pytest.raises(OracleUnavailable, match="vary by resolver"):
        dns_mod.a_record("google.com")
    monkeypatch.setattr(dns_mod, "_a_via", lambda domain, ns: ["9.9.9.9"])
    assert dns_mod.a_record("example.com") == ["9.9.9.9"]


def test_http_status_accepts_first_or_final_code():
    assert compare("one_of", [301, 200], 200)[0]
    assert compare("one_of", [301, 200], "301")[0]
    assert not compare("one_of", [301, 200], 404)[0]


# ---- comparators: strict where it matters, tolerant where it does not --------------------
def test_contains_tolerates_corporate_suffix_and_trailing_slash():
    assert compare("contains", "MarkMonitor Inc.", "MarkMonitor")[0]
    assert compare("contains", "https://www.cloudflare.com/", "https://www.cloudflare.com")[0]
    assert compare("contains", "R11", "Let's Encrypt R11")[0]
    assert not compare("contains", "MarkMonitor Inc.", "GoDaddy")[0]
    assert not compare("contains", "Example Domain", "Ex")[0]      # too short to be the specific form


def test_lang_compares_the_primary_subtag_only():
    assert compare("lang_eq", "en-us", "en")[0]
    assert not compare("lang_eq", "en", "french")[0]                 # 'en' in 'french' was a pass
    with pytest.raises(Ungradable):
        compare("lang_eq", "", "en")


def test_dates_in_common_formats_are_understood():
    assert compare("date_close", "2026-12-04", "Dec 4 2026")[0]
    assert compare("date_close", "2026-12-04", "Dec  4 23:29:33 2026 GMT")[0]
    assert compare("date_close", "2026-12-04", "December 4, 2026")[0]
    assert not compare("date_close", "2026-12-04", "2026-12-10")[0]
    with pytest.raises(Ungradable, match="format we could not parse"):
        compare("date_close", "2026-12-04", "sometime next winter")


# ---- score: quality is a blend only when it ran -------------------------------------------
def test_no_quality_item_means_no_quality_weight():
    o = [Assertion(id=f"o{i}", kind=Kind.ORACLE, claim="tls.expired", input={}, passed=True) for i in range(5)]
    r = build("a", "h", "live", AgentIdentity(host="h"), AgentCards(), o, {}, [])
    assert r.behavior_score == 100
    q = Assertion(id="q", kind=Kind.QUALITY, claim="quality.explainability", input={}, passed=None, score=50.0,
                  actual=QualityScores().model_dump())
    r = build("a", "h", "live", AgentIdentity(host="h"), AgentCards(), o + [q], {}, [])
    assert r.behavior_score == round(100 * (0.7 * 1 + 0.1 * 0.5) / 0.8)


def test_quality_cannot_flip_a_verdict():
    from bench.assertions.runner import Runner

    class Say:
        def send(self, p): return "Renew the certificate. It expired on 2015-04-12.", 3
    a = Assertion(id="q", kind=Kind.QUALITY, claim="quality.explainability", input={"domain": "example.com"})
    out = Runner(Say(), AgentCards()).run(a)
    assert out.passed is None and out.score > 0


# ---- self-claims cannot pass vacuously ---------------------------------------------------
def test_one_of_oracles_are_not_used_for_self_claims():
    from bench.generator.graph import selfclaim_assertions
    st = {"host": "x.example", "cards": {"agent_card": {"url": "https://x.example/a2a"}}, "existing_ids": [],
          "selfclaims": [{"claim_text": "Is publicly accessible", "oracle_keys": ["http.status"], "target": "x.example"}]}
    out, notes = selfclaim_assertions(st)
    assert out == [] and any("no claimed value" in n for n in notes)


# ---- cache: a flaky run is never served on stage ----------------------------------------
def test_flaky_run_is_not_cached_but_a_consistently_dead_host_is(tmp_path, monkeypatch):
    from bench.server.runs import RunStore, Job
    from bench.server import runs as runs_mod

    def report(assertions):
        return Report(agent="a", target_host="h", mode="live", identity=AgentIdentity(host="h"), cards=AgentCards(),
                      assertions=assertions, quality=QualityScores(), suite="generated")
    flaky = report([Assertion(id="1", kind=Kind.ORACLE, claim="c", input={}, raw_response="ok", passed=True),
                    Assertion(id="2", kind=Kind.ORACLE, claim="c", input={}, passed=None,
                              error="agent unavailable: HTTP 502 Bad Gateway: the agent could not serve the request")])
    dead = report([Assertion(id="1", kind=Kind.ORACLE, claim="c", input={}, passed=None,
                             error="agent unavailable: connection failed: ConnectError"),
                   Assertion(id="2", kind=Kind.SCHEMA, claim="endpoint.reachable", input={}, passed=False,
                             error="agent unavailable: connection failed: ConnectError")])
    store = RunStore("config.yaml", live=True, workers=1)
    for rep, cached in ((flaky, False), (dead, True)):
        monkeypatch.setattr(runs_mod.pipeline, "run", lambda cfg, **k: rep)
        job = Job("h", "generated")
        store._work(job)
        assert job.report is rep and (("h", "generated") in store._cache) == cached
        store._cache.clear()
