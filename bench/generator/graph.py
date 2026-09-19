"""LangGraph test generator — the primary path, not an add-on.

  parse_claims    LLM reads agent card + trust card + registry entry -> discrete claims
  route_claims    LLM proposes an oracle key per claim; deterministic code REQUIRES the
                  key to exist in ORACLES or the claim falls through to SCHEMA_ONLY /
                  UNVERIFIABLE. Coverage is a first-class result.
  generate_tests  LLM writes 2-4 concrete tests per VERIFIABLE claim, negatives first,
                  each with the prompt we will send the agent (phrased from its examples)
  validate_tests  deterministic guardrail, no LLM: unknown oracle, bad target, dup id,
                  unsupported comparator -> rejected. Cap at max_n.
  report_coverage claims_found / verifiable / schema_only / unverifiable + reasons

The LLM proposes; it never decides truth. Every generated test is graded by an oracle.
"""
from __future__ import annotations
import json
import re
from typing import Any, TypedDict
from langgraph.graph import StateGraph, END
from ..models import Assertion, Kind, Severity, AgentCards, CapabilityClaim, CoverageReport, Verifiability
from ..oracles.registry import ORACLES, SPECS, catalog
from ..llm import LLM, parse_json

SUPPORTED_COMPARATORS = ("bool", "eq", "set_eq", "set_overlap", "contains", "date_close", "nonempty")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$", re.I)
_URL = re.compile(r"^https?://[^\s/]+(/[^\s]*)?$", re.I)
_SCHEMA_WORDS = re.compile(r"\b(a2a|mcp|json-?rpc|endpoint|protocol|streamable|agent card|trust card|signed|signature|"
                           r"x402|payment|auth|did:web|jwks|streaming|push ?notification|input ?modes?|output ?modes?)\b", re.I)


class GenState(TypedDict, total=False):
    cards: dict
    existing_ids: list[str]
    max_n: int
    claims: list[dict]
    proposals: list[dict]
    assertions: list[dict]
    coverage: dict
    notes: list[str]


# ---- prompts ----------------------------------------------------------------
_PARSE_SYS = """You extract capability claims from an AI agent's self-descriptions.
A claim is one discrete, checkable-sounding thing the agent says it does or reports, e.g.
"reports whether the TLS certificate is expired", "reports the page's canonical link",
"scores a domain 1-10 for disruption". Split compound sentences into separate claims.
Include protocol/identity claims (A2A endpoint, MCP tool, signed card) as their own claims.
Do not invent capabilities that are not stated. 5 to 25 claims.

Return STRICT JSON: a list of {"claim_text": "...", "source": "skill"|"description"|"trust_card"|"registry"}.
JSON only."""

_ROUTE_SYS = """You match capability claims to oracle keys. An oracle independently computes the
true value of something about a domain or URL, so an agent's answer can be graded against it.

Oracle catalog (key, input kind, return type, meaning):
{catalog}

For EACH claim return one object:
  {{"claim_text": <verbatim>, "oracle_keys": [<0-3 catalog keys whose meaning the agent's claim
   directly covers>], "kind": "oracle"|"schema"|"none", "reason": "<one line>"}}
Rules: only use keys that appear in the catalog, exactly as written. Use "schema" for claims
about protocols, endpoints, auth, payment, signatures or card structure (checkable by
contract, not by an oracle). Use "none" with a reason when the claim is subjective, a score
the agent invents, or depends on data nobody can independently compute (press coverage,
"what breaks", recommendations, rewriting text). Be strict: a claim about recommendations
is NOT covered by an oracle that measures the underlying fact.
Return STRICT JSON list. JSON only."""

_GEN_SYS = """You design benchmark tests for an AI agent. Each test asks the agent about one
target (a domain or URL) and grades ONE fact the agent reports against an oracle that
computes the truth independently. You choose the target and write the prompt we send.

Oracle keys you may use (with meaning, input kind, comparator, and known negative targets):
{catalog}

Agent facts: name {name}; input modes {input_modes}; the agent's own example prompts:
{examples}

Write 2-4 tests per claim below. Prefer NEGATIVE and EDGE cases the agent could get wrong:
for network oracles use badssl.com subdomains (expired., self-signed., wrong.host.,
untrusted-root., revoked.), NXDOMAIN, domains with no MX; for web oracles use pages with
no <h1>, no meta description, no robots.txt / sitemap, 404 pages, redirects (e.g.
https://example.com/ has a title and h1 but NO meta description, canonical, JSON-LD, OG,
robots.txt or sitemap; https://example.com/does-not-exist is a 404). Targets must be real,
public and stable. Use the same target for several tests where sensible.

Return STRICT JSON list of:
  {{"id": "kebab-case-unique", "claim": "<oracle key>", "target": "<domain or full URL per the
   oracle's input kind>", "comparator": "<the oracle's comparator>", "severity": "HIGH"|"MEDIUM"|"LOW",
   "rationale": "<one line>", "prompt": "<the exact request to send the agent, phrased like its
   examples, naming the target, and explicitly asking it to state the facts under test with
   concrete values (say: respond in JSON if you can)>"}}
Do not reuse these ids: {existing}. At most {n} tests total. JSON only."""


def _trim_cards(cards: dict) -> dict:
    ac, tc, re_ = cards.get("agent_card", {}) or {}, dict(cards.get("trust_card", {}) or {}), cards.get("registry_entry", {}) or {}
    tc.pop("keys", None); tc.pop("transparencyReceipt", None)
    caps = ac.get("capabilities", {}) or {}
    return {
        "agent_card": {k: ac.get(k) for k in ("name", "description", "url", "version", "protocolVersion",
                                              "skills", "supportedInterfaces", "defaultInputModes", "defaultOutputModes",
                                              "securitySchemes", "x-identity") if ac.get(k) is not None}
                      | ({"capabilities": {**{k: v for k, v in caps.items() if k != "extensions"},
                                           "extensions": [{"uri": e.get("uri"), "description": e.get("description"),
                                                           "params": e.get("params")} for e in caps.get("extensions", []) or []]}}
                         if caps else {}),
        "trust_card": tc,
        "registry_entry": {k: re_.get(k) for k in ("ansName", "agentDisplayName", "agentDescription", "agentVersion",
                                                    "endpoints", "lifecycle") if re_.get(k) is not None},
    }


def _examples(ac: dict) -> list[str]:
    out = []
    for s in ac.get("skills", []) or []:
        out.extend(s.get("examples", []) or [])
    return out[:8]


# ---- nodes (closures over the llm) ------------------------------------------
def build_graph(llm: LLM):
    def parse_claims(st: GenState) -> GenState:
        notes = list(st.get("notes", []))
        try:
            raw = llm.complete(_PARSE_SYS, "Agent self-descriptions:\n" + json.dumps(_trim_cards(st["cards"]), indent=1)[:14000])
            props = parse_json(raw, "list")
        except Exception as e:
            notes.append(f"parse_claims failed: {type(e).__name__}: {e}")
            props = []
        claims, seen = [], set()
        for p in props if isinstance(props, list) else []:
            if not isinstance(p, dict) or not str(p.get("claim_text", "")).strip():
                continue
            txt = " ".join(str(p["claim_text"]).split())
            if txt.lower() in seen:
                continue
            seen.add(txt.lower())
            src = str(p.get("source", "description")).lower()
            claims.append(CapabilityClaim(claim_text=txt, source=src if src in ("skill", "description", "trust_card", "registry") else "description").model_dump())
        if not claims:
            notes.append("no capability claims extracted from the agent's self-descriptions")
        return {**st, "claims": claims, "notes": notes}

    def route_claims(st: GenState) -> GenState:
        claims = [CapabilityClaim(**c) for c in st.get("claims", [])]
        notes = list(st.get("notes", []))
        routed: dict[str, dict] = {}
        if claims:
            cat = "\n".join(f"- {c['key']} [{c['input']} -> {c['returns']}]: {c['description']}" for c in catalog())
            try:
                raw = llm.complete(_ROUTE_SYS.format(catalog=cat),
                                   "Claims:\n" + json.dumps([{"claim_text": c.claim_text} for c in claims], indent=1))
                for r in parse_json(raw, "list"):
                    if isinstance(r, dict) and r.get("claim_text"):
                        routed[" ".join(str(r["claim_text"]).split()).lower()] = r
            except Exception as e:
                notes.append(f"route_claims failed: {type(e).__name__}: {e}")
        for c in claims:
            r = routed.get(c.claim_text.lower(), {})
            keys = [k for k in (r.get("oracle_keys") or ([r["oracle_key"]] if r.get("oracle_key") else [])) if k in ORACLES]
            kind = str(r.get("kind", "")).lower()
            if keys:                                                  # deterministic: key MUST exist
                c.verifiability, c.oracle_key, c.reason = Verifiability.VERIFIABLE, ",".join(keys), r.get("reason", "") or f"oracle {keys[0]}"
            elif kind == "schema" or _SCHEMA_WORDS.search(c.claim_text):
                c.verifiability, c.reason = Verifiability.SCHEMA_ONLY, r.get("reason", "") or "contract/protocol claim: checkable by schema, not by an oracle"
            else:
                bad = [k for k in (r.get("oracle_keys") or []) if k not in ORACLES]
                c.verifiability = Verifiability.UNVERIFIABLE
                c.reason = r.get("reason", "") or ("proposed oracle does not exist: " + ", ".join(bad) if bad else "no independent oracle computes this")
        return {**st, "claims": [c.model_dump() for c in claims], "notes": notes}

    def generate_tests(st: GenState) -> GenState:
        claims = [CapabilityClaim(**c) for c in st.get("claims", []) if c.get("verifiability") == "VERIFIABLE"]
        notes = list(st.get("notes", []))
        if not claims:
            notes.append("no VERIFIABLE claims: nothing to generate")
            return {**st, "proposals": [], "notes": notes}
        keys = sorted({k for c in claims for k in c.oracle_key.split(",")})
        cat = "\n".join(f"- {k} [{SPECS[k].input}, comparator {SPECS[k].comparator}]: {SPECS[k].description}"
                        + (f" | negatives: {', '.join(SPECS[k].negatives)}" if SPECS[k].negatives else "") for k in keys)
        ac = st["cards"].get("agent_card", {}) or {}
        sys_ = _GEN_SYS.format(catalog=cat, name=ac.get("name", "?"), input_modes=ac.get("defaultInputModes", ["text/plain"]),
                               examples=json.dumps(_examples(ac)), existing=", ".join(st["existing_ids"]), n=st["max_n"])
        human = "Claims to test:\n" + json.dumps([{"claim_text": c.claim_text, "oracle_keys": c.oracle_key.split(",")} for c in claims], indent=1)
        try:
            props = parse_json(llm.complete(sys_, human), "list")
        except Exception as e:
            notes.append(f"generate_tests failed: {type(e).__name__}: {e}")
            props = []
        return {**st, "proposals": props if isinstance(props, list) else [], "notes": notes}

    g = StateGraph(GenState)
    g.add_node("parse_claims", parse_claims)
    g.add_node("route_claims", route_claims)
    g.add_node("generate_tests", generate_tests)
    g.add_node("validate_tests", validate_tests)
    g.add_node("report_coverage", report_coverage)
    g.set_entry_point("parse_claims")
    g.add_edge("parse_claims", "route_claims")
    g.add_edge("route_claims", "generate_tests")
    g.add_edge("generate_tests", "validate_tests")
    g.add_edge("validate_tests", "report_coverage")
    g.add_edge("report_coverage", END)
    return g.compile()


# ---- deterministic nodes (no LLM; unit-tested offline) ----------------------
def _slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(s).lower())).strip("-")[:48]


def validate_tests(st: GenState) -> GenState:
    seen = set(st.get("existing_ids", []))
    notes = list(st.get("notes", []))
    rejected: dict[str, int] = {}
    out: list[dict] = []
    prompt_by_target: dict[str, str] = {}
    for p in st.get("proposals", []) or []:
        if not isinstance(p, dict):
            continue
        claim, aid = str(p.get("claim", "")).strip(), _slug(p.get("id", ""))
        target = str(p.get("target") or p.get("domain") or p.get("url") or "").strip()
        sp = SPECS.get(claim)
        if sp is None:
            rejected["unknown oracle"] = rejected.get("unknown oracle", 0) + 1; continue
        if not aid or aid in seen:
            rejected["duplicate/empty id"] = rejected.get("duplicate/empty id", 0) + 1; continue
        if sp.input == "url":
            if _DOMAIN.match(target):
                target = f"https://{target.lower()}/"
            if not _URL.match(target):
                rejected["invalid target"] = rejected.get("invalid target", 0) + 1; continue
            inp = {"url": target}
        else:
            target = target.lower().split("://", 1)[-1].split("/", 1)[0]
            if not _DOMAIN.match(target):
                rejected["invalid target"] = rejected.get("invalid target", 0) + 1; continue
            inp = {"domain": target}
        comparator = p.get("comparator") if p.get("comparator") in SUPPORTED_COMPARATORS else sp.comparator
        if comparator == "bool" and sp.returns != "bool":
            comparator = sp.comparator                     # never grade an int/str as a bool
        sev = Severity(p["severity"]) if p.get("severity") in ("HIGH", "MEDIUM", "LOW") else Severity.MEDIUM
        prompt = " ".join(str(p.get("prompt") or "").split())
        if not prompt or target.split("://", 1)[-1].rstrip("/") not in prompt:
            prompt = f"{prompt} Target: {target}. State the facts you observe with concrete values; respond in JSON if you can.".strip()
        prompt = prompt_by_target.setdefault(target, prompt)   # one prompt per target -> one agent call
        seen.add(aid)
        out.append(Assertion(id=aid, kind=Kind.ORACLE, claim=claim, input=inp, prompt=prompt, oracle=claim,
                             comparator=comparator, severity=sev, generated=True,
                             rationale=str(p.get("rationale", ""))[:200]).model_dump())
        if len(out) >= int(st.get("max_n", 30)):
            notes.append(f"generated tests capped at {st.get('max_n', 30)}")
            break
    if rejected:
        notes.append("validate_tests rejected: " + ", ".join(f"{n} {why}" for why, n in rejected.items()))
    return {**st, "assertions": out, "notes": notes}


def report_coverage(st: GenState) -> GenState:
    claims = [CapabilityClaim(**c) for c in st.get("claims", [])]
    n = len(claims)
    v = sum(1 for c in claims if c.verifiability == Verifiability.VERIFIABLE)
    s = sum(1 for c in claims if c.verifiability == Verifiability.SCHEMA_ONLY)
    u = n - v - s
    cov = CoverageReport(claims_found=n, verifiable=v, schema_only=s, unverifiable=u, claims=claims,
                         coverage_ratio=round(v / n, 3) if n else 0.0,
                         tests_generated=len(st.get("assertions", [])), notes=list(st.get("notes", [])))
    return {**st, "coverage": cov.model_dump()}


# ---- entry point -------------------------------------------------------------
def generate(cards: AgentCards, existing: list[Assertion], model: str, max_n: int,
             llm: LLM | None = None) -> tuple[list[Assertion], CoverageReport, list[str]]:
    """Returns (generated assertions, coverage report, notes). Raises if no LLM can be
    built — a missing key must be loud, never a silent skip."""
    if llm is None:
        from ..llm import make_llm
        llm = make_llm(model)
    graph = build_graph(llm)
    st = graph.invoke({"cards": {"agent_card": cards.agent_card, "trust_card": cards.trust_card,
                                 "registry_entry": cards.registry_entry},
                       "existing_ids": [a.id for a in existing], "max_n": max_n, "notes": []})
    cov = CoverageReport(**st["coverage"])
    return [Assertion(**d) for d in st.get("assertions", [])], cov, st.get("notes", [])
