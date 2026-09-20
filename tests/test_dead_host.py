"""An agent whose host is dead is a finding, not a crash. This is our own situation on
demo day: anuabot.vip is ACTIVE in ANS with a sealed certificate, and nothing is served
there. The tool must say so, in the same words it would use for anyone else."""
import httpx
import pytest
from bench.agent import card as card_mod
from bench.agent.transport import A2ATransport, AgentUnavailable
from bench.oracles.packs import web
from bench.oracles.errors import OracleUnavailable
from bench.models import Assertion, Kind, AgentCards
from bench.assertions.runner import Runner

REG = {"ansName": "ans://v1.0.0.anuabot.vip", "agentHost": "anuabot.vip",
       "endpoints": [{"agentUrl": "https://anuabot.vip/a2a", "protocol": "A2A"}]}


def _dead(*a, **k):
    raise httpx.ConnectError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure")


def test_card_fetch_survives_a_dead_host_and_uses_the_registered_endpoint(monkeypatch):
    monkeypatch.setattr(card_mod, "_get_json", _dead)
    cards = card_mod.fetch("anuabot.vip", 5, live=True, registry_entry=REG)
    assert cards.agent_card == {} and cards.trust_card == {}
    assert "agent card unreachable" in cards.card_error and "ConnectError" in cards.card_error
    assert cards.endpoint == "https://anuabot.vip/a2a"        # what the agent REGISTERED
    assert cards.declared_protocols == ["a2a"]


def test_transport_connection_failure_is_agent_unavailable_not_a_runner_error(monkeypatch):
    monkeypatch.setattr(httpx.Client, "post", _dead)
    with pytest.raises(AgentUnavailable, match="connection failed"):
        A2ATransport("https://anuabot.vip/a2a", "message/send", 5).send("hi")
    a = Assertion(id="schema-endpoint-reachable", kind=Kind.SCHEMA, claim="endpoint.reachable",
                  input={"domain": "example.com"})
    out = Runner(A2ATransport("https://anuabot.vip/a2a", "message/send", 5), AgentCards()).run(a)
    assert out.passed is False and "refused the call" in out.evidence
    q = Assertion(id="q", kind=Kind.QUALITY, claim="quality.explainability", input={"domain": "example.com"})
    assert Runner(A2ATransport("https://anuabot.vip/a2a", "message/send", 5), AgentCards()).run(q).passed is None


class _Client:
    """Target refuses; control host answers."""
    control_ok = True
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, **k):
        if url == web._CONTROL_URL:
            if self.control_ok:
                return httpx.Response(200, request=httpx.Request("GET", url))
            raise httpx.ConnectError("no route")
        raise httpx.ConnectError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]")


def test_liveness_oracles_say_false_for_a_dead_target_when_we_can_reach_the_world(monkeypatch):
    web._page.cache_clear()
    monkeypatch.setattr(web.httpx, "Client", _Client)
    assert web.endpoint_live("https://anuabot.vip/a2a") is False
    assert web.fetchable("https://anuabot.vip/.well-known/agent-card.json") is False
    # a content oracle still refuses to guess: no page, no title
    with pytest.raises(OracleUnavailable):
        web.title("https://anuabot.vip/")


def test_liveness_oracles_stay_unavailable_when_our_own_network_is_down(monkeypatch):
    web._page.cache_clear()
    monkeypatch.setattr(web.httpx, "Client", _Client)
    monkeypatch.setattr(_Client, "control_ok", False)
    with pytest.raises(OracleUnavailable):
        web.endpoint_live("https://anuabot.vip/a2a")
