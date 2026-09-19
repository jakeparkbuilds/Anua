"""Design rule 2: an oracle that cannot compute truth never penalises the agent.

These tests pin the boundary between "verdict about the target" (cert is bad) and
"our problem" (timeout, dead host, no nameserver). All offline — failures are injected.
"""
from __future__ import annotations
import socket
import ssl

import dns.resolver
import httpx
import pytest

from bench.assertions.runner import Runner
from bench.models import Assertion, AgentCards, Kind
from bench.oracles import dns as odns, http as ohttp, tls as otls
from bench.oracles.errors import OracleUnavailable, is_cert_verification_error


# --- helpers ---------------------------------------------------------------
def _cert_error() -> ssl.SSLCertVerificationError:
    e = ssl.SSLCertVerificationError("certificate has expired")
    e.verify_message = "certificate has expired"
    return e


class _StubTransport:
    def send(self, prompt):  # pragma: no cover - should never be reached
        raise AssertionError("agent must not be called when ground truth is unknown")


# --- cause-chain detection -------------------------------------------------
def test_cert_error_detected_through_httpx_wrapper():
    """httpx buries the real cause: an expired cert and a dead host are both ConnectError."""
    wrapped = httpx.ConnectError("[SSL] verify failed")
    wrapped.__cause__ = _cert_error()
    assert is_cert_verification_error(wrapped)

    dead_host = httpx.ConnectError("nodename nor servname provided")
    dead_host.__cause__ = socket.gaierror(8, "nodename nor servname provided")
    assert not is_cert_verification_error(dead_host)


def test_cause_chain_walk_survives_a_cycle():
    a = httpx.ConnectError("a")
    b = httpx.ConnectError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert not is_cert_verification_error(a)


# --- TLS -------------------------------------------------------------------
def test_tls_bad_cert_is_a_verdict(monkeypatch):
    monkeypatch.setattr(otls.ssl.SSLContext, "wrap_socket",
                        lambda *a, **k: (_ for _ in ()).throw(_cert_error()))
    monkeypatch.setattr(otls.socket, "create_connection", lambda *a, **k: _FakeSock())
    assert otls.chain_valid("expired.example") is False


def test_tls_unreachable_host_is_unavailable(monkeypatch):
    monkeypatch.setattr(otls.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(socket.timeout("timed out")))
    for fn in (otls.chain_valid, otls.expired, otls.not_after, otls.issuer, otls.hostname_match):
        with pytest.raises(OracleUnavailable):
            fn("unreachable.example")


class _FakeSock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


# --- DNS -------------------------------------------------------------------
def test_dns_nxdomain_is_a_verdict(monkeypatch):
    monkeypatch.setattr(odns, "_resolve",
                        lambda n, t: (_ for _ in ()).throw(dns.resolver.NXDOMAIN()))
    assert odns.resolves("nope.invalid") is False
    assert odns.a_record("nope.invalid") == []
    assert odns.mx("nope.invalid") == []


def test_dns_timeout_is_unavailable(monkeypatch):
    def boom(name, rdtype, **kw):
        raise dns.resolver.NoNameservers("no nameservers")
    monkeypatch.setattr(odns._resolver, "resolve", boom)
    for fn in (odns.resolves, odns.a_record, odns.mx, odns.dnssec, odns.spf, odns.dmarc):
        with pytest.raises(OracleUnavailable):
            fn("example.com")


def test_dns_lifetime_timeout_retries_over_tcp(monkeypatch):
    calls = []

    def resolve(name, rdtype, tcp=False, **kw):
        calls.append(tcp)
        if not tcp:
            raise dns.resolver.LifetimeTimeout("udp timed out")
        return ["answer"]

    monkeypatch.setattr(odns._resolver, "resolve", resolve)
    assert odns._resolve("example.com", "TXT") == ["answer"]
    assert calls == [False, True]


# --- HTTP ------------------------------------------------------------------
def test_https_ok_bad_cert_is_false_dead_host_is_unavailable(monkeypatch):
    def cert_failure(self, *a, **k):
        err = httpx.ConnectError("verify failed")
        err.__cause__ = _cert_error()
        raise err

    monkeypatch.setattr(httpx.Client, "head", cert_failure)
    assert ohttp.https_ok("expired.example") is False

    def dns_failure(self, *a, **k):
        err = httpx.ConnectError("name resolution failed")
        err.__cause__ = socket.gaierror(8, "nodename")
        raise err

    monkeypatch.setattr(httpx.Client, "head", dns_failure)
    with pytest.raises(OracleUnavailable):
        ohttp.https_ok("unreachable.example")
    with pytest.raises(OracleUnavailable):
        ohttp.status("unreachable.example")


# --- runner integration ----------------------------------------------------
def test_runner_skips_and_never_calls_agent_when_truth_unknown(monkeypatch):
    """The whole point: ungraded (passed=None), not failed."""
    monkeypatch.setattr("bench.assertions.runner.compute",
                        lambda claim, domain: (_ for _ in ()).throw(OracleUnavailable("resolver down")))
    a = Assertion(id="t", kind=Kind.ORACLE, claim="tls.chain_valid",
                  input={"domain": "example.com"}, oracle="tls.chain_valid", comparator="bool")
    out = Runner(_StubTransport(), AgentCards()).run(a)
    assert out.passed is None
    assert "SKIPPED" in out.evidence


def test_runner_skips_when_oracle_returns_none(monkeypatch):
    monkeypatch.setattr("bench.assertions.runner.compute", lambda claim, domain: None)
    a = Assertion(id="t", kind=Kind.ORACLE, claim="tls.expired",
                  input={"domain": "example.com"}, oracle="tls.expired", comparator="bool")
    out = Runner(_StubTransport(), AgentCards()).run(a)
    assert out.passed is None


def test_skipped_assertions_excluded_from_pass_rate():
    from bench.report.builder import build
    from bench.models import AgentIdentity

    def mk(aid, passed):
        return Assertion(id=aid, kind=Kind.ORACLE, claim="tls.chain_valid",
                         input={"domain": "d"}, oracle="tls.chain_valid",
                         comparator="bool", passed=passed)

    report = build("a", "h", "mock", AgentIdentity(host="h"), AgentCards(),
                   [mk("p", True), mk("f", False), mk("s", None)],
                   {"oracle": .7, "schema": .2, "quality": .1}, [])
    assert report.summary["oracle_pass_rate"] == 0.5  # 1 of 2 graded, skipped excluded
