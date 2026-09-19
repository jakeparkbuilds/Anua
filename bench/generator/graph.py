"""LangGraph test-case generator.

Reads the agent card, proposes ADDITIONAL test inputs that map onto oracles we already
have, validates them (known oracle key, resolvable-looking domain, no dupes), and
returns Assertions flagged generated=True. Hand-written tests always run; these are
additive and labelled in the report.

Graph:  read_card -> propose -> validate -> done
Requires ANTHROPIC_API_KEY. Without it, generation is skipped with a note (pipeline
still runs). Never raises into the main run.
"""
from __future__ import annotations
import json
import os
import re
from typing import Any, TypedDict
from langgraph.graph import StateGraph, END
from ..models import Assertion, Kind, Severity, AgentCards
from ..oracles.registry import ORACLES
from ..assertions.router import route


class GenState(TypedDict, total=False):
    cards: dict
    existing_ids: list[str]
    max_n: int
    model: str
    proposals: list[dict]
    assertions: list[dict]
    notes: list[str]


_SYSTEM = """You design benchmark tests for an AI agent whose only job is diagnosing a
domain's DNS / TLS / HTTP / email configuration. You may ONLY propose tests whose ground
truth can be computed independently by one of these oracle keys:
{oracles}

Return STRICT JSON: a list of objects with keys
  id (kebab-case), claim (one oracle key), domain, comparator ("bool"|"eq"|"set_overlap"),
  severity ("HIGH"|"MEDIUM"|"LOW"), rationale.
Prefer *negative* cases (badssl.com subdomains such as expired., self-signed.,
wrong.host., untrusted-root., revoked., sha1-intermediate., rc4., dh480.) and edge cases
(IDN domains, apex vs www, domains with no MX). Do not repeat these ids: {existing}.
Propose at most {n}. JSON only, no prose, no code fences."""


def _read_card(state: GenState) -> GenState:
    return state


def _propose(state: GenState) -> GenState:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return {**state, "proposals": [], "notes": state.get("notes", []) + ["generator skipped: ANTHROPIC_API_KEY not set"]}
    try:
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=state["model"], temperature=0.4, max_tokens=2000)
        sys = _SYSTEM.format(oracles=", ".join(ORACLES), existing=", ".join(state["existing_ids"]), n=state["max_n"])
        card = json.dumps(state["cards"], indent=1)[:4000]
        msg = llm.invoke([("system", sys), ("human", f"Agent card:\n{card}\n\nPropose tests.")])
        txt = msg.content if isinstance(msg.content, str) else "".join(getattr(b, "text", "") for b in msg.content)
        m = re.search(r"\[.*\]", txt, re.S)
        props = json.loads(m.group(0)) if m else []
        return {**state, "proposals": props}
    except Exception as e:
        return {**state, "proposals": [], "notes": state.get("notes", []) + [f"generator error: {type(e).__name__}: {e}"]}


_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$", re.I)


def _validate(state: GenState) -> GenState:
    seen = set(state["existing_ids"])
    out: list[dict] = []
    for p in state.get("proposals", []):
        try:
            claim, domain, aid = p["claim"], p["domain"].strip().lower(), p["id"]
        except (KeyError, AttributeError):
            continue
        if claim not in ORACLES or aid in seen or not _DOMAIN.match(domain):
            continue
        if route(claim) is not Kind.ORACLE:
            continue
        seen.add(aid)
        out.append(Assertion(
            id=aid, kind=Kind.ORACLE, claim=claim, input={"domain": domain}, oracle=claim,
            comparator=p.get("comparator", "bool") if p.get("comparator") in ("bool", "eq", "set_overlap") else "bool",
            severity=Severity(p.get("severity", "MEDIUM")) if p.get("severity") in ("HIGH", "MEDIUM", "LOW") else Severity.MEDIUM,
            generated=True, rationale=p.get("rationale", ""),
        ).model_dump())
        if len(out) >= state["max_n"]:
            break
    return {**state, "assertions": out}


def build_graph():
    g = StateGraph(GenState)
    g.add_node("read_card", _read_card)
    g.add_node("propose", _propose)
    g.add_node("validate", _validate)
    g.set_entry_point("read_card")
    g.add_edge("read_card", "propose")
    g.add_edge("propose", "validate")
    g.add_edge("validate", END)
    return g.compile()


def generate(cards: AgentCards, existing: list[Assertion], model: str, max_n: int) -> tuple[list[Assertion], list[str]]:
    graph = build_graph()
    st = graph.invoke({"cards": cards.agent_card, "existing_ids": [a.id for a in existing],
                       "max_n": max_n, "model": model, "notes": []})
    return [Assertion(**d) for d in st.get("assertions", [])], st.get("notes", [])
