"""Claim drift: the agent describes itself in three places — agent card, trust card, ANS
registration metadata — and nobody compares them. We do, against the REAL shapes:

  agent card     url / supportedInterfaces[].url, version, name, skills[].id,
                 capabilities.extensions[] (MCP endpoint), x-identity.ans.uri
  trust card     ansName, version, agentHost, endpoints[{protocol, agentUrl, metaDataUrl}]
  registry entry ansName, agentVersion, agentDisplayName,
                 endpoints[{protocol, agentUrl, transports[], functions[{id,name,tags}]}]

Returns a list of divergence strings; empty means the three agree. (The old check looked
for trust_card["functions"], a key the real trust card never had, so it always passed.)
"""
from __future__ import annotations
import re
from typing import Any


def _norm_url(u: Any) -> str:
    return str(u or "").strip().lower().rstrip("/")


def _norm_ver(v: Any) -> str:
    return str(v or "").strip().lower().lstrip("v")


def _ans_version(ans_name: Any) -> str:
    m = re.match(r"ans://v?([0-9][0-9.]*)\.", str(ans_name or ""))
    return m.group(1) if m else ""


def _endpoint_urls(endpoints: Any, protocol: str = "a2a") -> set[str]:
    out = set()
    for ep in endpoints or []:
        if isinstance(ep, dict) and str(ep.get("protocol", "")).lower() == protocol:
            out.add(_norm_url(ep.get("agentUrl") or ep.get("url")))
    return {u for u in out if u}


def divergences(agent_card: dict, trust_card: dict, registry_entry: dict) -> list[str]:
    ac, tc, re_ = agent_card or {}, trust_card or {}, registry_entry or {}
    out: list[str] = []

    # --- A2A endpoint ---------------------------------------------------------
    card_urls = {_norm_url(ac.get("url"))} | {_norm_url(i.get("url")) for i in ac.get("supportedInterfaces", []) or [] if isinstance(i, dict)}
    card_urls.discard("")
    tc_urls, re_urls = _endpoint_urls(tc.get("endpoints")), _endpoint_urls(re_.get("endpoints"))
    if card_urls and tc_urls and not (card_urls & tc_urls):
        out.append(f"A2A url: agent card {sorted(card_urls)} vs trust card {sorted(tc_urls)}")
    if card_urls and re_urls and not (card_urls & re_urls):
        out.append(f"A2A url: agent card {sorted(card_urls)} vs registry {sorted(re_urls)}")

    # --- version ---------------------------------------------------------------
    vers = {"agent card": _norm_ver(ac.get("version")),
            "trust card": _norm_ver(tc.get("version")) or _ans_version(tc.get("ansName")),
            "registry": _norm_ver(re_.get("agentVersion")) or _ans_version(re_.get("ansName")),
            "agent card x-identity": _ans_version(((ac.get("x-identity") or {}).get("ans") or {}).get("uri"))}
    vers = {k: v for k, v in vers.items() if v}
    if len(set(vers.values())) > 1:
        out.append("version: " + ", ".join(f"{k}={v}" for k, v in vers.items()))

    # --- ANS name ---------------------------------------------------------------
    names = {"trust card": str(tc.get("ansName") or ""), "registry": str(re_.get("ansName") or ""),
             "agent card x-identity": str(((ac.get("x-identity") or {}).get("ans") or {}).get("uri") or "")}
    names = {k: v for k, v in names.items() if v}
    if len(set(names.values())) > 1:
        out.append("ansName: " + ", ".join(f"{k}={v}" for k, v in names.items()))

    # --- declared functions / skills ---------------------------------------------
    card_skills = {str(s.get("id") or s.get("name")) for s in ac.get("skills", []) or [] if isinstance(s, dict)}
    reg_fns = {str(f.get("id") or f.get("name")) for ep in re_.get("endpoints", []) or [] if isinstance(ep, dict)
               for f in ep.get("functions", []) or [] if isinstance(f, dict)}
    if card_skills and reg_fns and card_skills != reg_fns:
        only_reg, only_card = reg_fns - card_skills, card_skills - reg_fns
        if only_reg:
            out.append(f"functions in registry but not agent card: {sorted(only_reg)}")
        if only_card:
            out.append(f"skills in agent card but not registry: {sorted(only_card)}")

    # --- protocols -------------------------------------------------------------
    tc_protos = {str(ep.get("protocol", "")).lower() for ep in tc.get("endpoints", []) or [] if isinstance(ep, dict)}
    re_protos = {str(ep.get("protocol", "")).lower() for ep in re_.get("endpoints", []) or [] if isinstance(ep, dict)}
    tc_protos.discard(""); re_protos.discard("")
    if tc_protos and re_protos and tc_protos != re_protos:
        out.append(f"protocols: trust card {sorted(tc_protos)} vs registry {sorted(re_protos)}")

    # --- display name ------------------------------------------------------------
    if ac.get("name") and re_.get("agentDisplayName") and \
            " ".join(str(ac["name"]).lower().split()) != " ".join(str(re_["agentDisplayName"]).lower().split()):
        out.append(f"name: agent card {ac['name']!r} vs registry {re_['agentDisplayName']!r}")
    return out


def sources_present(agent_card: dict, trust_card: dict, registry_entry: dict) -> list[str]:
    return [n for n, d in (("agent card", agent_card), ("trust card", trust_card), ("registry", registry_entry)) if d]
