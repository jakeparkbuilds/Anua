"""Fetch the agent's three self-descriptions:
   /.well-known/agent-card.json      (A2A agent card)
   /.well-known/ans/trust-card.json  (ANS trust card)
   (registration metadata comes from the TL entry — see ans/registry.py)

Claim-drift = these disagreeing. MVP extracts skills/protocols/endpoint defensively.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
import httpx
from ..models import AgentCards

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _get_json(url: str, timeout: int) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def fetch(host: str, timeout: int, live: bool) -> AgentCards:
    if live:
        agent_card = _get_json(f"https://{host}/.well-known/agent-card.json", timeout)
        try:
            trust_card = _get_json(f"https://{host}/.well-known/ans/trust-card.json", timeout)
        except httpx.HTTPError:
            trust_card = {}
    else:
        agent_card = json.loads((FIXTURES / "mock_agent_card.json").read_text())
        trust_card = json.loads((FIXTURES / "mock_trust_card.json").read_text())

    cards = AgentCards(agent_card=agent_card, trust_card=trust_card)
    cards.endpoint = agent_card.get("url") or agent_card.get("endpoint")

    # A2A cards: skills[] with id/name; some list "capabilities"/"tools"
    skills = agent_card.get("skills") or agent_card.get("tools") or agent_card.get("capabilities") or []
    for s in skills:
        if isinstance(s, dict):
            cards.declared_skills.append(s.get("id") or s.get("name") or json.dumps(s)[:40])
        else:
            cards.declared_skills.append(str(s))

    protos = set()
    for k in ("protocols", "supportedProtocols"):
        for p in agent_card.get(k, []) or trust_card.get(k, []):
            protos.add(str(p).lower())
    if "url" in agent_card:
        protos.add("a2a")
    if trust_card.get("mcp") or agent_card.get("mcp"):
        protos.add("mcp")
    cards.declared_protocols = sorted(protos)
    return cards
