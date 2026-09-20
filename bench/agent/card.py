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


_MAX_BYTES = 2_000_000


def _get_json(url: str, timeout: int) -> dict[str, Any]:
    """A JSON object, or httpx.HTTPError. An HTML page, a JSON list, an empty body or a
    multi-megabyte blob at a card URL is "no card here", never a traceback."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        if len(r.content) > _MAX_BYTES:
            raise httpx.HTTPError(f"{url}: body is {len(r.content)} bytes, not a card")
        try:
            data = r.json()
        except ValueError as e:
            raise httpx.HTTPError(f"{url}: body is not JSON ({r.headers.get('content-type', '?')})") from e
        if not isinstance(data, dict):
            raise httpx.HTTPError(f"{url}: JSON is a {type(data).__name__}, not an object")
        return data


def fetch(host: str, timeout: int, live: bool, registry_entry: dict | None = None) -> AgentCards:
    card_error = None
    if live:
        try:
            agent_card = _get_json(f"https://{host}/.well-known/agent-card.json", timeout)
        except httpx.HTTPError as e:
            # No card at all. Do not stop: the registry entry still tells us what this agent
            # CLAIMS (its endpoint, its protocol), and every one of those claims can now be
            # checked against a host that is not answering.
            agent_card, card_error = {}, f"agent card unreachable: {type(e).__name__}: {e}"
        try:
            trust_card = _get_json(f"https://{host}/.well-known/ans/trust-card.json", timeout)
        except httpx.HTTPError:
            trust_card = {}
    else:
        agent_card = json.loads((FIXTURES / "mock_agent_card.json").read_text())
        trust_card = json.loads((FIXTURES / "mock_trust_card.json").read_text())

    cards = AgentCards(agent_card=agent_card, trust_card=trust_card, registry_entry=registry_entry or {},
                       card_error=card_error)
    cards.endpoint = agent_card.get("url") or agent_card.get("endpoint")
    if not cards.endpoint:
        for i in agent_card.get("supportedInterfaces", []) or []:
            if isinstance(i, dict) and i.get("url"):
                cards.endpoint = i["url"]; break
    reg_endpoints = [e for e in (registry_entry or {}).get("endpoints", []) or [] if isinstance(e, dict)]
    if not cards.endpoint:
        for e in reg_endpoints:                     # what the agent REGISTERED as its endpoint
            if e.get("agentUrl"):
                cards.endpoint = e["agentUrl"]; break

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
    if agent_card.get("url") or agent_card.get("supportedInterfaces"):
        protos.add("a2a")
    caps = agent_card.get("capabilities")
    # ack-onchain.dev publishes `capabilities` as a LIST; a dead card is a finding, a
    # traceback is not.
    for e in (caps.get("extensions", []) if isinstance(caps, dict) else []) or []:
        if isinstance(e, dict) and "modelcontextprotocol" in str(e.get("uri", "")):
            protos.add("mcp")
    if trust_card.get("mcp") or agent_card.get("mcp"):
        protos.add("mcp")
    for ep in list(trust_card.get("endpoints", []) or []) + reg_endpoints:
        if isinstance(ep, dict) and ep.get("protocol"):
            protos.add(str(ep["protocol"]).lower())
    cards.declared_protocols = sorted(protos)
    return cards
